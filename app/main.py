from __future__ import annotations

import asyncio
import logging

from telegram.ext import Application

from app.config import settings
from app.db.session import init_db
from app.bot.webhook import register_handlers
from app.queue.dispatcher import dispatch_loop, set_bot
from app.monitoring.events import log_event

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)


async def main() -> None:
    await init_db()

    app = Application.builder().token(settings.telegram_bot_token).build()
    register_handlers(app)
    set_bot(app)

    async with app:
        await app.bot.delete_webhook(drop_pending_updates=True)
        await app.initialize()
        await app.start()

        asyncio.create_task(dispatch_loop())

        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        scheduler = AsyncIOScheduler(timezone="UTC")
        scheduler.add_job(_night_run,     trigger="cron", hour=21, minute=0, id="night_run")
        # Almaty is UTC+5 (no DST). 09:00 Almaty = 04:00 UTC, 17:00 Almaty = 12:00 UTC.
        scheduler.add_job(_morning_run,   trigger="cron", hour=4,  minute=0, id="morning_run")
        scheduler.add_job(_afternoon_run, trigger="cron", hour=12, minute=0, id="afternoon_run")
        scheduler.start()

        await log_event("startup", "Job Agent started")
        log.info("Job Agent started. Scheduled: night@21:00UTC, morning@04:00UTC, afternoon@12:00UTC")

        await app.updater.start_polling(drop_pending_updates=True)
        await asyncio.Event().wait()


async def _night_run() -> None:
    from app.queue.processor import run_pipeline, is_paused
    if is_paused():
        log.info("Night run skipped — paused")
        return
    log.info("Night run starting")
    result = await run_pipeline(night_run=True)
    log.info("Night run done: %s", result)
    await log_event("night_run_done", str(result))


async def _morning_run() -> None:
    await _auto_scrape_and_notify("morning")


async def _afternoon_run() -> None:
    await _auto_scrape_and_notify("afternoon")


async def _auto_scrape_and_notify(label: str) -> None:
    """Scrape all-KZ jobs and send the browse session list (or 'No new matches found.')."""
    from app.queue.processor import is_paused
    import app.bot.webhook as wh
    if is_paused():
        log.info("Scheduled %s run skipped — pipeline paused", label)
        return
    if wh._app is None:
        log.warning("Scheduled %s run skipped — bot not initialized", label)
        return
    log.info("Scheduled %s run starting", label)
    msg = await wh._app.bot.send_message(
        chat_id=settings.telegram_chat_id,
        text="Scanning for new jobs...",
    )
    try:
        await wh._run_pipeline_and_report(
            wh._app.bot,
            msg.message_id,
            location_filter="kz",
            empty_text="No new matches found.",
        )
        await log_event("scheduled_run_done", f"label={label}")
    except Exception as e:
        log.error("Scheduled %s run failed: %s", label, e)
        await log_event("scheduled_run_error", f"label={label} error={e}")


if __name__ == "__main__":
    asyncio.run(main())