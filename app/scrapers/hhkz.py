from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta
from typing import Optional

import httpx
from bs4 import BeautifulSoup

from app.runtime_config import rc
from app.scrapers.structured_filter import title_is_relevant_async

log = logging.getLogger(__name__)

_BASE = "https://hh.kz"
_SEARCH = f"{_BASE}/search/vacancy"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
_MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4,
    "мая": 5, "июня": 6, "июля": 7, "августа": 8,
    "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}


async def _warmup(client: httpx.AsyncClient) -> None:
    try:
        await client.get(_BASE, timeout=15)
        await asyncio.sleep(1)
    except Exception:
        pass


def _parse_date(s: str) -> Optional[datetime]:
    s = (s or "").strip().lower()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.match(r"(\d{1,2})\s+(\w+)", s)
    if m and m.group(2) in _MONTHS:
        now = datetime.utcnow()
        return datetime(now.year, _MONTHS[m.group(2)], int(m.group(1)))
    return None


async def scrape_hhkz(days_back: int = 1, per_role: int = 50) -> list[dict]:
    from app.db.config_store import get as db_get
    roles = await db_get("roles") or ["Data Scientist", "ML Engineer"]
    cutoff = datetime.utcnow() - timedelta(days=days_back)
    results: list[dict] = []
    seen_ids: set[str] = set()

    async with httpx.AsyncClient(headers=_HEADERS, follow_redirects=True) as client:
        await _warmup(client)
        for role in roles:
            try:
                params = {
                    "text": role,
                    "area": 160,
                    "search_field": "name",
                    "per_page": min(per_role, 100),
                    "order_by": "publication_time",
                }
                r = await client.get(_SEARCH, params=params, timeout=20)
                if r.status_code != 200:
                    log.warning("hh.kz %s returned %s", role, r.status_code)
                    continue

                soup = BeautifulSoup(r.text, "lxml")
                cards = soup.select("div[data-qa='vacancy-serp__vacancy']")
                log.info("hh.kz role=%s cards=%d", role, len(cards))

                for card in cards:
                    try:
                        title_el = card.select_one("a[data-qa='serp-item__title']")
                        if not title_el:
                            continue
                        title = title_el.get_text(strip=True)
                        url = title_el.get("href", "").split("?")[0]
                        job_id = url.rstrip("/").split("/")[-1]

                        if job_id in seen_ids:
                            continue
                        if not await title_is_relevant_async(title):
                            continue

                        company_el = card.select_one("[data-qa='vacancy-serp__vacancy-employer']")
                        company = company_el.get_text(strip=True) if company_el else ""

                        sal_el = card.select_one("[data-qa='vacancy-serp__vacancy-compensation']")
                        salary = sal_el.get_text(strip=True) if sal_el else ""

                        date_el = card.select_one("span[data-qa='vacancy-serp__vacancy-date']")
                        pub_date = _parse_date(date_el.get_text(strip=True)) if date_el else None
                        if pub_date is None or pub_date < cutoff:
                            continue

                        seen_ids.add(job_id)
                        results.append({
                            "source": "hh",
                            "message_id": f"hh_{job_id}",
                            "title": title,
                            "company": company,
                            "url": url if url.startswith("http") else f"{_BASE}{url}",
                            "salary": salary,
                            "description": f"{title} at {company}. {salary}",
                        })
                    except Exception as e:
                        log.debug("hh.kz card error: %s", e)

                await asyncio.sleep(1.5)
            except Exception as e:
                log.warning("hh.kz error role=%s: %s", role, e)

    log.info("hh.kz total=%d", len(results))
    return results