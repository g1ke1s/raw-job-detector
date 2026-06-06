"""
LinkedIn guest (public) API scraper.
No auth required. Capped at 25 results per run to stay under radar.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

import httpx

from app.scrapers.structured_filter import title_is_relevant_async

log = logging.getLogger(__name__)

_SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
}


async def scrape_linkedin(max_results: int = 25, location: str = "Almaty, Kazakhstan") -> list[dict]:
    from app.db.config_store import get as db_get
    roles = await db_get("roles") or ["Data Scientist"]
    results: list[dict] = []
    seen_ids: set[str] = set()

    async with httpx.AsyncClient(headers=_HEADERS, follow_redirects=True) as client:
        for role in roles:
            if len(results) >= max_results:
                break
            try:
                params = {
                    "keywords": role,
                    "location": location,
                    "f_TPR": "r86400",      # last 24h
                    "start": 0,
                    "count": 10,
                }
                r = await client.get(_SEARCH_URL, params=params, timeout=20)
                if r.status_code != 200:
                    log.warning("LinkedIn %s returned %s", role, r.status_code)
                    continue

                # LinkedIn guest API returns HTML fragments
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(r.text, "lxml")
                cards = soup.select("li")

                for card in cards:
                    if len(results) >= max_results:
                        break
                    try:
                        title_el = card.select_one("h3.base-search-card__title")
                        if not title_el:
                            continue
                        title = title_el.get_text(strip=True)
                        if not await title_is_relevant_async(title):
                            continue

                        link_el = card.select_one("a.base-card__full-link")
                        url = link_el.get("href", "") if link_el else ""
                        job_id = url.split("/")[-1].split("?")[0] if url else ""
                        if job_id in seen_ids:
                            continue

                        company_el = card.select_one("h4.base-search-card__subtitle")
                        company = company_el.get_text(strip=True) if company_el else ""

                        seen_ids.add(job_id)
                        results.append({
                            "source": "linkedin",
                            "message_id": f"li_{job_id}",
                            "title": title,
                            "company": company,
                            "url": url,
                            "description": f"{title} at {company}",
                        })
                    except Exception as e:
                        log.debug("LinkedIn card error: %s", e)

                await asyncio.sleep(2)
            except Exception as e:
                log.warning("LinkedIn scrape error role=%s: %s", role, e)

    log.info("LinkedIn total=%d", len(results))
    return results
