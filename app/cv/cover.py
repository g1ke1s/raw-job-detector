"""LLM cover letter generation — fires only on Boost, never during scraping."""
from __future__ import annotations

import logging
import re
from typing import Optional

from app.db.session import AsyncSessionLocal
from app.db.models import Match
from app.llm.client import complete

log = logging.getLogger(__name__)

_RU_RE = re.compile(r"[а-яёА-ЯЁ]")

_SYSTEM_EN = (
    "You are helping an AI/ML engineer apply for jobs. Write a short, direct cover letter "
    "that sounds like a real person, not a form letter. 3-5 short paragraphs. No filler. "
    "No opener like \"I am writing to express my interest\". No corporate buzzwords. "
    "Reference specific things from the job description. Ground everything in the "
    "candidate's actual experience provided below — do not invent anything. "
    "Output ONLY the letter body — no subject line, no greeting header, no markdown."
)

_SYSTEM_RU = (
    "Ты помогаешь AI/ML инженеру подать заявку на работу. Напиши короткое, прямое "
    "сопроводительное письмо — так, как написал бы живой человек, а не шаблон. "
    "3-5 коротких абзаца. Без слов-наполнителей. Без открытия вроде «Я хочу выразить "
    "свой интерес к вакансии...». Без корпоративных клише. Ссылайся на конкретные "
    "детали из описания вакансии. Опирайся только на реальный опыт кандидата ниже — "
    "ничего не придумывай. Выводи ТОЛЬКО текст письма — без темы, без заголовка, "
    "без markdown."
)


def _detect_language(text: str) -> str:
    ru_chars = len(_RU_RE.findall(text[:600]))
    return "ru" if ru_chars > 25 else "en"


async def generate_cover(match_id: int) -> Optional[str]:
    """
    Generate a grounded cover letter for match_id using the active CV profile.
    Language is detected from the job description.
    Returns the letter text, or None if profile missing / LLM unavailable.
    Stores result in match.cover_text.
    """
    from app.cv.ingest import get_active_profile

    profile = await get_active_profile()
    if not profile or not profile.structured_json:
        log.info("cover_letter: no active profile for match %d", match_id)
        return None

    async with AsyncSessionLocal() as s:
        match = await s.get(Match, match_id)
        if not match:
            return None
        title = match.title or "the position"
        company = match.company or "your company"
        description = (match.description or "")[:1500]

    lang = _detect_language(description)
    system = _SYSTEM_RU if lang == "ru" else _SYSTEM_EN

    cv = profile.structured_json
    skills = ", ".join(cv.get("skills", [])[:25])
    summary = cv.get("summary", "")
    exp_lines = []
    for exp in cv.get("experiences", [])[:4]:
        bullets = "; ".join(b["text"] for b in exp.get("bullets", [])[:3])
        period = exp.get("period", "")
        exp_lines.append(
            f"{exp.get('title')} @ {exp.get('company')} ({period}): {bullets}"
        )
    cv_snippet = f"Summary: {summary}\nSkills: {skills}\n" + "\n".join(exp_lines)

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": (
            f"Job: {title} at {company}\n"
            f"Description:\n{description}\n\n"
            f"Candidate profile:\n{cv_snippet[:2500]}"
        )},
    ]
    raw = await complete(messages, max_tokens=600, purpose="cover_letter")
    if not raw:
        log.warning("cover_letter: LLM returned None for match %d", match_id)
        return None

    letter = raw.strip()

    async with AsyncSessionLocal() as s:
        m = await s.get(Match, match_id)
        if m:
            m.cover_text = letter
            await s.commit()

    return letter
