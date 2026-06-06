# Job Agent

Autonomous job search agent for DS/ML/AI roles. Scrapes hh.kz, LinkedIn, Telegram channels, and 5 remote boards. Rule-based filtering everywhere — LLM only for ambiguous Telegram posts.

## Quick Start

### 1. Prerequisites
- Docker Desktop
- Python 3.11+ (for one-time local scripts)

### 2. Configure
```bash
cp .env.example .env
# Fill in all values in .env
```

### 3. Generate Telegram session string (run ONCE locally, not in Docker)
```bash
pip install telethon
python generate_session.py
# Copy TELEGRAM_SESSION_STRING= line into .env
```

### 4. Start
```bash
docker compose up --build
```

### 5. Register bot commands (once)
```bash
pip install python-telegram-bot python-dotenv
python setup_bot_commands.py
```

### 6. Use
- Send `/find` or tap Find Jobs to run the pipeline
- Approve/skip cards as they arrive
- `/set days 3` — search last 3 days
- `/set roles Data Scientist, NLP Engineer` — update roles
- `/health` — check LLM provider status

## Architecture

```
Telegram bot (webhook)
    │
    ├── /find → run_pipeline()
    │              ├── hh.kz scraper      (rule filter: title matching)
    │              ├── LinkedIn scraper   (rule filter: title matching)
    │              ├── Telegram scraper   (Stage 0-2 rules → Stage 3 LLM tiebreak for ambiguous only)
    │              └── Remote boards      (rule filter: title + location)
    │                        │
    │                   matches table (status=waiting)
    │                        │
    └── QueueDispatcher (polls every 5s)
             │
             └── Gate 1 card → you approve/skip
```

## Filter Logic (Telegram)

Stage 0: Is it a job post? Drop ads, courses, too-short noise.
Stage 1: Split multi-vacancy posts into segments.
Stage 2: Rule score each segment → in_field / out_of_field / ambiguous.
Stage 3 (LLM, only ambiguous): walk gemini_a→b → groq_a→b → mistral → openrouter.
         All exhausted → surface as ⚠️ LOW CONF card for manual review.

## Reset dedup
```bash
docker exec -it job-agent-db-1 psql -U jobagent -d job_agent \
  -c "TRUNCATE all_messages, matches, seen_jobs CASCADE;"
```

## Force rebuild
```bash
docker compose down && docker compose build --no-cache && docker compose up
```
