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
        scheduler.add_job(_night_run, trigger="cron", hour=21, minute=0, id="night_run")
        scheduler.start()

        await log_event("startup", "Job Agent started")
        log.info("Job Agent started. Night run at 21:00 UTC (2am Almaty)")

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


if __name__ == "__main__":
    asyncio.run(main())