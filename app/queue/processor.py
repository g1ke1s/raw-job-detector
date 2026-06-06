from __future__ import annotations

import logging
import re
from datetime import datetime

from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.db.models import AllMessage, Match, RunLog
from app.dedup.fingerprint import is_duplicate, is_text_duplicate, make_text_fingerprint
from app.monitoring.events import log_event
from app.runtime_config import rc

# Matches @handle only when NOT preceded by word/email chars (excludes user@domain)
# and NOT followed by .letter (excludes @domain.tld)
_TG_HANDLE_RE = re.compile(
    r"(?<![a-zA-Z0-9_.+\-])@([a-zA-Z0-9_]{4,32})(?!\.[a-zA-Z])"
)
_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
_CONTACT_MARKERS = re.compile(
    r"(контакт|contact|отклик|резюме|писать|пишите|telegram|тг|tg|связь|"
    r"обращайтесь|откликнуться|apply|reach out)",
    re.IGNORECASE,
)

log = logging.getLogger(__name__)

# Telegram location filter: posts that mention non-KZ cities without any
# Kazakhstan/Almaty/remote signal are skipped in almaty mode.
_NON_KZ_CITIES_RE = re.compile(
    r"\b(москв[аеуио]|московск|питер|санкт.петербург|минск|ташкент|"
    r"бишкек|новосибирск|екатеринбург|тбилиси|баку|ереван|киев|київ)\b",
    re.IGNORECASE,
)
_KZ_OR_REMOTE_RE = re.compile(
    r"\b(алматы|almaty|казахстан|kazakhstan|қазақстан|астана|нур.султан|"
    r"шымкент|remote|удален|дистанц|онлайн)\b",
    re.IGNORECASE,
)

_paused = False


def set_paused(val: bool) -> None:
    global _paused
    _paused = val


def is_paused() -> bool:
    return _paused



def _extract_handle(text: str) -> str | None:
    """
    Extract recruiter contact from post text.
    Priority:
    1. Telegram @handle near a contact keyword
    2. Any Telegram @handle anywhere in text
    3. Email address near a contact keyword
    4. Any email address

    Telegram handles are distinguished from emails by requiring no word
    character immediately before the @, and no .letter immediately after
    the handle (which would indicate an email domain like @company.kz).
    """
    if not text:
        return None

    lines = text.split("\n")

    # Pass 1: @handle near contact marker
    for i, line in enumerate(lines):
        if _CONTACT_MARKERS.search(line):
            block = "\n".join(lines[i:i + 4])
            m = _TG_HANDLE_RE.search(block)
            if m:
                return f"@{m.group(1)}"

    # Pass 2: any @handle in text
    m = _TG_HANDLE_RE.search(text)
    if m:
        return f"@{m.group(1)}"

    # Pass 3: email near contact marker
    for i, line in enumerate(lines):
        if _CONTACT_MARKERS.search(line):
            block = "\n".join(lines[i:i + 4])
            emails = _EMAIL_RE.findall(block)
            if emails:
                return emails[0]

    # Pass 4: any email
    emails = _EMAIL_RE.findall(text)
    if emails:
        return emails[0]

    return None

async def run_pipeline(night_run: bool = False, location_filter: str = "almaty") -> dict:
    """
    location_filter: "almaty" — hh.kz area=160, LinkedIn Almaty, Telegram non-KZ skipped
                     "kz"     — hh.kz area=40 (all KZ), LinkedIn Kazakhstan, all Telegram
    """
    if _paused:
        return {"error": "Pipeline is paused. Use /resume to continue."}

    sources = rc.get("scraping.active_sources") or ["hh", "linkedin", "telegram", "remote"]
    days_back = 2 if night_run else int(rc.get("scraping.days_back") or 1)
    min_len = int(rc.get("filtering.min_message_length") or 20)

    hh_area = 160 if location_filter == "almaty" else 40
    linkedin_location = "Almaty, Kazakhstan" if location_filter == "almaty" else "Kazakhstan"

    run = RunLog(started_at=datetime.utcnow(), status="running", is_night=night_run)
    async with AsyncSessionLocal() as s:
        s.add(run)
        await s.commit()
        run_id = run.id

    scraped = enqueued = llm_calls = 0
    per_source: dict[str, int] = {}

    try:
        all_jobs: list[dict] = []

        if "hh" in sources:
            from app.scrapers.hhkz import scrape_hhkz
            jobs = await scrape_hhkz(
                days_back=days_back,
                per_role=int(rc.get("scraping.hh_per_role") or 50),
                area=hh_area,
            )
            all_jobs.extend(jobs)
            per_source["hh"] = len(jobs)
            await log_event("scrape_done", f"hh count={len(jobs)}")

        if "linkedin" in sources:
            from app.scrapers.linkedin import scrape_linkedin
            jobs = await scrape_linkedin(
                max_results=int(rc.get("scraping.linkedin_max") or 25),
                location=linkedin_location,
            )
            all_jobs.extend(jobs)
            per_source["linkedin"] = len(jobs)
            await log_event("scrape_done", f"linkedin count={len(jobs)}")

        if "telegram" in sources:
            from app.scrapers.telegram_groups import scrape_telegram
            jobs = await scrape_telegram(
                days_back=days_back,
                messages_per_channel=int(rc.get("scraping.telegram_messages") or 50),
            )
            all_jobs.extend(jobs)
            per_source["telegram"] = len(jobs)
            await log_event("scrape_done", f"telegram raw={len(jobs)}")

        if "remote" in sources:
            from app.scrapers.remote_jobs import scrape_remote_jobs
            jobs = await scrape_remote_jobs()
            all_jobs.extend(jobs)
            per_source["remote"] = len(jobs)
            await log_event("scrape_done", f"remote count={len(jobs)}")

        scraped = len(all_jobs)
        log.info("Total scraped: %d", scraped)

        for item in all_jobs:
            source = item.get("source", "")
            message_id = item.get("message_id", "")
            if not message_id:
                continue

            async with AsyncSessionLocal() as s:
                existing = await s.scalar(
                    select(AllMessage).where(
                        AllMessage.source == source,
                        AllMessage.message_id == message_id,
                    )
                )
                if existing:
                    continue
                s.add(AllMessage(
                    source=source, message_id=message_id,
                    raw_text=item.get("raw_text") or item.get("description", ""),
                    url=item.get("url", ""),
                ))
                await s.commit()

            if source == "telegram":
                raw_text = item.get("raw_text", "")
                # Location filter: skip posts that mention non-KZ cities
                # without any Kazakhstan/Almaty/remote signal
                if location_filter == "almaty":
                    if _NON_KZ_CITIES_RE.search(raw_text) and not _KZ_OR_REMOTE_RE.search(raw_text):
                        continue
                from app.scrapers.telegram_classify import classify_telegram_post
                vacancies = await classify_telegram_post(raw_text, min_length=min_len)
                for vac in vacancies:
                    company = item.get("channel", "telegram")
                        # Text-based dedup: same vacancy in different channels → same fingerprint
                    if await is_text_duplicate(vac.text, source):
                        continue
                    if "llm" in vac.reason:
                        llm_calls += 1
                    handle = _extract_handle(raw_text)
                    await _enqueue(
                        source=source,
                        title=vac.role or "Job (Telegram)",
                        company=company,
                        url=item.get("url", ""),
                        description=vac.text[:1000],
                        rule_score=vac.rule_score,
                        low_conf=vac.low_conf,
                        recruiter_handle=handle,
                        night_run=night_run,
                        extra={
                            "channel": item.get("channel"),
                            "reason": vac.reason,
                            "pub_date": item.get("pub_date"),
                        },
                    )
                    enqueued += 1
            else:
                title = item.get("title", "")
                company = item.get("company", "")
                if await is_duplicate(company, title, source):
                    continue
                await _enqueue(
                    source=source, title=title, company=company,
                    url=item.get("url", ""),
                    description=item.get("description", ""),
                    rule_score=1.0, low_conf=False,
                    recruiter_handle=None,
                    night_run=night_run,
                    extra={"pub_date": item.get("pub_date")},
                )
                enqueued += 1

        async with AsyncSessionLocal() as s:
            run = await s.get(RunLog, run_id)
            run.finished_at = datetime.utcnow()
            run.status = "done"
            run.scraped = scraped
            run.enqueued = enqueued
            run.llm_calls = llm_calls
            await s.commit()

        await log_event("run_done", f"scraped={scraped} enqueued={enqueued} llm={llm_calls} night={night_run}")
        return {
            "scraped": scraped,
            "enqueued": enqueued,
            "llm_calls": llm_calls,
            "per_source": per_source,
            "night_run": night_run,
        }

    except Exception as e:
        log.error("Pipeline error: %s", e, exc_info=True)
        async with AsyncSessionLocal() as s:
            run = await s.get(RunLog, run_id)
            run.finished_at = datetime.utcnow()
            run.status = "error"
            run.error_msg = str(e)[:500]
            await s.commit()
        await log_event("run_error", str(e), level="ERROR")
        return {"error": str(e)}


async def _enqueue(
    source, title, company, url, description,
    rule_score, low_conf, recruiter_handle, night_run, extra,
) -> None:
    async with AsyncSessionLocal() as s:
        s.add(Match(
            source=source, title=title, company=company, url=url,
            description=description[:2000],
            rule_score=rule_score, low_conf=low_conf,
            status="waiting",
            recruiter_handle=recruiter_handle,
            night_run=night_run,
            extra=extra,
        ))
        await s.commit()