from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from app.scrapers.telegram_filter import Verdict, SegmentResult, prefilter_post
from app.llm.prompts.telegram_tiebreak import build_messages
from app.llm.parsing import safe_json_parse

log = logging.getLogger(__name__)


@dataclass
class ClassifiedVacancy:
    text: str
    role: Optional[str]
    in_field: bool
    confidence: float
    low_conf: bool
    source_verdict: Verdict
    rule_score: float
    reason: str


async def classify_telegram_post(text: str, min_length: int = 20) -> list[ClassifiedVacancy]:
    segments = await prefilter_post(text, min_length=min_length)
    if not segments:
        return []

    out: list[ClassifiedVacancy] = []
    for seg in segments:
        if seg.verdict == Verdict.IN_FIELD:
            out.append(_from_rule(seg))
        elif seg.verdict == Verdict.OUT_OF_FIELD:
            continue
        elif seg.verdict == Verdict.AMBIGUOUS:
            verdict = await _llm_tiebreak(seg.text)
            if verdict is None:
                out.append(ClassifiedVacancy(
                    text=seg.text, role=None, in_field=True,
                    confidence=0.0, low_conf=True,
                    source_verdict=seg.verdict,
                    rule_score=seg.rule_score,
                    reason=f"{seg.reason}|llm_exhausted",
                ))
            elif verdict["in_field"]:
                out.append(ClassifiedVacancy(
                    text=seg.text, role=verdict.get("role"),
                    in_field=True, confidence=verdict["confidence"],
                    low_conf=False, source_verdict=seg.verdict,
                    rule_score=seg.rule_score,
                    reason=f"{seg.reason}|llm_in_field",
                ))
    return out


def _from_rule(seg: SegmentResult) -> ClassifiedVacancy:
    role = seg.matched_strong[0] if seg.matched_strong else None
    conf = min(1.0, 0.6 + 0.1 * len(seg.matched_strong))
    return ClassifiedVacancy(
        text=seg.text, role=role, in_field=True,
        confidence=conf, low_conf=False,
        source_verdict=seg.verdict,
        rule_score=seg.rule_score,
        reason=seg.reason,
    )


async def _llm_tiebreak(segment: str) -> Optional[dict]:
    from app.llm.client import complete
    messages = build_messages(segment)
    raw = await complete(messages=messages, max_tokens=120, purpose="tg_tiebreak")
    if not raw:
        return None
    data = safe_json_parse(raw)
    if not isinstance(data, dict) or "in_field" not in data:
        return None
    return {
        "in_field": bool(data.get("in_field")),
        "role": data.get("role") or None,
        "confidence": float(data.get("confidence") or 0.5),
    }