"""
Remote job scraper: YCombinator "Who's Hiring" thread only.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

log = logging.getLogger(__name__)

import html as _html_mod
import re as _re_mod


def _epoch_to_date(epoch) -> Optional[str]:
    try:
        return datetime.fromtimestamp(int(epoch), tz=timezone.utc).strftime("%d.%m.%Y")
    except Exception:
        return None


def _strip_html(text: str) -> str:
    text = _html_mod.unescape(text or "")
    text = _re_mod.sub(r"<[^>]+>", " ", text)
    return _re_mod.sub(r"\s{2,}", " ", text).strip()


_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; JobAgent/1.0)"}


async def _scrape_yc(client: httpx.AsyncClient) -> list[dict]:
    results = []
    try:
        r = await client.get(
            "https://hacker-news.firebaseio.com/v0/user/whoishiring/submitted.json",
            timeout=15,
        )
        if r.status_code != 200:
            from app.monitoring.events import log_event
            await log_event("scrape_error", f"[YC] whoishiring list: HTTP {r.status_code}", "WARNING")
            return []
        story_ids = r.json()[:3]

        for sid in story_ids:
            sr = await client.get(
                f"https://hacker-news.firebaseio.com/v0/item/{sid}.json",
                timeout=15,
            )
            if sr.status_code != 200:
                continue
            story = sr.json()
            if "hiring" not in (story.get("title") or "").lower():
                continue
            kids = story.get("kids", [])[:100]
            for kid in kids:
                try:
                    cr = await client.get(
                        f"https://hacker-news.firebaseio.com/v0/item/{kid}.json",
                        timeout=10,
                    )
                    if cr.status_code != 200:
                        continue
                    comment = cr.json()
                    text = comment.get("text") or ""
                    first_line = text.split("<")[0][:100]
                    from app.scrapers.structured_filter import title_seniority_check
                    check = await title_seniority_check(first_line)
                    if check == "drop":
                        continue
                    results.append({
                        "source": "remote_yc",
                        "message_id": f"yc_{kid}",
                        "title": first_line.strip() or "YC Hiring",
                        "company": "",
                        "url": f"https://news.ycombinator.com/item?id={kid}",
                        "description": _strip_html(text)[:500],
                        "pub_date": _epoch_to_date(comment.get("time")),
                        "seniority": "senior" if check == "senior" else None,
                    })
                    await asyncio.sleep(0.1)
                except Exception:
                    pass
            break
    except Exception as e:
        log.warning("YC scrape error: %s", e)
        from app.monitoring.events import log_event
        await log_event("scrape_error", f"[YC] {e}", "WARNING")
    return results


async def scrape_remote_jobs() -> list[dict]:
    async with httpx.AsyncClient(headers=_HEADERS, follow_redirects=True) as client:
        results = await _scrape_yc(client)

    seen: set[str] = set()
    deduped = []
    for j in results:
        mid = j.get("message_id", "")
        if mid and mid not in seen:
            seen.add(mid)
            deduped.append(j)

    log.info("Remote (YC) total=%d after dedup=%d", len(results), len(deduped))
    return deduped
