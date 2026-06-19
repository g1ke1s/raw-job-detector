"""CV ingestion: PDF → structured profile stored in Profile table."""
from __future__ import annotations

import logging
import re
from typing import Optional

from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.db.models import Profile
from app.llm.client import complete
from app.llm.parsing import safe_json_parse

log = logging.getLogger(__name__)

_SYSTEM = (
    "You are a CV parser. Extract the candidate's profile into structured JSON. "
    "Return ONLY valid JSON, no prose, no markdown fences.\n"
    "Schema: {\n"
    '  "name": str,\n'
    '  "email": str|null,\n'
    '  "summary": str,\n'
    '  "skills": [str, ...],\n'
    '  "experiences": [\n'
    '    {"id": "exp1", "company": str, "title": str, "period": str,\n'
    '     "bullets": [{"id": "exp1_b1", "text": str}, ...]}\n'
    "  ],\n"
    '  "education": [{"institution": str, "degree": str, "year": str}]\n'
    "}"
)


def _extract_text(pdf_bytes: bytes) -> str:
    """Try fitz → pdfminer → pypdf in order; fitz handles LaTeX/CM fonts best."""
    # 1. pymupdf (fitz) — best for LaTeX-generated PDFs
    try:
        import fitz  # pymupdf
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        pages = [page.get_text() for page in doc]
        text = "\n".join(pages).strip()
        if text and len(text) > 50:
            log.info("PDF extracted via fitz (%d chars)", len(text))
            return text
    except Exception as e:
        log.warning("fitz extraction failed: %s", e)

    # 2. pdfminer.six — good fallback for complex encodings
    try:
        import io
        from pdfminer.high_level import extract_text as pdfminer_extract
        text = pdfminer_extract(io.BytesIO(pdf_bytes)).strip()
        if text and len(text) > 50:
            log.info("PDF extracted via pdfminer (%d chars)", len(text))
            return text
    except Exception as e:
        log.warning("pdfminer extraction failed: %s", e)

    # 3. pypdf — last resort
    try:
        import io
        import pypdf
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        pages = [page.extract_text() or "" for page in reader.pages]
        text = "\n".join(pages).strip()
        if text and len(text) > 50:
            log.info("PDF extracted via pypdf (%d chars)", len(text))
            return text
    except Exception as e:
        log.warning("pypdf extraction failed: %s", e)

    log.error("All PDF extraction methods failed")
    return ""


def _fix_bullet_ids(structured: dict) -> dict:
    """Ensure stable expN_bM IDs for all experience bullets."""
    for exp_idx, exp in enumerate(structured.get("experiences", []), start=1):
        exp_id = f"exp{exp_idx}"
        exp["id"] = exp_id
        for b_idx, bullet in enumerate(exp.get("bullets", []), start=1):
            bullet["id"] = f"{exp_id}_b{b_idx}"
    return structured


async def ingest_pdf(pdf_bytes: bytes) -> tuple[bool, str]:
    """
    Parse PDF → call LLM → store in Profile table.
    Returns (success, message).
    """
    raw_text = _extract_text(pdf_bytes)
    if not raw_text or len(raw_text) < 50:
        return False, (
            "Could not extract text from PDF.\n"
            "Try: /setcv followed by pasting your CV as plain text."
        )

    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": f"CV text:\n\"\"\"\n{raw_text[:6000]}\n\"\"\""},
    ]
    raw = await complete(messages, max_tokens=2000, purpose="cv_parse")
    if not raw:
        return False, "LLM unavailable. Try again in a minute."

    structured = safe_json_parse(raw)
    if not structured or not isinstance(structured, dict):
        log.warning("cv_parse returned non-dict: %r", raw[:200])
        return False, "CV parsing failed (bad LLM output). Try again."

    structured = _fix_bullet_ids(structured)

    await _store_profile(raw_text, structured)
    name = structured.get("name") or "Unknown"
    exp_count = len(structured.get("experiences", []))
    skill_count = len(structured.get("skills", []))
    return True, (
        f"CV saved for {name}.\n"
        f"{exp_count} experience entries, {skill_count} skills extracted."
    )


async def _store_profile(raw_text: str, structured: dict) -> None:
    async with AsyncSessionLocal() as s:
        current_version = await s.scalar(
            select(Profile.version)
            .where(Profile.is_active == True)
            .order_by(Profile.version.desc())
        )
        new_version = (current_version or 0) + 1

        # Mark all existing profiles inactive
        existing = (await s.execute(
            select(Profile).where(Profile.is_active == True)
        )).scalars().all()
        for p in existing:
            p.is_active = False

        s.add(Profile(
            version=new_version,
            is_active=True,
            raw_cv_text=raw_text,
            structured_json=structured,
        ))
        await s.commit()
        log.info("Stored CV profile v%d", new_version)


async def get_active_profile() -> Optional[Profile]:
    async with AsyncSessionLocal() as s:
        return await s.scalar(
            select(Profile)
            .where(Profile.is_active == True)
            .order_by(Profile.version.desc())
        )
