"""
Remote job board scrapers: Remotive, RemoteOK, WeWorkRemotely, Himalayas, YCombinator.
All public APIs/pages, no auth needed.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

import httpx
from bs4 import BeautifulSoup

from app.scrapers.remote_filter import job_passes_remote_filter

log = logging.getLogger(__name__)

import html as _html_mod
import re as _re_mod

def _strip_html(text: str) -> str:
    text = _html_mod.unescape(text or "")
    text = _re_mod.sub(r"<[^>]+>", " ", text)
    return _re_mod.sub(r"\s{2,}", " ", text).strip()

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; JobAgent/1.0)"
    )
}


# ── Remotive ──────────────────────────────────────────────────────────────
async def _scrape_remotive(client: httpx.AsyncClient, roles: list[str]) -> list[dict]:
    results = []
    for role in roles:
        try:
            r = await client.get(
                "https://remotive.com/api/remote-jobs",
                params={"search": role, "limit": 20},
                timeout=20,
            )
            if r.status_code != 200:
                continue
            for j in r.json().get("jobs", []):
                title = j.get("title", "")
                if not job_passes_remote_filter(title, j.get("candidate_required_location", "")):
                    continue
                results.append({
                    "source": "remote_remotive",
                    "message_id": f"remotive_{j.get('id')}",
                    "title": title,
                    "company": j.get("company_name", ""),
                    "url": j.get("url", ""),
                    "description": _strip_html(j.get("description", ""))[:500],
                })
        except Exception as e:
            log.warning("Remotive error: %s", e)
    return results


# ── RemoteOK ──────────────────────────────────────────────────────────────
async def _scrape_remoteok(client: httpx.AsyncClient, roles: list[str]) -> list[dict]:
    results = []
    try:
        r = await client.get("https://remoteok.com/api", timeout=20)
        if r.status_code != 200:
            return []
        jobs = r.json()
        for j in jobs:
            if not isinstance(j, dict):
                continue
            title = j.get("position", "")
            tags = j.get("tags", [])
            if not job_passes_remote_filter(title, "", tags):
                continue
            results.append({
                "source": "remote_remoteok",
                "message_id": f"remoteok_{j.get('id')}",
                "title": title,
                "company": j.get("company", ""),
                "url": j.get("url", ""),
                "description": _strip_html(j.get("description", ""))[:500],
            })
    except Exception as e:
        log.warning("RemoteOK error: %s", e)
    return results


# ── WeWorkRemotely ────────────────────────────────────────────────────────
async def _scrape_wwr(client: httpx.AsyncClient) -> list[dict]:
    results = []
    for category in ["data-science", "machine-learning"]:
        try:
            r = await client.get(
                f"https://weworkremotely.com/categories/remote-{category}-jobs.rss",
                timeout=20,
            )
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, "xml")
            for item in soup.find_all("item")[:20]:
                title = item.find("title")
                title = title.get_text(strip=True) if title else ""
                # strip company prefix "Company: Title"
                if ": " in title:
                    title = title.split(": ", 1)[1]
                if not job_passes_remote_filter(title):
                    continue
                link = item.find("link")
                guid = item.find("guid")
                company_el = item.find("company")
                results.append({
                    "source": "remote_wwr",
                    "message_id": f"wwr_{guid.get_text() if guid else title[:40]}",
                    "title": title,
                    "company": company_el.get_text(strip=True) if company_el else "",
                    "url": link.get_text(strip=True) if link else "",
                    "description": title,
                })
        except Exception as e:
            log.warning("WWR error: %s", e)
    return results


# ── Himalayas ─────────────────────────────────────────────────────────────
async def _scrape_himalayas(client: httpx.AsyncClient, roles: list[str]) -> list[dict]:
    results = []
    for role in roles[:3]:  # keep it light
        try:
            r = await client.get(
                "https://himalayas.app/jobs/api",
                params={"q": role, "limit": 15},
                timeout=20,
            )
            if r.status_code != 200:
                continue
            for j in r.json().get("jobs", []):
                title = j.get("title", "")
                if not job_passes_remote_filter(title, j.get("locationRestrictions", "")):
                    continue
                results.append({
                    "source": "remote_himalayas",
                    "message_id": f"himalayas_{j.get('slug', title[:30])}",
                    "title": title,
                    "company": j.get("companyName", ""),
                    "url": j.get("applicationLink") or j.get("url", ""),
                    "description": _strip_html(j.get("description", ""))[:500],
                })
        except Exception as e:
            log.warning("Himalayas error: %s", e)
    return results


# ── YCombinator / HN Who's Hiring ─────────────────────────────────────────
async def _scrape_yc(client: httpx.AsyncClient) -> list[dict]:
    """Scrapes the HN 'Who is Hiring?' thread for ML/DS roles."""
    results = []
    try:
        # Find the latest "Who is Hiring?" post
        r = await client.get(
            "https://hacker-news.firebaseio.com/v0/user/whoishiring/submitted.json",
            timeout=15,
        )
        if r.status_code != 200:
            return []
        story_ids = r.json()[:3]  # check the 3 most recent

        for sid in story_ids:
            sr = await client.get(
                f"https://hacker-news.firebaseio.com/v0/item/{sid}.json",
                timeout=15,
            )
            if sr.status_code != 200:
                continue
            story = sr.json()
            title_lower = (story.get("title") or "").lower()
            if "hiring" not in title_lower:
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
                    # quick role check
                    from app.scrapers.structured_filter import title_is_relevant
                    first_line = text.split("<")[0][:100]
                    if not title_is_relevant(first_line):
                        continue
                    results.append({
                        "source": "remote_yc",
                        "message_id": f"yc_{kid}",
                        "title": first_line.strip() or "YC Hiring",
                        "company": "",
                        "url": f"https://news.ycombinator.com/item?id={kid}",
                        "description": _strip_html(text)[:500],
                    })
                    await asyncio.sleep(0.1)
                except Exception:
                    pass
            break  # only process the most recent valid thread
    except Exception as e:
        log.warning("YC scrape error: %s", e)
    return results


# ── Entry point ───────────────────────────────────────────────────────────
async def scrape_remote_jobs() -> list[dict]:
    from app.db.config_store import get as db_get
    roles = await db_get("roles") or ["Data Scientist", "ML Engineer"]
    results: list[dict] = []

    async with httpx.AsyncClient(headers=_HEADERS, follow_redirects=True) as client:
        tasks = [
            _scrape_remotive(client, roles),
            _scrape_remoteok(client, roles),
            _scrape_wwr(client),
            _scrape_himalayas(client, roles),
            _scrape_yc(client),
        ]
        for coro in asyncio.as_completed(tasks):
            try:
                batch = await coro
                results.extend(batch)
            except Exception as e:
                log.warning("Remote scraper error: %s", e)

    # Dedup by message_id
    seen: set[str] = set()
    deduped = []
    for j in results:
        mid = j.get("message_id", "")
        if mid and mid not in seen:
            seen.add(mid)
            deduped.append(j)

    log.info("Remote jobs total=%d after dedup=%d", len(results), len(deduped))
    return deduped
