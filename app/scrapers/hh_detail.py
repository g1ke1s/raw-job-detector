from __future__ import annotations

import html as html_mod
import logging
import re
from typing import Optional

import httpx
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}


def _clean(text: str) -> str:
    text = html_mod.unescape(text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s{2,}", " ", text).strip()


async def fetch_hh_details(url: str) -> Optional[dict]:
    try:
        async with httpx.AsyncClient(headers=_HEADERS, follow_redirects=True, timeout=20) as client:
            r = await client.get(url)
            if r.status_code != 200:
                log.warning("hh detail %s returned %s", url, r.status_code)
                return None

        soup = BeautifulSoup(r.text, "lxml")

        def _text(selector: str) -> str:
            el = soup.select_one(selector)
            return _clean(el.get_text(" ", strip=True)) if el else ""

        desc_el = soup.select_one("div[data-qa='vacancy-description']")
        description = _clean(desc_el.get_text(" ", strip=True)) if desc_el else ""

        skills = [
            _clean(s.get_text(strip=True))
            for s in soup.select("div[data-qa='bloko-tag__text']")
            if s.get_text(strip=True)
        ]

        exp = _text("span[data-qa='vacancy-experience']")
        emp = _text("p[data-qa='vacancy-view-employment-mode']")
        sched = _text("p[data-qa='vacancy-view-work-format']") or \
                _text("p[data-qa='vacancy-view-schedule']")
        salary = _text("span[data-qa='vacancy-salary-compensation-type-net']") or \
                 _text("div[data-qa='vacancy-salary']")
        company = _text("span[data-qa='vacancy-company-name']")

        return {
            "description": description[:2000],
            "skills": skills[:20],
            "experience": exp,
            "employment": emp,
            "schedule": sched,
            "salary": salary,
            "company": company,
        }
    except Exception as e:
        log.error("hh_detail error: %s", e)
        return None


def format_hh_details(details: dict, title: str, url: str) -> str:
    lines = [f"HH: {title}"]
    if details.get("company"):
        lines.append(f"Компания: {details['company']}")
    if details.get("salary"):
        lines.append(f"Зарплата: {details['salary']}")
    if details.get("experience"):
        lines.append(f"Опыт: {details['experience']}")
    if details.get("employment"):
        lines.append(f"Занятость: {details['employment']}")
    if details.get("schedule"):
        lines.append(f"График: {details['schedule']}")
    if details.get("skills"):
        lines.append(f"Навыки: {', '.join(details['skills'][:10])}")
    if details.get("description"):
        lines.append(f"\n{details['description'][:800]}")
    lines.append(f"\n{url}")
    return "\n".join(lines)