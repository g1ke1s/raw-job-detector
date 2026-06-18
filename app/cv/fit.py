"""LLM fit assessment for a boosted job match."""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.db.models import Match
from app.llm.client import complete
from app.llm.parsing import safe_json_parse

log = logging.getLogger(__name__)

_SYSTEM = (
    "You are a job-fit advisor. Given a candidate's CV summary and a job description, "
    "assess fit concisely. Return ONLY valid JSON, no prose, no markdown fences.\n"
    'Schema: {"score": 1-10, "fit_summary": str (2-3 sentences), '
    '"blockers": [str, ...], "strengths": [str, ...]}'
)


async def assess_fit(match_id: int) -> Optional[str]:
    """
    Run LLM fit assessment for match_id. Stores result on match.fit_assessment_json.
    Returns formatted plain-text summary for Telegram, or None on failure.
    """
    from app.cv.ingest import get_active_profile

    profile = await get_active_profile()
    if not profile or not profile.structured_json:
        return "No CV on file. Send me your CV as a PDF first."

    async with AsyncSessionLocal() as s:
        match = await s.get(Match, match_id)
        if not match:
            return None
        title = match.title or ""
        company = match.company or ""
        description = (match.description or "")[:1500]

    cv = profile.structured_json
    skills = ", ".join(cv.get("skills", [])[:30])
    summary = cv.get("summary", "")
    exp_lines = []
    for exp in cv.get("experiences", [])[:3]:
        bullets = "; ".join(b["text"] for b in exp.get("bullets", [])[:3])
        exp_lines.append(f"{exp.get('title')} @ {exp.get('company')}: {bullets}")
    cv_snippet = f"Summary: {summary}\nSkills: {skills}\n" + "\n".join(exp_lines)

    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": (
            f"Job: {title} at {company}\n"
            f"Description:\n{description}\n\n"
            f"Candidate CV:\n{cv_snippet[:2000]}"
        )},
    ]
    raw = await complete(messages, max_tokens=700, purpose="fit_assess")
    if not raw:
        return "LLM unavailable for fit assessment."

    parsed = safe_json_parse(raw)
    if not parsed or not isinstance(parsed, dict):
        log.warning("fit_assess bad output: %r", raw[:200])
        return "Fit assessment failed (bad LLM output)."

    async with AsyncSessionLocal() as s:
        match = await s.get(Match, match_id)
        if match:
            match.fit_assessment_json = parsed
            await s.commit()

    score = parsed.get("score", "?")
    fit_summary = parsed.get("fit_summary", "")
    strengths = parsed.get("strengths", [])
    blockers = parsed.get("blockers", [])

    lines = [f"Fit score: {score}/10", f"{fit_summary}"]
    if strengths:
        lines.append("Strengths: " + "; ".join(strengths[:3]))
    if blockers:
        lines.append("Blockers: " + "; ".join(blockers[:3]))
    return "\n".join(lines)
