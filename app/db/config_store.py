"""
DB-backed config store for sensitive config that must not be in git.
Keys: include_strong, include_weak, exclude_hard, senior_terms, roles
"""
from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import select
from app.db.session import AsyncSessionLocal
from app.db.models import AppConfig

log = logging.getLogger(__name__)

_DEFAULTS: dict[str, Any] = {
    "include_strong": [
        "data scientist", "ml engineer", "machine learning engineer",
        "ai engineer", "mlops", "data engineer", "data analyst",
        "nlp engineer", "computer vision", "ml researcher", "deep learning",
        "дата сайентист", "дата-сайентист", "ml инженер", "ml-инженер",
        "инженер машинного обучения", "машинного обучения",
        "ai инженер", "ии инженер", "инженер данных", "дата инженер",
        "аналитик данных", "нейросет", "компьютерное зрение",
        "обработка естественного языка",
        "деректер ғалымы", "машиналық оқыту", "деректер инженері",
        "деректер талдаушысы", "жасанды интеллект инженері",
    ],
    "include_weak": [
        "python", "pytorch", "tensorflow", "scikit", "sklearn", "pandas",
        "numpy", "spark", "airflow", "kafka", "mlflow", "sql", "bigquery",
        "snowflake", "huggingface", "llm", "transformer",
        "feature engineering", "power bi", "tableau", "аналитик", "data science",
    ],
    "exclude_hard": [
        "java developer", "backend developer", "frontend developer",
        "backend engineer", "frontend engineer", "backend", "frontend",
        "fullstack", "full-stack", "system analyst", "business analyst",
        "qa engineer", "tester", "sdet", "devops engineer",
        "android developer", "ios developer", "php developer",
        "golang developer", "1c developer", "sales manager",
        "hr manager", "hr generalist", "recruiter", "designer", "ui/ux",
        "project manager", "accountant",
        "java разработчик", "java-разработчик", "бэкенд", "бекенд",
        "фронтенд", "фуллстек", "системный аналитик", "бизнес аналитик",
        "бизнес-аналитик", "тестировщик", "qa инженер", "devops инженер",
        "1с разработчик", "1c разработчик", "android разработчик",
        "ios разработчик", "php разработчик", "менеджер по продажам",
        "маркетолог", "дизайнер", "рекрутер", "бухгалтер", "юрист",
        "сату менеджері", "тестілеуші",
    ],
    "senior_terms": [
        "senior", "lead", "head", "principal", "staff", "director",
        "chief", "вед.", "ведущий", "руководитель", "главный", "старший",
        "team lead", "tech lead",
    ],
    "roles": [
        "Data Scientist", "ML Engineer", "AI Engineer",
        "Data Engineer", "Data Analyst", "MLOps Engineer", "NLP Engineer",
    ],
}


async def get(key: str) -> Any:
    try:
        async with AsyncSessionLocal() as s:
            row = await s.scalar(select(AppConfig).where(AppConfig.key == key))
            if row:
                return json.loads(row.value)
    except Exception as e:
        log.warning("config_store.get(%s) failed: %s", key, e)
    return _DEFAULTS.get(key)


async def set(key: str, value: Any) -> None:
    try:
        async with AsyncSessionLocal() as s:
            existing = await s.scalar(select(AppConfig).where(AppConfig.key == key))
            if existing:
                existing.value = json.dumps(value, ensure_ascii=False)
            else:
                s.add(AppConfig(key=key, value=json.dumps(value, ensure_ascii=False)))
            await s.commit()
    except Exception as e:
        log.error("config_store.set(%s) failed: %s", key, e)


async def seed_defaults() -> None:
    for key, value in _DEFAULTS.items():
        try:
            async with AsyncSessionLocal() as s:
                existing = await s.scalar(select(AppConfig).where(AppConfig.key == key))
                if not existing:
                    s.add(AppConfig(key=key, value=json.dumps(value, ensure_ascii=False)))
                    await s.commit()
        except Exception as e:
            log.warning("seed_defaults key=%s: %s", key, e)
    log.info("AppConfig defaults seeded")