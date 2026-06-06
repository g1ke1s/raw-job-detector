"""Stage 3 tiebreaker — used ONLY for AMBIGUOUS segments from telegram_filter."""

SYSTEM = (
    "You filter job posts for a Data Science / ML / AI candidate. "
    "Posts may be in Russian, Kazakh, or English. "
    "IN-FIELD roles: Data Scientist, ML Engineer, AI Engineer, Data Engineer, "
    "Data Analyst, MLOps, NLP, Computer Vision, ML Researcher. "
    "OUT-OF-FIELD: backend, frontend, Java, Go, PHP, system/business analyst, "
    "QA/tester, DevOps-only, SRE, mobile, 1C, sales, HR, design, PM. "
    "A post may list overlapping skills — decide by the PRIMARY role. "
    "Output strict JSON only, no prose, no markdown."
)

USER_TEMPLATE = (
    'Job post segment:\n"""\n{segment}\n"""\n\n'
    "Return JSON exactly: "
    '{{"in_field": true|false, "role": "<role or null>", "confidence": 0.0-1.0}}'
)


def build_messages(segment: str, max_chars: int = 1200) -> list[dict]:
    seg = (segment or "").strip()[:max_chars]
    return [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": USER_TEMPLATE.format(segment=seg)},
    ]
