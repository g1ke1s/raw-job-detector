"""
Rule-based Telegram post filter — Stages 0-2 (ZERO LLM).
Keywords from DB. Senior segments dropped independently per segment.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Verdict(str, Enum):
    IN_FIELD = "in_field"
    OUT_OF_FIELD = "out_of_field"
    AMBIGUOUS = "ambiguous"
    NOT_A_JOB = "not_a_job"


@dataclass
class SegmentResult:
    text: str
    verdict: Verdict = Verdict.OUT_OF_FIELD
    rule_score: float = 0.0
    matched_strong: list[str] = field(default_factory=list)
    matched_weak: list[str] = field(default_factory=list)
    matched_hard: list[str] = field(default_factory=list)
    is_senior: bool = False
    reason: str = ""


_JOB_SIGNALS = re.compile(
    r"(ваканс|hiring|we are looking|we're looking|ищем|требуется|"
    r"открыта позиц|открыт набор|в команду|разыскива|"
    r"job opening|join (our|the) team|жұмыс|іздейміз|керек|"
    r"зарплат|salary|з/п|з\.п|оклад|вилка|"
    r"\bот\s+\d{2,}\b|\$\s?\d|₸|тенге|тг\b|"
    r"отклик|резюме|apply|cv\b|@[a-z0-9_]{4,})",
    re.IGNORECASE,
)
_AD_SIGNALS = re.compile(
    r"(куплю|продам|курс\b|курсы\b|вебинар|обучени|интенсив|марафон|"
    r"скидк|промокод|розыгрыш|реклам|#ad\b|sponsored|партнерск|"
    r"подпишись|подписывайтесь|переслано из|forwarded from|"
    r"набор на курс|запишись|бесплатный урок|free webinar|"
    r"оплати|рассрочк|тариф)",
    re.IGNORECASE,
)
_STRONG_JOB_OVERRIDE = re.compile(
    r"(ваканс|мы ищем|we are hiring|open position|"
    r"обязанност|требовани к кандидат|условия работы)",
    re.IGNORECASE,
)


def _normalize(text: str) -> str:
    return (text or "").replace("ё", "е").replace("Ё", "Е").lower()


def is_job_post(text: str, min_length: int = 20) -> tuple[bool, str]:
    raw = text or ""
    if len(raw.strip()) < min_length:
        if not _JOB_SIGNALS.search(raw):
            return False, "too_short_no_signal"
    norm = _normalize(raw)
    has_job = bool(_JOB_SIGNALS.search(norm))
    has_ad = bool(_AD_SIGNALS.search(norm))
    has_override = bool(_STRONG_JOB_OVERRIDE.search(norm))
    if has_ad and not has_override:
        return False, "ad_or_course"
    if not has_job:
        return False, "no_job_signal"
    return True, "ok"


_BULLET = re.compile(r"^\s*([•▪◦‣·\-–—*]|\d+[.)]|[🔹🔸🟢🟡📌💼🎯])\s+", re.MULTILINE)
_VAC_HEADER = re.compile(
    r"^\s*(ваканси[яи]\s*№?\s*\d*|позици[яи]\s*№?\s*\d*|vacancy\s*\d*|"
    r"position\s*\d*)\s*[:.\-—]?",
    re.IGNORECASE | re.MULTILINE,
)
_ROLE_LINE = re.compile(
    r"^\s*(?:[•▪◦‣·\-–—*🔹🔸🟢🟡📌💼🎯]\s*)?"
    r"(data\s|ml[\s-]?engineer|ai\s|mlops|nlp|backend|frontend|java|"
    r"python\s+(dev|разр)|"
    r"дата[\s-]|аналитик|инженер|разработчик|developer|engineer|analyst|"
    r"scientist|деректер)",
    re.IGNORECASE | re.MULTILINE,
)


def split_vacancies(text: str) -> list[str]:
    raw = (text or "").strip()
    if not raw:
        return []
    headers = list(_VAC_HEADER.finditer(raw))
    if len(headers) >= 2:
        return _split_at_positions(raw, [m.start() for m in headers])
    bullets = list(_BULLET.finditer(raw))
    if len(bullets) >= 2:
        segs = _split_at_positions(raw, [m.start() for m in bullets])
        roley = [s for s in segs if _ROLE_LINE.search(_normalize(s))]
        if len(roley) >= 2:
            return segs
    role_lines = list(_ROLE_LINE.finditer(raw))
    if len(role_lines) >= 2:
        positions = [m.start() for m in role_lines]
        if positions[-1] - positions[0] > 120:
            return _split_at_positions(raw, positions)
    return [raw]


def _split_at_positions(text: str, positions: list[int]) -> list[str]:
    positions = sorted(set(positions))
    if not positions or positions[0] != 0:
        positions = [0] + positions
    segments = []
    for i, start in enumerate(positions):
        end = positions[i + 1] if i + 1 < len(positions) else len(text)
        seg = text[start:end].strip()
        if seg:
            segments.append(seg)
    return segments or [text.strip()]


def _compile_terms(terms: list[str]) -> list[tuple[str, re.Pattern]]:
    compiled = []
    for t in terms:
        t_norm = _normalize(t).strip()
        if not t_norm:
            continue
        pat = re.compile(
            r"(?<![a-zа-яё0-9_])" + re.escape(t_norm) + r"(?![a-zа-яё0-9_])",
            re.IGNORECASE,
        )
        compiled.append((t_norm, pat))
    return compiled


async def _load_tiers() -> dict:
    from app.db.config_store import get as db_get
    from app.runtime_config import rc
    cfg = rc.get("telegram_detection") or {}
    return {
        "include_strong": await db_get("include_strong") or [],
        "include_weak": await db_get("include_weak") or [],
        "exclude_hard": await db_get("exclude_hard") or [],
        "senior_terms": await db_get("senior_terms") or [],
        "strong_w": float(cfg.get("strong_weight", 1.0)),
        "weak_w": float(cfg.get("weak_weight", 0.4)),
        "hard_w": float(cfg.get("hard_weight", 1.0)),
    }


async def score_segment(segment: str, tiers: Optional[dict] = None) -> SegmentResult:
    if tiers is None:
        tiers = await _load_tiers()
    norm = _normalize(segment)

    strong = [t for t, p in _compile_terms(tiers["include_strong"]) if p.search(norm)]
    weak = [t for t, p in _compile_terms(tiers["include_weak"]) if p.search(norm)]
    hard = [t for t, p in _compile_terms(tiers["exclude_hard"]) if p.search(norm)]
    senior_hits = [t for t, p in _compile_terms(tiers["senior_terms"]) if p.search(norm)]
    is_senior = len(senior_hits) > 0

    score = (
        len(strong) * tiers["strong_w"]
        + len(weak) * tiers["weak_w"]
        - len(hard) * tiers["hard_w"]
    )

    base = SegmentResult(
        text=segment, rule_score=round(score, 3),
        matched_strong=strong, matched_weak=weak, matched_hard=hard,
        is_senior=is_senior,
    )

    if is_senior:
        base.verdict = Verdict.OUT_OF_FIELD
        base.reason = f"senior:{senior_hits[:2]}"
        return base

    if hard and not strong:
        base.verdict = Verdict.OUT_OF_FIELD
        base.reason = f"hard_exclude:{hard[:3]}"
    elif strong and not hard:
        base.verdict = Verdict.IN_FIELD
        base.reason = f"strong:{strong[:3]}"
    elif strong and hard:
        base.verdict = Verdict.AMBIGUOUS
        base.reason = f"collision:{strong[:2]}|{hard[:2]}"
    elif weak and not strong and not hard:
        base.verdict = Verdict.AMBIGUOUS
        base.reason = f"weak_only:{weak[:3]}"
    else:
        base.verdict = Verdict.OUT_OF_FIELD
        base.reason = "no_signal"
    return base


async def prefilter_post(text: str, min_length: int = 20) -> list[SegmentResult]:
    ok, _ = is_job_post(text, min_length=min_length)
    if not ok:
        return []
    tiers = await _load_tiers()
    results = []
    for seg in split_vacancies(text):
        results.append(await score_segment(seg, tiers))
    return results