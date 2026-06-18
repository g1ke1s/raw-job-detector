"""CV tailoring: constrained rephrase + ReportLab PDF generation."""
from __future__ import annotations

import logging
import re
from typing import Optional

from app.db.session import AsyncSessionLocal
from app.db.models import Match
from app.llm.client import complete
from app.llm.parsing import safe_json_parse

log = logging.getLogger(__name__)

_SYSTEM = (
    "You are a CV tailoring assistant. Rephrase ONLY the experience bullets that are "
    "most relevant to the given job. You MUST NOT introduce any new claims, skills, "
    "technologies, or information not present in the original bullets or skills list. "
    "You may rephrase for emphasis and clarity. "
    "Return ONLY valid JSON, no prose, no markdown fences.\n"
    'Schema: {"tailored_bullets": [{"id": str, "original": str, "rephrased": str}], '
    '"summary": str (1-2 sentences tailored to the role)}'
)


def _tokenize(text: str) -> set[str]:
    return set(re.findall(r"[a-zA-Z0-9]+", text.lower()))


def _validate_rephrase(original_tokens: set[str], skills_tokens: set[str], rephrased: str) -> list[str]:
    """Return list of tokens in rephrased that are not in original or skills."""
    allowed = original_tokens | skills_tokens
    new_tokens = _tokenize(rephrased) - allowed
    # Allow common filler words
    stopwords = {
        "i", "a", "an", "the", "and", "or", "to", "of", "in", "for",
        "with", "on", "at", "by", "as", "is", "was", "are", "were",
        "my", "our", "this", "that", "which", "from", "into", "using",
        "through", "across", "within", "during", "including",
    }
    return [t for t in new_tokens if t not in stopwords and len(t) > 2]


async def tailor_and_render(match_id: int) -> tuple[Optional[bytes], str]:
    """
    Tailor CV for match_id and render to PDF.
    Returns (pdf_bytes, status_message).
    """
    from app.cv.ingest import get_active_profile

    profile = await get_active_profile()
    if not profile or not profile.structured_json:
        return None, "No CV on file. Upload your CV as a PDF first."

    async with AsyncSessionLocal() as s:
        match = await s.get(Match, match_id)
        if not match:
            return None, "Match not found."
        title = match.title or ""
        company = match.company or ""
        description = (match.description or "")[:1500]

    cv = profile.structured_json
    skills = cv.get("skills", [])
    skills_tokens = _tokenize(" ".join(skills))

    all_bullets = []
    original_tokens = set()
    for exp in cv.get("experiences", []):
        for b in exp.get("bullets", []):
            all_bullets.append(b)
            original_tokens |= _tokenize(b["text"])
    original_tokens |= _tokenize(cv.get("summary", ""))

    bullets_snippet = "\n".join(
        f'  {b["id"]}: {b["text"]}' for b in all_bullets[:20]
    )
    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": (
            f"Job: {title} at {company}\n"
            f"Description:\n{description}\n\n"
            f"Candidate skills: {', '.join(skills[:30])}\n\n"
            f"Experience bullets:\n{bullets_snippet}"
        )},
    ]
    raw = await complete(messages, max_tokens=2500, purpose="cv_tailor")
    if not raw:
        return None, "LLM unavailable for CV tailoring."

    parsed = safe_json_parse(raw)
    if not parsed or not isinstance(parsed, dict):
        log.warning("cv_tailor bad output: %r", raw[:200])
        return None, "Tailoring failed (bad LLM output)."

    # Validation pass
    violations: list[str] = []
    for tb in parsed.get("tailored_bullets", []):
        bad = _validate_rephrase(original_tokens, skills_tokens, tb.get("rephrased", ""))
        if bad:
            violations.extend(bad)

    audit = {
        "match_id": match_id,
        "tailored_bullets": parsed.get("tailored_bullets", []),
        "summary": parsed.get("summary", ""),
        "validation_violations": violations,
    }

    async with AsyncSessionLocal() as s:
        m = await s.get(Match, match_id)
        if m:
            m.cv_tailor_json = audit
            await s.commit()

    if violations:
        log.warning("CV tailor validation: new tokens introduced: %s", violations[:10])

    try:
        pdf_bytes = _render_pdf(cv, parsed, title, company)
    except Exception as e:
        log.error("PDF render failed: %s", e)
        return None, f"PDF generation failed: {e}"

    status = "Tailored CV generated."
    if violations:
        status += f" (Warning: {len(violations)} new tokens introduced — review carefully)"
    return pdf_bytes, status


def _render_pdf(cv: dict, tailored: dict, job_title: str, company: str) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib import colors
    import io

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        topMargin=2*cm, bottomMargin=2*cm,
        leftMargin=2.5*cm, rightMargin=2.5*cm,
    )

    styles = getSampleStyleSheet()
    name_style = ParagraphStyle("Name", fontSize=18, fontName="Helvetica-Bold", spaceAfter=4)
    section_style = ParagraphStyle("Section", fontSize=12, fontName="Helvetica-Bold",
                                   spaceBefore=12, spaceAfter=4, textColor=colors.HexColor("#1a1a1a"))
    body_style = ParagraphStyle("Body", fontSize=10, fontName="Helvetica",
                                leading=14, spaceAfter=2)
    bullet_style = ParagraphStyle("Bullet", fontSize=10, fontName="Helvetica",
                                  leading=14, leftIndent=12, spaceAfter=2)

    # Build a lookup from bullet id → rephrased text
    rephrased_map = {
        tb["id"]: tb["rephrased"]
        for tb in tailored.get("tailored_bullets", [])
        if "id" in tb and "rephrased" in tb
    }
    tailored_summary = tailored.get("summary", "")

    story = []

    # Name + contact
    name = cv.get("name", "Candidate")
    story.append(Paragraph(name, name_style))
    email = cv.get("email", "")
    if email:
        story.append(Paragraph(email, body_style))
    story.append(Spacer(1, 0.3*cm))

    # Tailored summary
    summary_text = tailored_summary or cv.get("summary", "")
    if summary_text:
        story.append(Paragraph("Summary", section_style))
        story.append(Paragraph(summary_text, body_style))

    # Skills
    skills = cv.get("skills", [])
    if skills:
        story.append(Paragraph("Skills", section_style))
        story.append(Paragraph(", ".join(skills), body_style))

    # Experience
    if cv.get("experiences"):
        story.append(Paragraph("Experience", section_style))
        for exp in cv["experiences"]:
            header = f"<b>{exp.get('title', '')}</b> — {exp.get('company', '')}  <i>{exp.get('period', '')}</i>"
            story.append(Paragraph(header, body_style))
            for b in exp.get("bullets", []):
                bid = b.get("id", "")
                text = rephrased_map.get(bid, b.get("text", ""))
                story.append(Paragraph(f"• {text}", bullet_style))

    # Education
    if cv.get("education"):
        story.append(Paragraph("Education", section_style))
        for edu in cv["education"]:
            line = f"{edu.get('degree', '')} — {edu.get('institution', '')}  {edu.get('year', '')}"
            story.append(Paragraph(line, body_style))

    doc.build(story)
    return buf.getvalue()
