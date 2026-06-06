"""
Telegram channel scraper using Telethon (user session, read-only).
Reads all channels in the configured folder (default: 🎯).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.config import settings
from app.runtime_config import rc

log = logging.getLogger(__name__)


async def scrape_telegram(days_back: int = 1, messages_per_channel: int = 50) -> list[dict]:
    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
        from telethon.tl.functions.messages import GetDialogFiltersRequest
    except ImportError:
        log.error("Telethon not installed")
        return []

    folder_name = rc.get("scraping.telegram_folder") or "🎯"
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    results: list[dict] = []

    client = TelegramClient(
        StringSession(settings.telegram_session_string),
        settings.telegram_api_id,
        settings.telegram_api_hash,
    )

    try:
        await client.connect()
        if not await client.is_user_authorized():
            log.error("Telegram session not authorized — re-run generate_session.py")
            return []

        # Find folder by name
        filters = await client(GetDialogFiltersRequest())
        target_folder = None
        filter_list = filters if isinstance(filters, list) else filters.filters
        for f in filter_list:
            if hasattr(f, "title") and f.title == folder_name:
                target_folder = f
                break

        if not target_folder:
            log.warning("Telegram folder '%s' not found — scraping all dialogs", folder_name)
            channels = await _get_all_channels(client)
        else:
            channels = await _get_folder_channels(client, target_folder)

        log.info("Telegram: %d channels in folder '%s'", len(channels), folder_name)

        for channel in channels:
            try:
                async for message in client.iter_messages(
                    channel, limit=messages_per_channel,
                    offset_date=None, reverse=False,
                ):
                    if message.date and message.date < cutoff:
                        break
                    if not message.text:
                        continue

                    results.append({
                        "source": "telegram",
                        "message_id": f"tg_{channel.id}_{message.id}",
                        "raw_text": message.text,
                        "url": f"https://t.me/{getattr(channel, 'username', channel.id)}/{message.id}",
                        "channel": getattr(channel, "username", str(channel.id)),
                        "date": message.date.isoformat(),
                    })
                await asyncio.sleep(0.5)
            except Exception as e:
                log.warning("Telegram channel %s error: %s", channel, e)

    finally:
        await client.disconnect()

    log.info("Telegram raw messages=%d", len(results))
    return results


async def _get_folder_channels(client, folder) -> list:
    from telethon.tl.types import InputPeerChannel
    channels = []
    for peer in getattr(folder, "include_peers", []):
        try:
            entity = await client.get_entity(peer)
            channels.append(entity)
        except Exception:
            pass
    return channels


async def _get_all_channels(client) -> list:
    channels = []
    async for dialog in client.iter_dialogs():
        if dialog.is_channel:
            channels.append(dialog.entity)
    return channels[:50]  # safety cap
