"""
Rule-based title filter for hh.kz and LinkedIn.
Keywords loaded from DB. Senior/lead titles are dropped.
"""
from __future__ import annotations

import re
import logging

log = logging.getLogger(__name__)
_NORM_RE = re.compile(r"[^\w\s]", re.UNICODE)


def _norm(s: str) -> str:
    s = (s or "").lower().replace("ё", "е")
    return _NORM_RE.sub(" ", s).strip()


def _compile(terms: list[str]):
    out = []
    for t in terms:
        tn = _norm(t)
        if tn:
            pat = re.compile(
                r"(?<![a-zа-яё0-9_])" + re.escape(tn) + r"(?![a-zа-яё0-9_])"
            )
            out.append((tn, pat))
    return out


async def title_is_relevant_async(title: str) -> bool:
    if not title or len(title.strip()) < 3:
        return False
    norm = _norm(title)
    from app.db.config_store import get as db_get
    strong = _compile(await db_get("include_strong") or [])
    hard = _compile(await db_get("exclude_hard") or [])
    senior = _compile(await db_get("senior_terms") or [])
    has_strong = any(p.search(norm) for _, p in strong)
    has_hard = any(p.search(norm) for _, p in hard)
    is_senior = any(p.search(norm) for _, p in senior)
    return has_strong and not has_hard and not is_senior


def title_is_relevant(title: str) -> bool:
    """Sync fallback using hardcoded defaults."""
    if not title or len(title.strip()) < 3:
        return False
    from app.scrapers._tg_defaults import DEFAULTS
    norm = _norm(title)
    strong = _compile(DEFAULTS["include_strong"])
    hard = _compile(DEFAULTS["exclude_hard"])
    has_strong = any(p.search(norm) for _, p in strong)
    has_hard = any(p.search(norm) for _, p in hard)
    return has_strong and not has_hard