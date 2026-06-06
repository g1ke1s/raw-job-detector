from __future__ import annotations

import logging
from app.config import settings

log = logging.getLogger(__name__)


async def send_dm(handle: str, text: str) -> bool:
    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession

        client = TelegramClient(
            StringSession(settings.telegram_session_string),
            settings.telegram_api_id,
            settings.telegram_api_hash,
        )
        await client.connect()
        if not await client.is_user_authorized():
            log.error("Telethon session not authorized")
            return False

        await client.send_message(handle.lstrip("@"), text)
        await client.disconnect()
        log.info("DM sent to %s", handle)
        return True
    except Exception as e:
        log.error("Failed to send DM to %s: %s", handle, e)
        return False