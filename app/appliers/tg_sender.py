from __future__ import annotations

import io
import logging
from app.config import settings

log = logging.getLogger(__name__)


async def _get_client():
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    client = TelegramClient(
        StringSession(settings.telegram_session_string),
        settings.telegram_api_id,
        settings.telegram_api_hash,
    )
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise RuntimeError("Telethon session not authorized")
    return client


async def send_dm(handle: str, text: str) -> bool:
    try:
        client = await _get_client()
        await client.send_message(handle.lstrip("@"), text)
        await client.disconnect()
        log.info("DM sent to %s", handle)
        return True
    except Exception as e:
        log.error("Failed to send DM to %s: %s", handle, e)
        return False


async def send_dm_with_document(
    handle: str, text: str, doc_bytes: bytes, filename: str = "CV.pdf"
) -> bool:
    """Send a Telegram DM with a text message followed by a PDF document."""
    try:
        client = await _get_client()
        peer = handle.lstrip("@")
        await client.send_message(peer, text)
        buf = io.BytesIO(doc_bytes)
        buf.name = filename
        await client.send_file(peer, buf, caption=filename)
        await client.disconnect()
        log.info("DM with document sent to %s", handle)
        return True
    except Exception as e:
        log.error("Failed to send DM with document to %s: %s", handle, e)
        return False