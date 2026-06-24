"""CV tailoring: constrained rephrase + LaTeX PDF via tectonic."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import tempfile
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
    'Schema: {\n'
    '  "subtitle": str,  // short framing of candidate background for this job (max 5 words),'
    ' reflecting actual experience only — e.g. "AI/ML Engineer" or "Data Engineer"\n'
    '  "summary": str,   // 1-2 sentences tailored to the role, grounded in actual CV only\n'
    '  "tailored_bullets": [{"id": str, "original": str, "rephrased": str}],\n'
    '  "skills_order": [str, ...]  // all skill category names in priority order for this role\n'
    '}'
)

# Canonical skill categories with their content — exact from the user's CV.
# "Languages \\& Tools" → in the LaTeX source this becomes: Languages \& Tools
_SKILLS_ROWS: list[tuple[str, str]] = [
    ("LLM / Agentic",
     "LangGraph, LangChain, RAG, Vector DBs, Transformers, HuggingFace"),
    ("Machine Learning",
     "PyTorch, XGBoost, LightGBM, scikit-learn"),
    ("Data Engineering",
     "Apache Airflow, PostgreSQL, ClickHouse, Docker, Kubernetes, Grafana, Prometheus, S3"),
    ("Languages \\& Tools",
     "Python, Pandas, R, SQL, Git, PowerBI, Tableau"),
    ("Spoken Languages",
     "Kazakh (Native), Russian (Native), English (Fluent), French (Beginner)"),
]

# Normalised name → row index, for LLM-directed reordering
_SKILLS_NORM: dict[str, int] = {
    re.sub(r"[^\w]", "", k.lower()): i for i, (k, _) in enumerate(_SKILLS_ROWS)
}

# ── LaTeX fixed sections ──────────────────────────────────────────────────────

_PREAMBLE = r"""\documentclass[a4paper]{article}
    \usepackage{fullpage}
    \usepackage{amsmath}
    \usepackage{amssymb}
    \usepackage{textcomp}
    \usepackage[utf8]{inputenc}
    \usepackage[T1]{fontenc}
    \usepackage[hidelinks]{hyperref}
    \usepackage[left=2cm, right=2cm, top=2cm]{geometry}
    \usepackage{longtable}
    \textheight=10in
    \pagestyle{empty}
    \raggedright

\def\bull{\vrule height 0.8ex width .7ex depth -.1ex }

\newcommand{\area} [2] {
    \vspace*{-9pt}
    \begin{verse}
        \textbf{#1}   #2
    \end{verse}
}

\newcommand{\lineunder} {
    \vspace*{-8pt} \\
    \hspace*{-18pt} \hrulefill \\
}

\newcommand{\header} [1] {
    {\hspace*{-18pt}\vspace*{6pt} \textsc{#1}}
    \vspace*{-6pt} \lineunder
}

\newcommand{\employer} [3] {
    { \textbf{#1} (#2)\\ \underline{\textbf{\emph{#3}}}\\  }
}

\newcommand{\contact} [3] {
    \vspace*{-10pt}
    \begin{center}
        {\Huge \scshape {#1}}\\
        #2 \\ #3
    \end{center}
    \vspace*{-8pt}
}

\newenvironment{achievements}{
    \begin{list}
        {$\bullet$}{\topsep 0pt \itemsep -2pt}}{\vspace*{4pt}
    \end{list}
}

\newcommand{\schoolwithcourses} [4] {
    \textbf{#1} #2 $\bullet$ #3\\
    #4 \\
    \vspace*{5pt}
}

\newcommand{\school} [4] {
    \textbf{#1} #2 $\bullet$ #3\\
    #4 \\
}
"""

_EDUCATION = r"""
\header{Education}
\vspace{1mm}
\textbf{University of London}\hfill London, UK\\
Double diploma program \hfill October 2023 - May 2026\\
BSc Data Science and Business Analytics \quad {\sl GPA: First-Class Honours}\\
\vspace{1mm}
\textbf{Universit\'{e} de Namur}\hfill Namur, Belgium\\
Erasmus+ Scholarship Grantee  \hfill January 2025 - June 2025\\
Business Analytics \& Big Data (MSc Level)\\
\vspace{1mm}
\textbf{Kazakh-British Technical University}\hfill Almaty, Kazakhstan\\
BSc Information Systems in Business \quad {\sl GPA: 3.6} \hfill September 2022 - May 2026\\
"""

_PROJECTS = r"""
    \header{Projects}
    \vspace{2mm}

    \textbf{Multi-Agent Content Generation} \hfill \textit{(LangGraph, FastAPI, FAISS, Groq/Gemini/Mistral)}
        \begin{itemize} \itemsep -3pt
        \item Built a 9-node agentic pipeline with human-in-the-loop validation.
        \item Designed a web-grounded originality scorer and adversarial thesis-selection step to reduce generic LLM output.
    \end{itemize}
    \vspace*{2mm}
    \textbf{AI Job Application Agent} \hfill \textit{(LLM pipeline, Telegram API)}
        \begin{itemize} \itemsep -3pt
        \item Built a job-search automation tool that scrapes postings from multiple sources, scores them against my resume using an LLM pipeline, and sends matches to a Telegram bot for approval before applying.
        \item Reduced manual job screening by 100+ postings per week by automating sourcing and relevance scoring across providers.
    \end{itemize}
    \textbf{Fiscal Decentralization \& Public Procurement --- Diploma Research} \hfill \textit{(DuckDB, Python, R)}
        \begin{itemize} \itemsep -3pt
        \item Built a DiD and RDD pipeline analyzing 2018 Kazakhstan budget reform's impact on procurement competition, processing millions of tender records.
        \item Found a 20pp decrease in saving rate, 0.4-unit increase in supplier participation, and 2.5-unit increase in contract delay post-reform.
    \end{itemize}
    \vspace*{2mm}
"""

_HONORS = r"""
    \header{Honors \& Certifications}
    \vspace{2mm}

    \textbf{Nazarbayev University Metricon 2.0} | \textbf{2nd Place}  \hfill  2026\\
    \vspace*{1mm}
    \textbf{QazDatathon} | \textbf{2nd Place} \hfill  2025\\
    \textit{\href{https://datathon.stat.gov.kz/}{Link: National Statistics Bureau Datathon}}
    \vspace*{1mm}

    \textbf{Freedom Broker Datathon} | \textbf{2nd Place} \hfill  2025\\
    \vspace*{1mm}
    \textbf{KBTU | Top 5 Highest Scoring Student} \hfill 2022-2026\\
    \textit{\href{https://drive.google.com/file/d/1ftn44rrth9K45v1QPi1GfzIdayFLsrN2/view?usp=sharing}{Academic Ranking: Top 5 out of 70+ students in Data Science specialty}}
    \vspace*{1mm}

    \textbf{University of London | Letter of Commendation} \hfill 2024\\
    \textit{\href{https://drive.google.com/file/d/1W_mWXfVluFiWnDbxodeg4ka8uvzIWbrJ/view?usp=sharing}{Global Recognition for Academic Excellence}}
    \vspace*{1mm}

    \textbf{Astana Hub | Tech Orda Grant Recipient} \hfill 2024\\
    \textit{\href{https://drive.google.com/file/d/1gXoWvcwENNyPoqAOtoap4ome03FpyJ6w/view?usp=sharing}{Awarded competitive government grant for advanced training in Machine Learning Engineering}}
    \vspace*{1mm}

    \textbf{BigData Team | Machine Learning \& Industrial Python Specialization} \hfill 2024\\
    \textit{6-Month Intensive Program: \href{https://bigdatateam.org/certificates?cid=10IC1a8GTtDpszdngcAiu9-V_5JUtKoWs}{Python for Big Data}, \href{https://bigdatateam.org/certificates?cid=13_SiQ1LtZp9HSfr9oehnOJ-XG0TvM7BF}{Effective Python}, \href{https://bigdatateam.org/certificates?cid=1zMe8nLZVjsHZDKkn-VdMSvFtHyVO7VNN}{Practical ML}}
    \begin{itemize} \itemsep -3pt
        \item ML systems engineered for production using XGBoost and LightGBM, implementing industrial standards: TDD with pytest, CI/CD pipelines, and design patterns.
        \item Deployed scalable web services using Flask, monitored via Grafana.
    \end{itemize}
"""

# ── LaTeX escaping ────────────────────────────────────────────────────────────

_LATEX_ESCAPE: dict[str, str] = {
    "\\": r"\textbackslash{}",
    "&":  r"\&",
    "%":  r"\%",
    "$":  r"\$",
    "#":  r"\#",
    "_":  r"\_",
    "^":  r"\^{}",
    "~":  r"\textasciitilde{}",
    "{":  r"\{",
    "}":  r"\}",
}


def _escape(text: str) -> str:
    """Escape LaTeX special characters in dynamically generated text."""
    return "".join(_LATEX_ESCAPE.get(c, c) for c in (text or ""))


# ── Token-level validation (unchanged) ───────────────────────────────────────

def _tokenize(text: str) -> set[str]:
    return set(re.findall(r"[a-zA-Z0-9]+", text.lower()))


def _validate_rephrase(original_tokens: set[str], skills_tokens: set[str], rephrased: str) -> list[str]:
    allowed = original_tokens | skills_tokens
    new_tokens = _tokenize(rephrased) - allowed
    stopwords = {
        "i", "a", "an", "the", "and", "or", "to", "of", "in", "for",
        "with", "on", "at", "by", "as", "is", "was", "are", "were",
        "my", "our", "this", "that", "which", "from", "into", "using",
        "through", "across", "within", "during", "including",
    }
    return [t for t in new_tokens if t not in stopwords and len(t) > 2]


# ── LaTeX document assembly ───────────────────────────────────────────────────

def _experience_section(cv: dict, rephrased_map: dict[str, str]) -> str:
    blocks: list[str] = []
    for exp in cv.get("experiences", []):
        company = _escape(exp.get("company", ""))
        title   = _escape(exp.get("title", ""))
        period  = _escape(exp.get("period", ""))
        block_lines = [
            f"      \\textbf{{{company}}}\\textbf{{ | {title}  }}",
            "      \\vspace{2mm}",
            f"      \\hfill Almaty, Kazakhstan | {period}\\\\",
            "          \\vspace{-3mm}",
            r"\begin{itemize} \itemsep -3pt",
        ]
        for b in exp.get("bullets", []):
            bid  = b.get("id", "")
            text = _escape(rephrased_map.get(bid, b.get("text", "")))
            block_lines.append(f"    \\item {text}")
        block_lines.append(r"\end{itemize}")
        blocks.append("\n".join(block_lines))
    return "\n".join(blocks)


def _skills_section(skills_order_raw: list[str]) -> str:
    if skills_order_raw:
        order: list[int] = []
        seen: set[int] = set()
        for name in skills_order_raw:
            idx = _SKILLS_NORM.get(re.sub(r"[^\w]", "", name.lower()))
            if idx is not None and idx not in seen:
                order.append(idx)
                seen.add(idx)
        for i in range(len(_SKILLS_ROWS)):
            if i not in seen:
                order.append(i)
        rows = [_SKILLS_ROWS[i] for i in order]
    else:
        rows = list(_SKILLS_ROWS)

    lines: list[str] = []
    for i, (cat, content) in enumerate(rows):
        if i > 0:
            lines.append("  \\vspace{1mm}")
        lines.append(f"  {cat}: &")
        lines.append(f"  \\vspace{{1mm}} {content} \\\\")
    return "\n".join(lines)


def _build_latex(cv: dict, tailored: dict, _job_title: str, _company: str) -> str:
    subtitle = _escape(tailored.get("subtitle", "AI/ML Engineer"))
    summary  = _escape(tailored.get("summary", cv.get("summary", "")))

    rephrased_map = {
        tb["id"]: tb["rephrased"]
        for tb in tailored.get("tailored_bullets", [])
        if "id" in tb and "rephrased" in tb
    }

    exp_block    = _experience_section(cv, rephrased_map)
    skills_block = _skills_section(tailored.get("skills_order", []))

    return (
        _PREAMBLE
        + "\n\\begin{document}\n\\vspace*{-40pt}\n\n"

        # Contact header
        + "\\vspace*{-2pt}\n\\begin{center}\n"
        + "  {\\Huge \\scshape Kenessary Garifulla}\\\\\n"
        + "  \\vspace*{2pt}\n"
        + f"  {subtitle}\\\\\n"
        + "  \\vspace*{2pt}\n"
        + ("  \\href{mailto:k_garifulla@kbtu.kz}{k\\_garifulla@kbtu.kz}"
           " \\quad | \\quad \\href{tel:+77056384032}{+7 705 638 4032}"
           " \\quad | \\quad Almaty, Kazakhstan\\\\\n")
        + "  \\vspace*{2pt}\n"
        + ("  \\textbf{\\href{https://github.com/g1ke1s}{GitHub}}"
           " \\quad | \\quad"
           " \\textbf{\\href{https://www.linkedin.com/in/kenessary-garifulla-35295a255/}{LinkedIn}}\\\\\n")
        + "\\end{center}\n\n"

        # Summary
        + "\\header{Summary}\n\\vspace{1mm}\n"
        + summary + "\n\n"

        # Experience
        + "\\header{Experience}\n\\vspace{2mm}\n\n"
        + exp_block + "\n\n"

        # Education (fixed)
        + _EDUCATION + "\n"

        # Skills
        + "\\header{Skills}\n\\vspace{2mm}\n"
        + "  \\begin{longtable}{p{4cm}p{12cm}}\n"
        + skills_block + "\n"
        + "  \\end{longtable}\n\\vspace{1mm}\n"

        # Projects (fixed)
        + _PROJECTS + "\n"

        # Honors (fixed)
        + _HONORS + "\n"

        + "\\end{document}\n"
    )


# ── Compilation ───────────────────────────────────────────────────────────────

def _compile_latex_sync(tex_source: str) -> bytes:
    with tempfile.TemporaryDirectory() as tmpdir:
        tex_path = os.path.join(tmpdir, "cv.tex")
        pdf_path = os.path.join(tmpdir, "cv.pdf")
        with open(tex_path, "w", encoding="utf-8") as f:
            f.write(tex_source)
        result = subprocess.run(
            ["tectonic", tex_path],
            capture_output=True, text=True, timeout=90, cwd=tmpdir,
        )
        if result.returncode != 0:
            stderr = (result.stderr or "")[:800]
            log.error("tectonic failed:\n%s", stderr)
            raise RuntimeError(f"LaTeX compilation failed (exit {result.returncode}):\n{stderr[:300]}")
        if not os.path.exists(pdf_path):
            raise RuntimeError("tectonic ran but no PDF was produced")
        with open(pdf_path, "rb") as f:
            return f.read()


async def _compile_latex(tex_source: str) -> bytes:
    return await asyncio.to_thread(_compile_latex_sync, tex_source)


# ── Public API ────────────────────────────────────────────────────────────────

async def tailor_and_render(match_id: int) -> tuple[Optional[bytes], str]:
    """
    Tailor CV for match_id and render to PDF via LaTeX.
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
        title       = match.title or ""
        company     = match.company or ""
        description = (match.description or "")[:1500]

    cv           = profile.structured_json
    skills       = cv.get("skills", [])
    skills_tokens = _tokenize(" ".join(skills))

    all_bullets: list[dict] = []
    original_tokens: set[str] = set()
    for exp in cv.get("experiences", []):
        for b in exp.get("bullets", []):
            all_bullets.append(b)
            original_tokens |= _tokenize(b["text"])
    original_tokens |= _tokenize(cv.get("summary", ""))

    bullets_snippet = "\n".join(
        f'  {b["id"]}: {b["text"]}' for b in all_bullets[:20]
    )
    skill_category_names = ", ".join(cat for cat, _ in _SKILLS_ROWS)

    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": (
            f"Job: {title} at {company}\n"
            f"Description:\n{description}\n\n"
            f"Candidate skills: {', '.join(skills[:30])}\n\n"
            f"Skill categories (for skills_order field): {skill_category_names}\n\n"
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

    # Validation pass — unchanged from ReportLab version
    violations: list[str] = []
    for tb in parsed.get("tailored_bullets", []):
        bad = _validate_rephrase(original_tokens, skills_tokens, tb.get("rephrased", ""))
        if bad:
            violations.extend(bad)

    if violations:
        log.warning("CV tailor validation: new tokens introduced: %s", violations[:10])

    audit = {
        "match_id":             match_id,
        "tailored_bullets":     parsed.get("tailored_bullets", []),
        "summary":              parsed.get("summary", ""),
        "subtitle":             parsed.get("subtitle", ""),
        "validation_violations": violations,
    }
    async with AsyncSessionLocal() as s:
        m = await s.get(Match, match_id)
        if m:
            m.cv_tailor_json = audit
            await s.commit()

    # Build LaTeX source and compile
    try:
        tex_source = _build_latex(cv, parsed, title, company)
        pdf_bytes  = await _compile_latex(tex_source)
    except Exception as e:
        log.error("LaTeX render failed for match %d: %s", match_id, e)
        return None, f"PDF generation failed: {e}"

    status = "Tailored CV generated."
    if violations:
        status += f" (Warning: {len(violations)} new tokens introduced — review carefully)"
    return pdf_bytes, status
