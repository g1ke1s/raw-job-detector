"""
Fingerprint dedup: prevents the same company+role appearing twice
across different sources (e.g. hh.kz and LinkedIn).
"""
import re
import logging
from sqlalchemy import select
from app.db.session import AsyncSessionLocal
from app.db.models import SeenJob

log = logging.getLogger(__name__)

_NOISE = re.compile(r"[^\w\s]", re.UNICODE)
_SPACE = re.compile(r"\s+")

_ROLE_ALIASES = {
    "data scientist": "ds",
    "ml engineer": "mle",
    "machine learning engineer": "mle",
    "ai engineer": "aie",
    "data engineer": "de",
    "data analyst": "da",
    "mlops engineer": "mlops",
    "nlp engineer": "nlp",
}


def _norm(s: str) -> str:
    s = (s or "").lower().strip()
    s = _NOISE.sub(" ", s)
    s = _SPACE.sub(" ", s).strip()
    for alias, short in _ROLE_ALIASES.items():
        s = s.replace(alias, short)
    return s


def make_fingerprint(company: str, title: str) -> str:
    return f"{_norm(company)}::{_norm(title)}"


async def is_duplicate(company: str, title: str, source: str) -> bool:
    fp = make_fingerprint(company, title)
    try:
        async with AsyncSessionLocal() as s:
            row = await s.scalar(select(SeenJob).where(SeenJob.content_fingerprint == fp))
            if row:
                return True
            s.add(SeenJob(content_fingerprint=fp, source=source))
            await s.commit()
            return False
    except Exception as e:
        log.warning("is_duplicate error: %s", e)
        return False
