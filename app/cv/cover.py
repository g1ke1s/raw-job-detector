"""LLM cover letter generation — fires only on Boost, never during scraping."""
from __future__ import annotations

import logging
import re
from typing import Optional

from app.db.session import AsyncSessionLocal
from app.db.models import Match
from app.llm.client import complete

log = logging.getLogger(__name__)

_RU_RE = re.compile(r"[а-яёА-ЯЁ]")

_SYSTEM_EN = """\
You are helping a Data Science / ML / AI engineer apply for jobs in Kazakhstan.

STEP 1 — scan the job description for explicit instructions from the employer:
• Did they ask for salary expectations? → write "open to discussion" naturally into the letter.
• Did they ask a specific question? → answer it directly.
• Did they specify a stack that matches the candidate's real experience? → mention it.
• Did they require a work sample, portfolio, or anything else explicit? → address it briefly.
If nothing explicit found, skip to Step 2 directly.

STEP 2 — write the cover letter:
• 3 paragraphs max, 2-4 sentences each.
• Do NOT open with "I am writing to express my interest..." or any variant of it.
• Start the very first sentence with a specific fact from the candidate's REAL experience that is directly relevant to this job — a project, a tool, a metric, a company.
• Mention 1-2 concrete details: real company names from the CV, real numbers (R², accuracy, latency, throughput), real tools used.
• Human tone — write the way a real engineer talks, not a corporate memo.
• Zero buzzwords: no "leveraging", "passionate about", "unique blend", "dynamic environment", "excited to contribute", "results-driven".
• Final paragraph: one sentence only.
• Output ONLY the letter body — no subject line, no greeting, no signature, no markdown formatting.

BAD: "I've worked with a range of technologies that align with the AI Engineer role, including PyTorch and HuggingFace, which I believe could be valuable in developing and deploying AI models. My experience as a Data Engineer has given me a strong foundation in data pipelines."

GOOD: "At AlemAgro built ETL pipelines on Airflow handling 5+ platforms under load — looks similar to what you're describing here. At Kaz Minerals built predictive models with XGBoost, R² 89% on real sensor data.

Happy to discuss further."\
"""

_SYSTEM_RU = """\
Ты помогаешь Data Science / ML / AI инженеру подавать заявки на работу в Казахстане.

ШАГ 1 — прочитай описание вакансии и выдели явные инструкции от работодателя:
• Просят указать ожидания по зарплате? → напиши «открыт к обсуждению» естественно в письме.
• Задают конкретный вопрос? → ответь на него прямо.
• Требуют конкретный стек, который есть в реальном опыте кандидата? → упомяни его.
• Просят портфолио, примеры работ или что-то ещё явное? → коротко адресуй это.
Если явных инструкций нет — переходи сразу к Шагу 2.

ШАГ 2 — напиши сопроводительное письмо:
• Максимум 3 абзаца, 2-4 предложения каждый.
• НЕ начинай с «Я хочу выразить интерес к вакансии...» или любого похожего вступления.
• Первое предложение — конкретный факт из РЕАЛЬНОГО опыта кандидата, который напрямую подходит под эту роль: проект, инструмент, метрика, компания.
• Упомяни 1-2 конкретные детали: реальные названия компаний из CV, реальные числа (R², точность, задержка, пропускная способность), реальные инструменты.
• Человеческий тон — пиши так, как говорит настоящий инженер, не как корпоративный документ.
• Никаких клише: не «используя синергии», не «я увлечён», не «динамичная среда», не «уникальный набор навыков», не «рад внести вклад».
• Последний абзац: только одно предложение.
• Выводи ТОЛЬКО текст письма — без темы, без приветствия, без подписи, без markdown-форматирования.

ПЛОХО: «Я работал с рядом технологий, включая PyTorch и HuggingFace, которые, я считаю, могут быть ценны для разработки AI моделей. Мой опыт в качестве Data Engineer дал мне прочную основу в области конвейеров данных.»

ХОРОШО: «В AlemAgro строил ETL-пайплайны на Airflow под нагрузку 5+ платформ — судя по описанию, у вас похожая задача. В Kaz Minerals разрабатывал predictive модели на XGBoost с R² 89% на реальных сенсорных данных.

Буду рад обсудить подробнее.»\
"""


def _detect_language(text: str) -> str:
    ru_chars = len(_RU_RE.findall(text[:600]))
    return "ru" if ru_chars > 25 else "en"


async def generate_cover(match_id: int) -> Optional[str]:
    """
    Generate a grounded cover letter for match_id using the active CV profile.
    Language is detected from the job description.
    Returns the letter text, or None if profile missing / LLM unavailable.
    Stores result in match.cover_text.
    """
    from app.cv.ingest import get_active_profile

    profile = await get_active_profile()
    if not profile or not profile.structured_json:
        log.info("cover_letter: no active profile for match %d", match_id)
        return None

    async with AsyncSessionLocal() as s:
        match = await s.get(Match, match_id)
        if not match:
            return None
        title = match.title or "the position"
        company = match.company or "your company"
        description = (match.description or "")[:1500]

    lang = _detect_language(description)
    system = _SYSTEM_RU if lang == "ru" else _SYSTEM_EN

    cv = profile.structured_json
    skills = ", ".join(cv.get("skills", [])[:25])
    summary = cv.get("summary", "")
    exp_lines = []
    for exp in cv.get("experiences", [])[:4]:
        bullets = "; ".join(b["text"] for b in exp.get("bullets", [])[:3])
        period = exp.get("period", "")
        exp_lines.append(
            f"{exp.get('title')} @ {exp.get('company')} ({period}): {bullets}"
        )
    cv_snippet = f"Summary: {summary}\nSkills: {skills}\n" + "\n".join(exp_lines)

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": (
            f"Job: {title} at {company}\n"
            f"Description:\n{description}\n\n"
            f"Candidate profile:\n{cv_snippet[:2500]}"
        )},
    ]
    raw = await complete(messages, max_tokens=600, purpose="cover_letter")
    if not raw:
        log.warning("cover_letter: LLM returned None for match %d", match_id)
        return None

    letter = raw.strip()

    async with AsyncSessionLocal() as s:
        m = await s.get(Match, match_id)
        if m:
            m.cover_text = letter
            await s.commit()

    return letter
