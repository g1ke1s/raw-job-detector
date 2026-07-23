"""
Local semantic relevance classifier using sentence-transformers.

Loaded lazily on first call; anchor embeddings pre-computed once and cached.
Falls back to (None, 0.0) — uncertain / LLM decides — if model cannot load.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

log = logging.getLogger(__name__)

# ── Anchor phrases ─────────────────────────────────────────────────────────────

RELEVANT_ANCHORS = [
    "machine learning engineer",
    "ML engineer building models",
    "data scientist analyzing data",
    "data engineer building pipelines",
    "AI engineer working with neural networks",
    "MLOps engineer deploying models",
    "NLP engineer working with language models",
    "computer vision engineer",
    "deep learning researcher",
    "AI researcher diffusion models",
    "applied scientist machine learning",
    "BI analyst business intelligence",
    "analytics engineer dbt sql",
    "data analyst sql python",
    "LLM engineer language models",
    "generative AI engineer",
    "foundation model researcher",
    "аналитик данных",
    "инженер машинного обучения",
    "дата инженер",
    "ML разработчик",
]

IRRELEVANT_ANCHORS = [
    "frontend developer javascript react",
    "android mobile developer",
    "iOS developer swift",
    "sales manager revenue",
    "HR recruiter hiring",
    "QA engineer quality assurance testing automation",
    "QA tester software testing",
    "accountant finance",
    "backend developer java spring",
    "graphic designer UI",
    "project manager agile",
    "NET developer C sharp",
]

# ── Module-level singletons ────────────────────────────────────────────────────

_model = None
_rel_embs = None
_irrel_embs = None
_load_attempted = False
_load_failed = False


def _load() -> None:
    """Load model and pre-compute anchor embeddings (called once on first use)."""
    global _model, _rel_embs, _irrel_embs, _load_attempted, _load_failed
    if _load_attempted:
        return
    _load_attempted = True
    try:
        t0 = time.time()
        from sentence_transformers import SentenceTransformer  # noqa: PLC0415

        model = SentenceTransformer("paraphrase-multilingual-MiniLM-L12-v2")
        _rel_embs = model.encode(
            RELEVANT_ANCHORS, normalize_embeddings=True, show_progress_bar=False
        )
        _irrel_embs = model.encode(
            IRRELEVANT_ANCHORS, normalize_embeddings=True, show_progress_bar=False
        )
        _model = model

        elapsed = time.time() - t0
        try:
            import os
            import psutil
            mem = psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024
            log.info(
                "semantic_filter: model loaded in %.1fs, process RSS %.0f MB",
                elapsed, mem,
            )
        except Exception:
            log.info("semantic_filter: model loaded in %.1fs", elapsed)

    except Exception as exc:
        _load_failed = True
        log.warning(
            "semantic_filter: model load failed (%s) — will fall back to LLM/regex",
            exc,
        )


# ── Public API ─────────────────────────────────────────────────────────────────

def is_relevant_role(
    title: str,
    threshold: float = 0.35,
) -> tuple[Optional[bool], float]:
    """
    Classify a job title against DS/ML/AI anchors.

    Returns (is_relevant, confidence):
      True   → confident pass; skip LLM
      False  → hard block; skip LLM
      None   → uncertain zone; caller should use LLM fallback

    confidence = max_relevant_score − max_irrelevant_score (can be negative)
    """
    _load()
    if _model is None:
        # Model unavailable — caller falls back to LLM/regex
        return None, 0.0

    title_emb = _model.encode(title, normalize_embeddings=True, show_progress_bar=False)

    # Cosine similarity = dot product for L2-normalised vectors
    rel_scores = _rel_embs @ title_emb
    irrel_scores = _irrel_embs @ title_emb

    max_rel = float(rel_scores.max())
    max_irrel = float(irrel_scores.max())
    confidence = max_rel - max_irrel

    # Hard block: clearly closer to irrelevant anchors
    if max_irrel > max_rel and max_irrel > threshold:
        return False, confidence

    # Confident pass: above threshold AND clearly more similar to relevant
    if max_rel >= threshold and confidence > 0.25:
        return True, confidence

    # Uncertain: LLM fallback
    return None, confidence


def debug_scores(title: str) -> dict:
    """Return detailed scores per anchor for debugging / threshold tuning."""
    _load()
    if _model is None:
        return {"error": "model not loaded"}

    title_emb = _model.encode(title, normalize_embeddings=True, show_progress_bar=False)
    rel_scores = _rel_embs @ title_emb
    irrel_scores = _irrel_embs @ title_emb
    return {
        "title": title,
        "relevant": sorted(
            zip(RELEVANT_ANCHORS, map(float, rel_scores)), key=lambda x: -x[1]
        )[:5],
        "irrelevant": sorted(
            zip(IRRELEVANT_ANCHORS, map(float, irrel_scores)), key=lambda x: -x[1]
        )[:5],
        "max_rel": float(rel_scores.max()),
        "max_irrel": float(irrel_scores.max()),
        "confidence": float(rel_scores.max()) - float(irrel_scores.max()),
    }
