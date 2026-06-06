"""Fallback keyword tiers used when config.yaml is unavailable."""

DEFAULTS = {
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
}
