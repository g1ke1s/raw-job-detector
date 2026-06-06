from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import select
from app.db.session import AsyncSessionLocal
from app.db.models import CoverTemplate

log = logging.getLogger(__name__)

_ROLE_KEYWORDS = [
    ("mle",    ["ml engineer", "machine learning", "mlops", "ml-engineer",
                 "ml инженер", "инженер машинного обучения", "mlops engineer"]),
    ("ds",     ["data scientist", "дата сайентист", "дата-сайентист"]),
    ("de",     ["data engineer", "инженер данных", "дата инженер"]),
    ("da",     ["data analyst", "аналитик данных"]),
    ("nlp",    ["nlp", "natural language", "обработка естественного"]),
    ("cv_eng", ["computer vision", "компьютерное зрение"]),
]


def match_role_slug(title: str) -> Optional[str]:
    norm = (title or "").lower()
    for slug, keywords in _ROLE_KEYWORDS:
        if any(kw in norm for kw in keywords):
            return slug
    return None


async def get_template(slug: str) -> Optional[str]:
    async with AsyncSessionLocal() as s:
        row = await s.scalar(
            select(CoverTemplate).where(CoverTemplate.role_slug == slug)
        )
        return row.template if row else None


async def get_all_templates() -> list[tuple[str, str]]:
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(select(CoverTemplate))).scalars().all()
        return [(r.role_slug, r.template) for r in rows]


async def save_template(slug: str, template: str) -> None:
    async with AsyncSessionLocal() as s:
        existing = await s.scalar(
            select(CoverTemplate).where(CoverTemplate.role_slug == slug)
        )
        if existing:
            existing.template = template
        else:
            s.add(CoverTemplate(role_slug=slug, template=template))
        await s.commit()


def fill_template(template: str, company: str, role_title: str) -> str:
    return (
        template
        .replace("{company}", company or "вашей компании")
        .replace("{role_title}", role_title or "данной вакансии")
    )