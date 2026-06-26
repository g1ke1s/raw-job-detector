"""
Rule-based title filter for hh.kz and LinkedIn.
Keywords loaded from DB. Senior-level titles are tagged, not dropped.

Priority rules (Bug 2 fix):
  1. Strong positive signal (AI/ML/Data) ALWAYS wins over exclude_hard terms.
  2. Hard-exclude only fires when there is ZERO positive signal.
  3. Genuinely ambiguous titles (match neither list) get a narrow yes/no LLM call.
"""
from __future__ import annotations

import re
import logging

log = logging.getLogger(__name__)
_NORM_RE = re.compile(r"[^\w\s]", re.UNICODE)

# In-process cache for LLM title decisions — resets on bot restart, avoids repeat calls.
_llm_title_cache: dict[str, bool] = {}
_llm_call_count: int = 0
_MAX_LLM_CALLS: int = 20  # hard cap per process lifetime; logged when hit


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


async def _llm_title_check(title: str) -> bool:
    """
    Narrow LLM fallback — called ONLY when title matches neither include_strong
    nor exclude_hard (genuinely ambiguous).  Sends just the title (~10 tokens in,
    1 token out). Results cached in-process so the same title is never sent twice.
    Hard-capped at _MAX_LLM_CALLS per process lifetime.
    """
    global _llm_call_count
    norm = _norm(title)
    if norm in _llm_title_cache:
        return _llm_title_cache[norm]

    if _llm_call_count >= _MAX_LLM_CALLS:
        log.warning(
            "LLM title-classify cap (%d) reached — treating ambiguous title as irrelevant: %r",
            _MAX_LLM_CALLS, title,
        )
        _llm_title_cache[norm] = False
        return False

    _llm_call_count += 1
    try:
        from app.llm.client import complete
        messages = [{"role": "user", "content": (
            f'Job title: "{title}"\n'
            "Is the PRIMARY role here Data Science, ML, AI engineering, or Data Engineering? "
            "(A secondary skill mention does not count — the main job must be DS/ML/AI/DE.) "
            "Reply ONLY: yes or no."
        )}]
        raw = await complete(messages, max_tokens=5, purpose="title_classify")
        result = bool(raw) and raw.strip().lower().startswith("yes")
        _llm_title_cache[norm] = result
        log.info(
            "LLM title-classify [%d/%d]: %r → %s",
            _llm_call_count, _MAX_LLM_CALLS, title, "pass" if result else "drop",
        )
        return result
    except Exception as e:
        log.warning("LLM title-classify failed for %r: %s", title, e)
        _llm_title_cache[norm] = False
        return False


async def title_seniority_check(title: str) -> str:
    """
    Returns:
      "pass"   — in-field, not senior → enqueue normally
      "senior" — in-field, senior level → tag and store, skip auto-queue
      "drop"   — not in-field → discard

    Priority (corrected from the original has_hard-beats-has_strong bug):
      • has_strong=True  → always relevant; hard terms in the same title are context,
                           not grounds for rejection (logged as [compound_keep] for info).
      • has_strong=False, has_hard=True  → pure irrelevant role, drop silently.
      • has_strong=False, has_hard=False → ambiguous; narrow LLM yes/no decides.
    """
    if not title or len(title.strip()) < 3:
        return "drop"
    norm = _norm(title)
    from app.db.config_store import get as db_get
    strong  = _compile(await db_get("include_strong") or [])
    hard    = _compile(await db_get("exclude_hard") or [])
    senior  = _compile(await db_get("senior_terms") or [])
    has_strong = any(p.search(norm) for _, p in strong)
    has_hard   = any(p.search(norm) for _, p in hard)
    is_senior  = any(p.search(norm) for _, p in senior)

    if has_strong:
        # Strong AI/ML/Data signal is present — this is relevant regardless of other terms.
        if has_hard:
            # Compound/hybrid title (e.g. "AI Engineer / Backend Dev") — log for visibility,
            # but strong wins.  These used to be wrongly dropped as [hard_exclude].
            from app.monitoring.events import log_event
            await log_event("filter_compound", f"[compound_keep] {title}", "INFO")
    else:
        # No strong positive signal.
        if has_hard:
            # Pure irrelevant role (backend, sales, QA, etc.) — drop silently.
            return "drop"
        # Neither list matched — genuinely ambiguous.  Try narrow LLM yes/no.
        if not await _llm_title_check(title):
            from app.monitoring.events import log_event
            await log_event("filter_reject", f"[llm_classify:no] {title}", "INFO")
            return "drop"
        # LLM said relevant — fall through to senior check.

    return "senior" if is_senior else "pass"


async def title_is_relevant_async(title: str) -> bool:
    return await title_seniority_check(title) == "pass"
