from __future__ import annotations

import asyncio
import html
import logging
import re
from datetime import datetime, timedelta

from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.db.models import Match, Decision
from app.runtime_config import rc

log = logging.getLogger(__name__)
_bot_app = None
_decision_event: asyncio.Event | None = None

_SOURCE_DISPLAY = {
    "hh": "hh.kz",
    "linkedin": "LinkedIn",
    "telegram": "Telegram",
    "remote": "Remote",
}


def set_bot(app) -> None:
    global _bot_app
    _bot_app = app


def notify_decision() -> None:
    """Wake the dispatch loop immediately after a Gate 1 decision resolves a match."""
    global _decision_event
    if _decision_event is not None:
        _decision_event.set()


async def dispatch_loop() -> None:
    global _decision_event
    _decision_event = asyncio.Event()
    log.info("QueueDispatcher started")
    while True:
        try:
            await _tick()
        except Exception as e:
            log.error("Dispatcher tick error: %s", e)
        try:
            await asyncio.wait_for(_decision_event.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            pass
        _decision_event.clear()


async def _tick() -> None:
    if _bot_app is None:
        return
    from app.queue.processor import is_paused
    if is_paused():
        return

    timeout_h = int(rc.get("approval.approval_timeout_hours") or 24)
    cutoff = datetime.utcnow() - timedelta(hours=timeout_h)

    async with AsyncSessionLocal() as s:
        result = await s.execute(
            select(Match).where(
                Match.status == "sent_to_user",
                Match.updated_at < cutoff,
            )
        )
        for match in result.scalars():
            match.status = "rejected"
            match.updated_at = datetime.utcnow()
            s.add(Decision(match_id=match.id, verdict="rejected", reason="timeout"))
        await s.commit()

        in_flight = await s.scalar(select(Match).where(Match.status == "sent_to_user"))
        if in_flight:
            return

        next_match = await s.scalar(
            select(Match).where(
                Match.status == "waiting",
                Match.night_run == False,
                Match.seniority.is_(None),
            ).order_by(Match.created_at)
        )
        if not next_match:
            return

        try:
            msg_id = await _send_gate1_card(next_match)
            next_match.status = "sent_to_user"
            next_match.telegram_message_id = msg_id
            next_match.updated_at = datetime.utcnow()
            await s.commit()
        except Exception as e:
            log.error("Failed to send Gate 1 card for match %d: %s", next_match.id, e)


def _clean(t: str) -> str:
    t = html.unescape(t or "")
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s{2,}", " ", t).strip()


def _build_card_text(match: Match) -> str:
    title = _clean(match.title or "Unknown role")
    company = _clean(match.company or "")

    desc = _clean(match.description or "")
    if len(desc) > 200:
        cut = desc[:200].rsplit(" ", 1)[0]
        desc = cut + "..."

    source_label = _SOURCE_DISPLAY.get(match.source or "", match.source or "")
    salary = _clean((match.job_json or {}).get("salary", "") or "") if match.job_json else ""
    salary_line = f"💰 {salary}" if salary else "💰 Not specified"

    lines = [title]
    if company:
        lines.append(company)
    if desc:
        lines += ["", desc]
    lines += ["", f"{salary_line} · {source_label}"]
    if match.url:
        lines += ["", f"🔗 {match.url}"]
    return "\n".join(lines)


async def _send_gate1_card(match: Match) -> int:
    from app.config import settings
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    text = _build_card_text(match)
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"g1_approve_{match.id}"),
        InlineKeyboardButton("🚀 Boost", callback_data=f"g1_boost_{match.id}"),
        InlineKeyboardButton("❌ Skip", callback_data=f"g1_skip_{match.id}"),
    ]])
    msg = await _bot_app.bot.send_message(
        chat_id=settings.telegram_chat_id,
        text=text,
        reply_markup=keyboard,
    )
    return msg.message_id


async def send_card_direct(match: Match) -> int:
    return await _send_gate1_card(match)
