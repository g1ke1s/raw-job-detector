"""
Job-title relevance filter for hh.kz and LinkedIn.
Keywords loaded from DB. Senior-level titles are tagged, not dropped.

Relevance decision order:
  1. Pre-filter A — AI-as-context false positive check (fast regex, no model).
  2. Pre-filter B — Non-DS trainee/course false positive check (fast regex).
  3. Semantic classifier (sentence-transformers, local, no API call):
       confident pass  → proceed
       hard block      → drop
       uncertain       → LLM fallback (narrow yes/no, ~5 tokens)
  4. LLM fallback — only for genuinely ambiguous titles.

Seniority detection — regex only (structural, not semantic):
  "Senior X"             → tagged senior
  "Junior–Senior X"      → NOT tagged (junior negates the range)
  "Staff / Principal X"  → tagged senior
"""
from __future__ import annotations

import re
import logging

log = logging.getLogger(__name__)
_NORM_RE = re.compile(r"[^\w\s]", re.UNICODE)

# In-process cache for LLM title decisions — resets on bot restart.
_llm_title_cache: dict[str, bool] = {}
_llm_call_count: int = 0
_MAX_LLM_CALLS: int = 20


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


# ── Context-based false-positive patterns (post-_norm, lower-case) ─────────────

# Bare abbreviations that can appear as tool-context qualifiers.
# Everything ELSE in include_strong is treated as a safe phrase (multi-word or specific).
_CONTEXT_ABBREV = frozenset({"ml", "ai"})

# Non-DS primary-role indicators.  Hyphen → space is already done by _norm, so
# "Front-End" → "front end", ".NET" → "net", etc.
_RE_NON_DS = re.compile(
    r"(?<![a-zа-яё0-9])"
    r"(?:"
    r"frontend|front end"
    r"|android"
    r"|ios"
    r"|mobile"
    r"|net"           # .NET framework (dot stripped by _norm)
    r"|java"
    r"|backend|back end"
    r"|devops"
    r"|php"
    r"|golang"
    r"|swift"
    r"|kotlin"
    r"|1c"
    r"|functional testing"
    r"|software testing"
    r"|qa engineer|qa developer|qa automation"
    r"|sdet"
    r")"
    r"(?![a-zа-яё0-9])",
    re.UNICODE,
)

# AI/ML used as a tool/context qualifier, not the primary role.
_RE_AI_AS_CONTEXT = re.compile(
    r"(?:"
    r"with\s+ai\b"
    r"|using\s+ai\b"
    r"|for\s+ai\b"
    r"|ai\s+tools?"
    r"|ai\s+powered"
    r"|ai\s+assisted"
    r"|generative\s+ai\s+for\b"
    r")"
)

# Training-program / course indicators that pair with a non-DS field → drop.
_RE_COURSE_SIGNAL = re.compile(
    r"(?:"
    r"\btrainee\b"
    r"|\bbootcamp\b"
    r"|\bкурс\b"
    r"|\bобучени"
    r"|\bfor\s+software\s+development\b"
    r")"
)

# Junior signals that negate a senior tag when they co-appear in the same title.
# Handles "Junior–Senior", "Junior/Senior", "all levels", "all grades".
_RE_JUNIOR = re.compile(
    r"\bjunior\b|\bjr\.?\b|\ball\s+levels?\b|\ball\s+grades?\b",
    re.UNICODE,
)

# ──────────────────────────────────────────────────────────────────────────────


async def _llm_title_check(title: str) -> bool:
    """
    Narrow LLM fallback — called ONLY for genuinely ambiguous titles.
    Results cached in-process. Hard-capped at _MAX_LLM_CALLS per process.
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
            "Is the PRIMARY role here Data Science, ML engineering, AI engineering, "
            "Data Engineering, or Data Analysis? "
            "Rules: (1) A secondary or tool mention does NOT count — the main job "
            "duties must be DS/ML/AI/DE/DA. "
            "(2) Training programs, courses, bootcamps, and 'with AI tools' mentions "
            "do NOT qualify. "
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

    Decision order:
      1. Pre-filters A and B (fast regex, no model inference).
      2. Semantic relevance via semantic_filter.is_relevant_role().
      3. LLM fallback for the uncertain zone only.

    Seniority regex is applied only to titles that pass relevance.
    Titles matching "Junior–Senior" / "all levels" ranges are NOT tagged senior.
    """
    if not title or len(title.strip()) < 3:
        return "drop"

    norm = _norm(title)
    from app.db.config_store import get as db_get

    # ── Pre-filters: use has_strong_phrase guard to protect compound DS titles ──
    # ("DevOps/MLOps Engineer" has "mlops" in strong_phrase → guard fires → skip pre-filter)
    all_strong = _compile(await db_get("include_strong") or [])
    strong_phrase = [(t, p) for t, p in all_strong if t not in _CONTEXT_ABBREV]
    has_strong_phrase = any(p.search(norm) for _, p in strong_phrase)
    ai_is_context = bool(_RE_AI_AS_CONTEXT.search(norm))

    # Pre-filter A: non-DS primary role + AI as context qualifier
    if not has_strong_phrase and ai_is_context and _RE_NON_DS.search(norm):
        from app.monitoring.events import log_event
        await log_event("filter_reject", f"[ai_context:drop] {title}", "INFO")
        return "drop"

    # Pre-filter B: non-DS primary role + trainee/course signal
    if not has_strong_phrase and _RE_NON_DS.search(norm) and _RE_COURSE_SIGNAL.search(norm):
        from app.monitoring.events import log_event
        await log_event("filter_reject", f"[nonds_course:drop] {title}", "INFO")
        return "drop"

    # ── Seniority detection (regex, structural — NOT semantic) ─────────────────
    senior = _compile(await db_get("senior_terms") or [])
    has_senior_term = any(p.search(norm) for _, p in senior)
    # "Junior–Senior" / "all levels" range → not a senior-only role
    is_senior = has_senior_term and not _RE_JUNIOR.search(norm)

    # ── Semantic relevance classification ──────────────────────────────────────
    from app.scrapers.semantic_filter import is_relevant_role
    is_relevant, confidence = is_relevant_role(title)

    if is_relevant is True:
        return "senior" if is_senior else "pass"

    if is_relevant is False:
        return "drop"

    # Uncertain zone → LLM fallback
    if not await _llm_title_check(title):
        from app.monitoring.events import log_event
        await log_event("filter_reject", f"[llm_classify:no] {title}", "INFO")
        return "drop"

    return "senior" if is_senior else "pass"


async def title_is_relevant_async(title: str) -> bool:
    return await title_seniority_check(title) == "pass"
