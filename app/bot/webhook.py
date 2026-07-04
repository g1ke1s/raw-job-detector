from __future__ import annotations

import asyncio
import html
import logging
import re
from datetime import datetime

from telegram import Update
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes,
)
from sqlalchemy import select, func

from app.config import settings
from app.db.session import AsyncSessionLocal
from app.db.models import Match, Decision, RunLog, EventLog, FindSession
from app.bot.keyboards import main_menu_keyboard, settings_keyboard, ALL_SOURCES
from app.monitoring.events import log_event
from app.runtime_config import rc

log = logging.getLogger(__name__)

_pending_cv_text: set[int] = set()
# PDF bytes waiting for Boost "Send + apply" confirmation
_pending_pdfs: dict[int, bytes] = {}
_app = None

# ── Find session state (DB is source of truth; memory is a cache) ─────────────
_active_session_id: int | None = None
_active_session_msg_id: int | None = None
_active_session_match_ids: list[int] = []
_active_session_decisions: dict[int, str] = {}  # match_id (int) → "✅"/"❌"/"🚀"
_current_detail_match_id: int | None = None     # job currently in detail/cover view
_boost_target_match_id: int | None = None       # anti-race: cleared when user navigates away

# CV-parse system prompt imported at module level for handle_message
from app.cv.ingest import _SYSTEM as _SYSTEM_CV_PARSE

_SOURCE_DISPLAY = {"hh": "hh.kz", "linkedin": "LinkedIn", "telegram": "Telegram", "remote": "Remote"}


def _authorized(update: Update) -> bool:
    uid = None
    if update.message:
        uid = update.message.from_user.id
    elif update.callback_query:
        uid = update.callback_query.from_user.id
    return uid == settings.telegram_chat_id


def _clean(t: str) -> str:
    t = html.unescape(t or "")
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s{2,}", " ", t).strip()


def _settings_state():
    sources = rc.get("scraping.active_sources") or ALL_SOURCES
    days = int(rc.get("scraping.days_back") or 1)
    max_m = int(rc.get("filtering.max_matches_per_run") or 10)
    return sources, days, max_m


def _health_text() -> str:
    from app.llm.client import _benched, _windows, _RPM
    import time
    lines = ["LLM providers:"]
    for provider in _RPM:
        if provider in _benched and time.time() < _benched[provider]:
            lines.append(f"  {provider}: BENCHED {int(_benched[provider]-time.time())}s")
        else:
            lines.append(f"  {provider}: OK ({len(_windows.get(provider,[]))}/{_RPM[provider]} rpm)")
    return "\n".join(lines)


async def _logs_text() -> str:
    async with AsyncSessionLocal() as s:
        events = (await s.execute(
            select(EventLog).order_by(EventLog.ts.desc()).limit(15)
        )).scalars().all()
    if not events:
        return "No events yet."
    return "\n".join(
        f"{e.ts.strftime('%H:%M:%S')} [{e.level}] {e.event}: {e.detail}"
        for e in reversed(events)
    )


def _notify_dispatcher() -> None:
    from app.queue.dispatcher import notify_decision
    notify_decision()


# ── Find session DB helpers ────────────────────────────────────────────────────

async def _ensure_session_loaded() -> bool:
    """Load active session from DB into memory if not already loaded. Returns True if found."""
    global _active_session_id, _active_session_msg_id, _active_session_match_ids, _active_session_decisions
    if _active_session_id is not None:
        return True
    async with AsyncSessionLocal() as s:
        session = await s.scalar(
            select(FindSession).where(FindSession.closed_at.is_(None))
        )
        if not session:
            return False
        _active_session_id = session.id
        _active_session_msg_id = session.message_id
        _active_session_match_ids = list(session.match_ids)
        _active_session_decisions = {int(k): v for k, v in (session.decisions or {}).items()}
        return True


async def _create_session(message_id: int, match_ids: list[int]) -> None:
    """Close any existing session and open a new one."""
    global _active_session_id, _active_session_msg_id, _active_session_match_ids
    global _active_session_decisions, _current_detail_match_id, _boost_target_match_id
    async with AsyncSessionLocal() as s:
        existing = await s.scalar(select(FindSession).where(FindSession.closed_at.is_(None)))
        if existing:
            existing.closed_at = datetime.utcnow()
        session = FindSession(
            chat_id=settings.telegram_chat_id,
            message_id=message_id,
            match_ids=match_ids,
            decisions={},
        )
        s.add(session)
        await s.commit()
        await s.refresh(session)
        _active_session_id = session.id
    _active_session_msg_id = message_id
    _active_session_match_ids = list(match_ids)
    _active_session_decisions = {}
    _current_detail_match_id = None
    _boost_target_match_id = None


async def _session_record_decision(match_id: int, icon: str) -> None:
    """Persist a decision in memory + DB. Closes session when all decided."""
    global _active_session_decisions
    _active_session_decisions[match_id] = icon
    if not _active_session_id:
        return
    async with AsyncSessionLocal() as s:
        session = await s.get(FindSession, _active_session_id)
        if not session:
            return
        decisions = dict(session.decisions or {})
        decisions[str(match_id)] = icon
        session.decisions = decisions
        if all(str(mid) in decisions for mid in session.match_ids):
            session.closed_at = datetime.utcnow()
        await s.commit()


async def _close_session() -> None:
    global _active_session_id, _active_session_msg_id, _active_session_match_ids
    global _active_session_decisions, _current_detail_match_id, _boost_target_match_id
    if _active_session_id:
        async with AsyncSessionLocal() as s:
            session = await s.get(FindSession, _active_session_id)
            if session and not session.closed_at:
                session.closed_at = datetime.utcnow()
                await s.commit()
    _active_session_id = None
    _active_session_msg_id = None
    _active_session_match_ids = []
    _active_session_decisions = {}
    _current_detail_match_id = None
    _boost_target_match_id = None


# ── View builders ──────────────────────────────────────────────────────────────

async def _load_match_data(match_ids: list[int]) -> dict[int, tuple[str, str, float]]:
    """Returns {match_id: (title, company, score)}."""
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(
            select(Match).where(Match.id.in_(match_ids))
        )).scalars().all()
    return {m.id: (_clean(m.title or "?"), _clean(m.company or "?"), m.rule_score or 0.0) for m in rows}


def _build_list_text(match_ids: list[int], match_data: dict, decisions: dict) -> str:
    undecided = sum(1 for mid in match_ids if mid not in decisions)
    header = f"Found {len(match_ids)} jobs — tap to open:" if undecided else f"{len(match_ids)} jobs — all reviewed:"
    lines = [header, ""]
    for i, mid in enumerate(match_ids):
        title, company, score = match_data.get(mid, ("?", "?", 0.0))
        icon = decisions.get(mid)
        if icon:
            lines.append(f"{i+1}. {icon} {title[:35]} · {company[:20]}")
        else:
            lines.append(f"{i+1}. {title[:30]} · {company[:20]} · {int(score*100)}%")
    return "\n".join(lines)


def _build_list_keyboard(match_ids: list[int], decisions: dict):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    buttons = [
        InlineKeyboardButton(str(i + 1), callback_data=f"fs_detail_{mid}")
        for i, mid in enumerate(match_ids)
        if mid not in decisions
    ]
    rows = [buttons[i:i+4] for i in range(0, len(buttons), 4)]
    return InlineKeyboardMarkup(rows) if rows else InlineKeyboardMarkup([[]])


def _build_detail_text(match: Match) -> str:
    title = _clean(match.title or "Unknown role")
    company = _clean(match.company or "")
    desc = _clean(match.description or "")
    if len(desc) > 200:
        cut = desc[:200].rsplit(" ", 1)[0]
        desc = cut + "..."
    source_label = _SOURCE_DISPLAY.get(match.source or "", match.source or "")
    salary = _clean((match.job_json or {}).get("salary", "") or "")
    salary_line = f"💰 {salary}" if salary else "💰 Not specified"
    lines = [title]
    if company:
        lines.append(company)
    if desc:
        lines += ["", desc]
    lines += ["", f"{salary_line} · {source_label}"]
    if match.url:
        lines += ["", f"Open posting: {match.url}"]
    return "\n".join(lines)


def _build_detail_keyboard(match_id: int):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Approve", callback_data=f"fs_approve_{match_id}"),
            InlineKeyboardButton("🚀 Boost",   callback_data=f"fs_boost_{match_id}"),
            InlineKeyboardButton("❌ Skip",     callback_data=f"fs_skip_{match_id}"),
        ],
        [InlineKeyboardButton("← Back to list", callback_data="fs_back")],
    ])


def _build_loading_text(title: str, company: str) -> str:
    return f"Generating cover letter for\n{title} · {company}..."


def _build_back_keyboard():
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    return InlineKeyboardMarkup([[InlineKeyboardButton("← Back to list", callback_data="fs_back")]])


def _build_cover_text(match: Match, cover_text: str) -> str:
    title = _clean(match.title or "")
    company = _clean(match.company or "")
    sep = "--------------------"
    return f"{title} · {company}\n{sep}\n{cover_text[:3000]}\n{sep}"


def _build_cover_keyboard(match_id: int):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📋 Copy letter",  callback_data=f"fs_copy_{match_id}"),
            InlineKeyboardButton("📤 Send + apply", callback_data=f"fs_send_{match_id}"),
            InlineKeyboardButton("❌ Cancel",        callback_data=f"fs_cancel_{match_id}"),
        ],
        [InlineKeyboardButton("← Back to list", callback_data="fs_back")],
    ])


# ── Session message editor (handles >48h gracefully) ──────────────────────────

async def _edit_session_message(text: str, keyboard=None) -> None:
    global _active_session_msg_id, _active_session_id
    if _active_session_msg_id is None or _app is None:
        return
    try:
        await _app.bot.edit_message_text(
            chat_id=settings.telegram_chat_id,
            message_id=_active_session_msg_id,
            text=text,
            reply_markup=keyboard,
        )
    except Exception as e:
        err = str(e).lower()
        if any(kw in err for kw in ("can't be edited", "not found", "message_id_invalid", "too old")):
            # Message is >48h old — send a fresh one and update session
            msg = await _app.bot.send_message(
                chat_id=settings.telegram_chat_id,
                text=text,
                reply_markup=keyboard,
            )
            _active_session_msg_id = msg.message_id
            if _active_session_id:
                async with AsyncSessionLocal() as s:
                    session = await s.get(FindSession, _active_session_id)
                    if session:
                        session.message_id = msg.message_id
                        await s.commit()
            try:
                await _app.bot.pin_chat_message(
                    chat_id=settings.telegram_chat_id,
                    message_id=msg.message_id,
                    disable_notification=True,
                )
            except Exception:
                pass
        else:
            log.warning("edit_session_message failed: %s", e)


async def _go_list_view() -> None:
    """Return the session message to State 1 (list view)."""
    if not _active_session_match_ids:
        return
    match_data = await _load_match_data(_active_session_match_ids)
    text = _build_list_text(_active_session_match_ids, match_data, _active_session_decisions)
    kb = _build_list_keyboard(_active_session_match_ids, _active_session_decisions)
    await _edit_session_message(text, kb)


# ── Pipeline runner ────────────────────────────────────────────────────────────

async def _run_pipeline_and_report(bot, msg_id: int, location_filter: str = "almaty") -> None:
    from app.queue.processor import run_pipeline
    start_ts = datetime.utcnow()
    result = await run_pipeline(location_filter=location_filter)

    if "error" in result:
        await bot.edit_message_text(
            chat_id=settings.telegram_chat_id, message_id=msg_id,
            text=f"Pipeline error:\n{result['error']}",
        )
        return

    if not result.get("enqueued"):
        loc_tag = "" if location_filter == "almaty" else f" [{location_filter.upper()}]"
        await bot.edit_message_text(
            chat_id=settings.telegram_chat_id, message_id=msg_id,
            text=f"Done{loc_tag}. Scraped {result['scraped']}, nothing new.",
        )
        return

    async with AsyncSessionLocal() as s:
        new_matches = (await s.execute(
            select(Match)
            .where(
                Match.status == "waiting",
                Match.created_at >= start_ts,
                Match.night_run == False,
                Match.seniority.is_(None),
            )
            .order_by(Match.rule_score.desc())
        )).scalars().all()

    if not new_matches:
        await bot.edit_message_text(
            chat_id=settings.telegram_chat_id, message_id=msg_id,
            text=f"Done. Scraped {result['scraped']}, nothing queued.",
        )
        return

    match_ids = [m.id for m in new_matches]
    await _create_session(msg_id, match_ids)

    match_data = {
        m.id: (_clean(m.title or "?"), _clean(m.company or "?"), m.rule_score or 0.0)
        for m in new_matches
    }
    text = _build_list_text(match_ids, match_data, {})
    kb = _build_list_keyboard(match_ids, {})

    await bot.edit_message_text(
        chat_id=settings.telegram_chat_id,
        message_id=msg_id,
        text=text,
        reply_markup=kb,
    )
    try:
        await bot.pin_chat_message(
            chat_id=settings.telegram_chat_id,
            message_id=msg_id,
            disable_notification=True,
        )
    except Exception:
        pass


# ── Find-session callback handlers ────────────────────────────────────────────

async def _handle_fs_detail(query, match_id: int) -> None:
    """State 1 → State 2: show full job card."""
    global _current_detail_match_id, _boost_target_match_id
    async with AsyncSessionLocal() as s:
        match = await s.get(Match, match_id)
    if not match:
        await query.answer("Job not found.", show_alert=True)
        return
    if match_id in _active_session_decisions:
        icon = _active_session_decisions[match_id]
        await query.answer(f"Already decided: {icon}", show_alert=False)
        return
    _current_detail_match_id = match_id
    _boost_target_match_id = None
    text = _build_detail_text(match)
    kb = _build_detail_keyboard(match_id)
    await _edit_session_message(text, kb)


async def _handle_fs_approve(query, match_id: int) -> None:
    """State 2 → State 1: approve job, auto-send if Telegram/remote contact exists."""
    global _current_detail_match_id
    async with AsyncSessionLocal() as s:
        match = await s.get(Match, match_id)
        if not match:
            await query.answer("Job not found.", show_alert=True)
            return
        source = match.source
        handle = match.recruiter_handle
        title = _clean(match.title or "")
        company = _clean(match.company or "")
        url = match.url or ""
        auto_send = source in ("telegram", "remote") and bool(handle)
        match.status = "applied" if auto_send else "approved"
        match.updated_at = datetime.utcnow()
        s.add(Decision(match_id=match_id, verdict="approved", reason="approved"))
        await s.commit()

    await log_event("gate1_approved", f"match_id={match_id} source={source}")
    if auto_send:
        asyncio.create_task(_auto_send_background(match_id, handle, title, company, url))

    _current_detail_match_id = None
    await _session_record_decision(match_id, "✅")
    await _go_list_view()
    _notify_dispatcher()


async def _handle_fs_skip(query, match_id: int) -> None:
    """State 2 → State 1: skip job."""
    global _current_detail_match_id
    async with AsyncSessionLocal() as s:
        match = await s.get(Match, match_id)
        if not match:
            await query.answer("Job not found.", show_alert=True)
            return
        match.status = "rejected"
        match.updated_at = datetime.utcnow()
        s.add(Decision(match_id=match_id, verdict="rejected", reason="skipped"))
        await s.commit()

    await log_event("gate1_skipped", f"match_id={match_id}")
    _current_detail_match_id = None
    await _session_record_decision(match_id, "❌")
    await _go_list_view()
    _notify_dispatcher()


async def _handle_fs_boost(query, match_id: int) -> None:
    """State 2 → State 3 (loading): start cover letter generation in background."""
    global _current_detail_match_id, _boost_target_match_id
    async with AsyncSessionLocal() as s:
        match = await s.get(Match, match_id)
        if not match:
            await query.answer("Job not found.", show_alert=True)
            return
        title = _clean(match.title or "")
        company = _clean(match.company or "")
        match.status = "approved"
        match.updated_at = datetime.utcnow()
        s.add(Decision(match_id=match_id, verdict="approved", reason="boost"))
        await s.commit()

    await log_event("gate1_boosted", f"match_id={match_id}")
    _current_detail_match_id = match_id
    _boost_target_match_id = match_id

    await _edit_session_message(
        _build_loading_text(title, company),
        _build_back_keyboard(),
    )
    asyncio.create_task(_boost_in_session(match_id, title, company))


async def _boost_in_session(match_id: int, title: str, company: str) -> None:
    """Background: generate cover + tailor CV, then edit session message to State 3 ready."""
    global _boost_target_match_id
    cover_text: str | None = None
    pdf_bytes: bytes | None = None

    try:
        from app.cv.cover import generate_cover
        cover_text = await generate_cover(match_id)
    except Exception as e:
        log.error("boost cover gen failed for match %d: %s", match_id, e)

    try:
        from app.cv.tailor import tailor_and_render
        pdf_bytes, _ = await tailor_and_render(match_id)
    except Exception as e:
        log.error("boost tailor failed for match %d: %s", match_id, e)

    if pdf_bytes:
        _pending_pdfs[match_id] = pdf_bytes

    # Anti-race: abort if user navigated away during generation
    if _boost_target_match_id != match_id or _app is None:
        return

    if not cover_text:
        await _edit_session_message(
            f"Cover letter generation failed for {title} · {company}.\nCheck /errors.",
            _build_back_keyboard(),
        )
        return

    async with AsyncSessionLocal() as s:
        match = await s.get(Match, match_id)
    if not match:
        return

    text = _build_cover_text(match, cover_text)
    kb = _build_cover_keyboard(match_id)
    await _edit_session_message(text, kb)


async def _handle_fs_back(query) -> None:
    """State 2 or 3 → State 1: return to list without recording a decision."""
    global _current_detail_match_id, _boost_target_match_id
    _current_detail_match_id = None
    _boost_target_match_id = None  # cancel pending boost display if racing
    await _go_list_view()


async def _handle_fs_copy(query, match_id: int) -> None:
    """State 3: send raw cover letter as a separate copyable message (deleted after 60s)."""
    async with AsyncSessionLocal() as s:
        match = await s.get(Match, match_id)
    cover_text = (match.cover_text or "") if match else ""
    if not cover_text:
        await query.answer("No cover letter available.", show_alert=True)
        return
    if _app:
        msg = await _app.bot.send_message(
            chat_id=settings.telegram_chat_id,
            text=cover_text,
        )
        asyncio.create_task(_delete_after(msg.message_id, 60))
    await query.answer("Letter sent — copies itself after 60s", show_alert=False)


async def _handle_fs_send(query, match_id: int) -> None:
    """State 3 → State 1: send cover letter + PDF to recruiter, mark applied."""
    global _current_detail_match_id
    async with AsyncSessionLocal() as s:
        match = await s.get(Match, match_id)
        if not match:
            await query.answer("Job not found.", show_alert=True)
            return
        cover_text = match.cover_text or ""
        handle = match.recruiter_handle
        title = _clean(match.title or "")
        company = _clean(match.company or "")
        url = match.url or ""

    if not cover_text:
        await _edit_session_message(
            f"No cover letter available for {title}.\nRe-open and use Boost again.",
            _build_back_keyboard(),
        )
        return

    pdf_bytes = _pending_pdfs.pop(match_id, None)

    if not handle:
        # No direct contact — deliver PDF to user's chat for manual upload
        if pdf_bytes and _app:
            import io
            bio = io.BytesIO(pdf_bytes)
            bio.name = f"CV_{match_id}.pdf"
            await _app.bot.send_document(
                chat_id=settings.telegram_chat_id,
                document=bio,
                filename=f"CV_{match_id}.pdf",
                caption="Tailored CV — apply manually at the link below.",
            )
        _current_detail_match_id = None
        await _session_record_decision(match_id, "✅")
        await _go_list_view()
        if url:
            await _app.bot.send_message(
                chat_id=settings.telegram_chat_id,
                text=f"No direct recruiter contact. Apply manually:\n{url}",
            )
        _notify_dispatcher()
        return

    filename = f"CV_{title[:30].replace(' ', '_')}_{match_id}.pdf"
    ok = False
    if handle.startswith("@"):
        from app.appliers.tg_sender import send_dm, send_dm_with_document
        ok = await (send_dm_with_document(handle, cover_text, pdf_bytes, filename) if pdf_bytes else send_dm(handle, cover_text))
    else:
        from app.appliers.email_sender import send_email, send_email_with_pdf
        subject = f"Application: {title} at {company}"
        ok = await (send_email_with_pdf(handle, subject, cover_text, pdf_bytes, filename) if pdf_bytes else send_email(handle, subject, cover_text))

    await log_event("boost_sent", f"match_id={match_id} handle={handle} ok={ok}")

    if not ok:
        await _edit_session_message(
            f"Send failed to {handle}.\nCheck /errors.\n\nApply manually: {url}",
            _build_back_keyboard(),
        )
        return

    async with AsyncSessionLocal() as s:
        match = await s.get(Match, match_id)
        if match:
            match.status = "applied"
            match.updated_at = datetime.utcnow()
            await s.commit()

    _current_detail_match_id = None
    await _session_record_decision(match_id, "✅")
    await _go_list_view()
    _notify_dispatcher()


async def _handle_fs_cancel(query, match_id: int) -> None:
    """State 3 → State 2: cancel cover letter view, go back to detail."""
    global _boost_target_match_id
    _boost_target_match_id = None
    async with AsyncSessionLocal() as s:
        match = await s.get(Match, match_id)
    if not match:
        await _handle_fs_back(query)
        return
    text = _build_detail_text(match)
    kb = _build_detail_keyboard(match_id)
    await _edit_session_message(text, kb)


# ── Auto-send helpers ──────────────────────────────────────────────────────────

async def _auto_send_background(match_id: int, handle: str, title: str, company: str, url: str) -> None:
    """Background auto-send for Telegram/remote sources on Approve."""
    async with AsyncSessionLocal() as s:
        match = await s.get(Match, match_id)
        cover_text = (match.cover_text or "") if match else ""

    if not cover_text:
        log.info("auto_send match %d: no cover text, skipping send", match_id)
        return

    ok = False
    if handle.startswith("@"):
        from app.appliers.tg_sender import send_dm
        ok = await send_dm(handle, cover_text)
    else:
        from app.appliers.email_sender import send_email
        ok = await send_email(handle, f"Application: {title} at {company}", cover_text)

    await log_event("auto_send", f"match={match_id} handle={handle} ok={ok}")


async def _delete_after(msg_id: int, delay_seconds: int) -> None:
    await asyncio.sleep(delay_seconds)
    try:
        if _app:
            await _app.bot.delete_message(
                chat_id=settings.telegram_chat_id,
                message_id=msg_id,
            )
    except Exception:
        pass


# ── Legacy Gate 1 handlers (auto-dispatch cards when no session is active) ────

async def _handle_approve(query, match_id: int) -> None:
    async with AsyncSessionLocal() as s:
        match = await s.get(Match, match_id)
        if not match:
            await query.edit_message_text("Match not found.")
            return
        match.status = "approved"
        match.updated_at = datetime.utcnow()
        s.add(Decision(match_id=match_id, verdict="approved", reason="approved"))
        await s.commit()
        title = _clean(match.title or "")
        company = _clean(match.company or "")
        url = match.url or ""
    await log_event("gate1_approved", f"match_id={match_id}")
    text = f"✅ {title} · {company}"
    if url:
        text += f"\n🔗 {url}"
    await query.edit_message_text(text)
    _notify_dispatcher()


async def _handle_boost(query, match_id: int) -> None:
    async with AsyncSessionLocal() as s:
        match = await s.get(Match, match_id)
        if not match:
            await query.edit_message_text("Match not found.")
            return
        match.status = "approved"
        match.updated_at = datetime.utcnow()
        s.add(Decision(match_id=match_id, verdict="approved", reason="boost"))
        await s.commit()
        title = _clean(match.title or "")
        company = _clean(match.company or "")
        url = match.url or ""
    await log_event("gate1_boosted", f"match_id={match_id}")
    text = f"🚀 {title} · {company}"
    if url:
        text += f"\n🔗 {url}"
    await query.edit_message_text(text)
    if _app:
        asyncio.create_task(_boost_legacy(match_id, title, company))
    _notify_dispatcher()


async def _boost_legacy(match_id: int, title: str, company: str) -> None:
    """Legacy boost: generates cover + CV, sends as separate messages (no session)."""
    cover_text: str | None = None
    pdf_bytes: bytes | None = None
    tailor_status = ""
    try:
        from app.cv.cover import generate_cover
        cover_text = await generate_cover(match_id)
    except Exception as e:
        log.error("legacy boost cover failed for match %d: %s", match_id, e)
    try:
        from app.cv.tailor import tailor_and_render
        pdf_bytes, tailor_status = await tailor_and_render(match_id)
    except Exception as e:
        log.error("legacy boost tailor failed for match %d: %s", match_id, e)
        tailor_status = str(e)
    if pdf_bytes:
        _pending_pdfs[match_id] = pdf_bytes
    if not _app:
        return
    if not cover_text:
        await _app.bot.send_message(
            chat_id=settings.telegram_chat_id,
            text=f"Cover letter generation failed for {title} @ {company}. Check /errors.",
        )
        return
    await _app.bot.send_message(chat_id=settings.telegram_chat_id, text=cover_text[:4000])
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    action_lines = [f"{title} · {company}"]
    if not pdf_bytes and tailor_status:
        action_lines.append(f"CV: {tailor_status[:80]}")
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("📤 Send now", callback_data=f"boost_send_{match_id}")]])
    await _app.bot.send_message(
        chat_id=settings.telegram_chat_id,
        text="\n".join(action_lines),
        reply_markup=kb,
    )


async def _handle_boost_send(query, match_id: int) -> None:
    """Legacy: handle boost_send_ callbacks from old-style boost messages."""
    await query.edit_message_reply_markup(reply_markup=None)
    async with AsyncSessionLocal() as s:
        match = await s.get(Match, match_id)
        if not match:
            await query.answer("Match not found.", show_alert=True)
            return
        cover_text = match.cover_text or ""
        handle = match.recruiter_handle
        title = _clean(match.title or "")
        company = _clean(match.company or "")
        url = match.url or ""
    pdf_bytes = _pending_pdfs.pop(match_id, None)
    if not cover_text:
        await _app.bot.send_message(
            chat_id=settings.telegram_chat_id,
            text="No cover letter available. Re-run Boost to regenerate.",
        )
        return
    if not handle:
        if pdf_bytes:
            import io
            bio = io.BytesIO(pdf_bytes)
            bio.name = f"CV_{match_id}.pdf"
            await _app.bot.send_document(
                chat_id=settings.telegram_chat_id,
                document=bio,
                filename=f"CV_{match_id}.pdf",
                caption="Tailored CV (for manual upload)",
            )
        await _app.bot.send_message(
            chat_id=settings.telegram_chat_id,
            text=f"No direct recruiter channel. Apply manually:\n{url}",
        )
        return
    filename = f"CV_{title[:30].replace(' ', '_')}_{match_id}.pdf"
    if handle.startswith("@"):
        from app.appliers.tg_sender import send_dm, send_dm_with_document
        ok = await (send_dm_with_document(handle, cover_text, pdf_bytes, filename) if pdf_bytes else send_dm(handle, cover_text))
        result = f"DM sent to {handle}." if ok else f"DM failed to {handle}. Check /errors."
    else:
        from app.appliers.email_sender import send_email, send_email_with_pdf
        subject = f"Application: {title} at {company}"
        ok = await (send_email_with_pdf(handle, subject, cover_text, pdf_bytes, filename) if pdf_bytes else send_email(handle, subject, cover_text))
        result = f"Email sent to {handle}." if ok else f"Email failed to {handle}. Check /errors."
    await _app.bot.send_message(chat_id=settings.telegram_chat_id, text=result)
    await log_event("boost_sent", f"match_id={match_id} handle={handle} ok={ok}")


async def _handle_skip(query, match_id: int, reason: str) -> None:
    async with AsyncSessionLocal() as s:
        match = await s.get(Match, match_id)
        if not match:
            await query.edit_message_text("Match not found.")
            return
        match.status = "rejected"
        match.updated_at = datetime.utcnow()
        s.add(Decision(match_id=match_id, verdict="rejected", reason=reason))
        await s.commit()
    await query.edit_message_text("Skipped.")
    await log_event("gate1_skipped", f"match_id={match_id}")
    _notify_dispatcher()


# ── Commands ──────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update): return
    await update.message.reply_text("Job Agent online.", reply_markup=main_menu_keyboard())


async def cmd_find(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update): return
    msg = await update.message.reply_text("Starting pipeline... (Almaty)")
    asyncio.create_task(_run_pipeline_and_report(ctx.bot, msg.message_id, location_filter="almaty"))


async def cmd_findall(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update): return
    msg = await update.message.reply_text("Starting pipeline... (all KZ)")
    asyncio.create_task(_run_pipeline_and_report(ctx.bot, msg.message_id, location_filter="kz"))


async def cmd_queue(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update): return
    async with AsyncSessionLocal() as s:
        waiting = await s.scalar(select(func.count()).select_from(Match).where(Match.status == "waiting"))
        sent = await s.scalar(select(func.count()).select_from(Match).where(Match.status == "sent_to_user"))
    await update.message.reply_text(f"Queue: {waiting} waiting, {sent} awaiting reply.")


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update): return
    async with AsyncSessionLocal() as s:
        runs = (await s.execute(select(RunLog).order_by(RunLog.started_at.desc()).limit(5))).scalars().all()
        approved = await s.scalar(select(func.count()).select_from(Decision).where(Decision.verdict == "approved"))
        rejected = await s.scalar(select(func.count()).select_from(Decision).where(Decision.verdict == "rejected"))
        night_total = await s.scalar(select(func.count()).select_from(Match).where(Match.night_run == True))
        night_waiting = await s.scalar(select(func.count()).select_from(Match).where(Match.night_run == True, Match.status == "waiting"))
        night_approved = await s.scalar(select(func.count()).select_from(Match).where(Match.night_run == True, Match.status == "approved"))
        from sqlalchemy import text
        src_rows = (await s.execute(text("SELECT source, count(*) FROM matches GROUP BY source"))).fetchall()
    lines = [
        f"Decisions: {approved} approved, {rejected} skipped",
        f"\nNight run: {night_total} total | {night_waiting} waiting | {night_approved} approved",
        "\nBy source:",
    ]
    for src, cnt in src_rows:
        lines.append(f"  {src}: {cnt}")
    lines.append("\nLast 5 runs:")
    for r in runs:
        dur = f" ({int((r.finished_at-r.started_at).total_seconds())}s)" if r.finished_at and r.started_at else ""
        night = " 🌙" if r.is_night else ""
        lines.append(f"  {r.started_at.strftime('%m-%d %H:%M')}{night} [{r.status}]{dur} scraped={r.scraped} enqueued={r.enqueued} llm={r.llm_calls}")
    await update.message.reply_text("\n".join(lines))


async def cmd_health(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update): return
    await update.message.reply_text(_health_text())


async def cmd_logs(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update): return
    await update.message.reply_text(await _logs_text())


async def cmd_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update): return
    sources, days, max_m = _settings_state()
    await update.message.reply_text("Settings:", reply_markup=settings_keyboard(sources, days, max_m))


async def cmd_set(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update): return
    args = ctx.args
    if not args or len(args) < 2:
        await update.message.reply_text("Usage: /set <key> <value>\nKeys: roles, sources, days, max_matches")
        return
    key = args[0].lower()
    value_str = " ".join(args[1:])
    mapping = {
        "roles":       ("scraping.roles",              lambda v: [r.strip() for r in v.split(",")]),
        "sources":     ("scraping.active_sources",     lambda v: [s.strip() for s in v.split(",")]),
        "days":        ("scraping.days_back",           int),
        "max_matches": ("filtering.max_matches_per_run", int),
    }
    if key not in mapping:
        await update.message.reply_text(f"Unknown key. Available: {', '.join(mapping)}")
        return
    yaml_key, converter = mapping[key]
    try:
        rc.set_nested(yaml_key, converter(value_str))
        await update.message.reply_text(f"Updated {yaml_key}")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def cmd_rejected(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update): return
    async with AsyncSessionLocal() as s:
        events = (await s.execute(
            select(EventLog)
            .where(EventLog.event.in_(["filter_reject", "dedup_drop"]))
            .order_by(EventLog.ts.desc())
            .limit(20)
        )).scalars().all()
    if not events:
        await update.message.reply_text("No system-rejected jobs yet.")
        return
    lines = [f"Last {len(events)} system-rejected:\n"]
    for e in reversed(events):
        ts = e.ts.strftime("%m-%d %H:%M") if e.ts else ""
        lines.append(f"{ts} [{e.event}] {e.detail}")
    await update.message.reply_text("\n".join(lines))


async def cmd_errors(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update): return
    async with AsyncSessionLocal() as s:
        events = (await s.execute(
            select(EventLog)
            .where(EventLog.level.in_(["WARNING", "ERROR"]))
            .order_by(EventLog.ts.desc())
            .limit(20)
        )).scalars().all()
    if not events:
        await update.message.reply_text("No scraper errors recorded.")
        return
    lines = [f"Last {len(events)} scraper errors:\n"]
    for e in reversed(events):
        ts = e.ts.strftime("%m-%d %H:%M") if e.ts else ""
        lines.append(f"{ts} [{e.level}] {e.detail}")
    await update.message.reply_text("\n".join(lines))


async def cmd_seniors(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update): return
    async with AsyncSessionLocal() as s:
        matches = (await s.execute(
            select(Match).where(Match.seniority == "senior").order_by(Match.created_at.desc()).limit(20)
        )).scalars().all()
    if not matches:
        await update.message.reply_text("No senior-tagged jobs yet.")
        return
    lines = [f"Last {len(matches)} senior-level roles (hidden from auto-queue):\n"]
    for m in matches:
        pub_date = (m.extra or {}).get("pub_date", "")
        line = f"[{m.source.upper()}] {_clean(m.title or '?')}"
        if m.company: line += f" @ {_clean(m.company)}"
        if pub_date: line += f" | {pub_date}"
        if m.url: line += f"\n{m.url}"
        lines.append(line)
    text = "\n\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n...(truncated)"
    await update.message.reply_text(text)


async def cmd_export(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update): return
    import csv, io
    async with AsyncSessionLocal() as s:
        matches = (await s.execute(select(Match).order_by(Match.created_at.desc()).limit(1000))).scalars().all()
        decisions_rows = (await s.execute(select(Decision))).scalars().all()
    decisions = {d.match_id: d for d in decisions_rows}
    rows = []
    for m in matches:
        dec = decisions.get(m.id)
        if m.status == "applied": status_label = "applied"
        elif dec and dec.verdict == "rejected" and dec.reason == "skipped": status_label = "rejected_user"
        elif m.status == "rejected": status_label = "rejected_user" if dec else "rejected_system"
        elif m.seniority == "senior": status_label = "senior_hidden"
        else: status_label = m.status
        fit = m.fit_assessment_json or {}
        rows.append({
            "date":         m.created_at.strftime("%Y-%m-%d") if m.created_at else "",
            "title":        m.title or "",
            "company":      m.company or "",
            "source":       m.source or "",
            "score":        round(m.rule_score or 0, 2),
            "seniority":    m.seniority or "",
            "status":       status_label,
            "fit_score":    fit.get("score", ""),
            "fit_blockers": "; ".join(fit.get("blockers", [])),
            "url":          m.url or "",
            "pub_date":     (m.extra or {}).get("pub_date", ""),
        })
    output = io.StringIO()
    if rows:
        writer = csv.DictWriter(output, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    bio = io.BytesIO(output.getvalue().encode("utf-8"))
    bio.name = "jobs_export.csv"
    await update.message.reply_document(document=bio, filename="jobs_export.csv",
                                        caption=f"{len(rows)} jobs exported.")


async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update): return
    from app.queue.processor import set_paused
    set_paused(True)
    await update.message.reply_text("Pipeline paused. Use /resume to continue.")


async def cmd_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update): return
    from app.queue.processor import set_paused
    set_paused(False)
    await update.message.reply_text("Pipeline resumed.")


async def cmd_config(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update): return
    import os
    path = os.environ.get("CONFIG_PATH", "/app/config.yaml")
    if not os.path.exists(path):
        path = "config.yaml"
    try:
        with open(path) as f:
            text = f.read()
        await update.message.reply_text(f"<pre>{html.escape(text)}</pre>", parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"Config error: {e}")


# ── CV commands ───────────────────────────────────────────────────────────────

async def _handle_boost_tailor(query, match_id: int) -> None:
    await query.edit_message_text("Tailoring CV... this may take a moment.")
    try:
        from app.cv.tailor import tailor_and_render
        pdf_bytes, status = await tailor_and_render(match_id)
        if pdf_bytes:
            import io
            bio = io.BytesIO(pdf_bytes)
            bio.name = f"cv_tailored_{match_id}.pdf"
            await _app.bot.send_document(
                chat_id=settings.telegram_chat_id,
                document=bio,
                filename=f"cv_tailored_{match_id}.pdf",
                caption=status,
            )
            await query.edit_message_text("Tailored CV sent as PDF.")
        else:
            await query.edit_message_text(f"Tailoring failed: {status}")
    except Exception as e:
        log.error("boost_tailor error for match %d: %s", match_id, e)
        await query.edit_message_text(f"Error during tailoring: {e}")


async def cmd_setcv(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update): return
    _pending_cv_text.add(settings.telegram_chat_id)
    await update.message.reply_text("Paste your CV as plain text in the next message.\nI'll parse it immediately.")


async def cmd_cv(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update): return
    from app.cv.ingest import get_active_profile
    profile = await get_active_profile()
    if not profile:
        await update.message.reply_text("No CV on file.\n\nSend me a PDF of your CV and I'll parse it automatically.")
        return
    cv = profile.structured_json or {}
    name = cv.get("name", "Unknown")
    lines = [
        f"CV v{profile.version} — {name}",
        f"{len(cv.get('experiences', []))} experience entries, {len(cv.get('skills', []))} skills",
        f"Uploaded: {profile.created_at.strftime('%Y-%m-%d %H:%M') if profile.created_at else '?'}",
        "\nSend a new PDF to replace.",
    ]
    async with AsyncSessionLocal() as s:
        last_boosted = await s.scalar(
            select(Match).join(Decision, Decision.match_id == Match.id)
            .where(Decision.reason == "boost").order_by(Match.updated_at.desc())
        )
    if last_boosted:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(f"🔧 Tailor CV for: {(last_boosted.title or '')[:40]}",
                                 callback_data=f"boost_tailor_{last_boosted.id}")
        ]])
        await update.message.reply_text("\n".join(lines), reply_markup=kb)
    else:
        await update.message.reply_text("\n".join(lines))


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update): return
    text = (
        "Job Agent commands\n\n"
        "Search\n"
        "/find — scrape Almaty, open browse session\n"
        "/findall — scrape all Kazakhstan, open browse session\n\n"
        "Browse session\n"
        "Tap a number to open full job card\n"
        "Approve — mark approved (Telegram/remote: auto-sends cover letter)\n"
        "Boost — generate cover letter + tailored CV, then send\n"
        "Skip — reject immediately\n"
        "Back to list — return without decision\n\n"
        "Queue\n"
        "/queue — waiting / in-flight count\n"
        "/seniors — senior-tagged jobs (not auto-sent)\n"
        "/rejected — system-rejected close calls\n"
        "/export — download all matches as CSV\n\n"
        "CV\n"
        "/cv — CV status + tailor button\n"
        "/setcv — paste CV as plain text (if PDF fails)\n\n"
        "Config\n"
        "/settings — toggle sources / days / max\n"
        "/set <key> <value> — edit runtime config\n"
        "/config — show config.yaml\n\n"
        "Monitoring\n"
        "/stats — pipeline statistics\n"
        "/health — LLM provider status\n"
        "/logs — recent events\n"
        "/errors — scraper errors\n\n"
        "Pipeline\n"
        "/stop — pause\n"
        "/resume — unpause"
    )
    await update.message.reply_text(text)


# ── CV upload / text-paste handlers ──────────────────────────────────────────

async def handle_pdf_upload(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update): return
    doc = update.message.document
    if not doc or not doc.file_name or not doc.file_name.lower().endswith(".pdf"): return
    msg = await update.message.reply_text("Parsing CV...")
    try:
        tg_file = await ctx.bot.get_file(doc.file_id)
        pdf_bytes = await tg_file.download_as_bytearray()
        from app.cv.ingest import ingest_pdf
        ok, text = await ingest_pdf(bytes(pdf_bytes))
        await msg.edit_text(("CV saved.\n" if ok else "Failed: ") + text)
    except Exception as e:
        log.error("CV upload error: %s", e)
        await msg.edit_text(f"Error processing PDF: {e}")


async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update): return
    user_id = update.message.from_user.id
    if user_id not in _pending_cv_text:
        return
    _pending_cv_text.discard(user_id)
    raw_text = (update.message.text or "").strip()
    if len(raw_text) < 50:
        await update.message.reply_text("Text too short. Try again with /setcv.")
        return
    msg = await update.message.reply_text("Parsing CV text...")
    from app.cv.ingest import _fix_bullet_ids, _store_profile
    from app.llm.client import complete
    from app.llm.parsing import safe_json_parse
    messages = [
        {"role": "system", "content": _SYSTEM_CV_PARSE},
        {"role": "user", "content": f"CV text:\n\"\"\"\n{raw_text[:6000]}\n\"\"\""},
    ]
    raw = await complete(messages, max_tokens=2000, purpose="cv_parse")
    if not raw:
        await msg.edit_text("LLM unavailable. Try again.")
        return
    structured = safe_json_parse(raw)
    if not structured or not isinstance(structured, dict):
        await msg.edit_text("CV parsing failed (bad LLM output). Try again.")
        return
    structured = _fix_bullet_ids(structured)
    await _store_profile(raw_text, structured)
    name = structured.get("name") or "Unknown"
    await msg.edit_text(
        f"CV saved for {name}.\n"
        f"{len(structured.get('experiences', []))} experience entries, "
        f"{len(structured.get('skills', []))} skills."
    )


# ── Callback router ───────────────────────────────────────────────────────────

async def gate1_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data

    # ── Find session (new single-message flow) ────────────────────────────────
    if data.startswith("fs_detail_"):
        await _ensure_session_loaded()
        await _handle_fs_detail(query, int(data[10:]))
    elif data.startswith("fs_approve_"):
        await _ensure_session_loaded()
        await _handle_fs_approve(query, int(data[11:]))
    elif data.startswith("fs_skip_"):
        await _ensure_session_loaded()
        await _handle_fs_skip(query, int(data[8:]))
    elif data.startswith("fs_boost_"):
        await _ensure_session_loaded()
        await _handle_fs_boost(query, int(data[9:]))
    elif data == "fs_back":
        await _ensure_session_loaded()
        await _handle_fs_back(query)
    elif data.startswith("fs_copy_"):
        await _handle_fs_copy(query, int(data[8:]))
    elif data.startswith("fs_send_"):
        await _ensure_session_loaded()
        await _handle_fs_send(query, int(data[8:]))
    elif data.startswith("fs_cancel_"):
        await _handle_fs_cancel(query, int(data[10:]))

    # ── Legacy Gate 1 (auto-dispatch cards) ──────────────────────────────────
    elif data.startswith("g1_approve_"):
        await _handle_approve(query, int(data.split("_")[-1]))
    elif data.startswith("g1_boost_"):
        await _handle_boost(query, int(data.split("_")[-1]))
    elif data.startswith("g1_skip_"):
        await _handle_skip(query, int(data.split("_")[-1]), "skipped")
    elif data.startswith("boost_send_"):
        await _handle_boost_send(query, int(data.split("_")[-1]))
    elif data.startswith("boost_tailor_"):
        await _handle_boost_tailor(query, int(data.split("_")[-1]))

    # ── Settings / menu ───────────────────────────────────────────────────────
    elif data.startswith("cfg_src_"):
        src = data[8:]
        current, days, max_m = _settings_state()
        if src == "all": current = list(ALL_SOURCES)
        elif src == "none": current = []
        elif src in current: current = [s for s in current if s != src]
        else: current.append(src)
        rc.set_nested("scraping.active_sources", current)
        await query.edit_message_reply_markup(reply_markup=settings_keyboard(current, days, max_m))
    elif data.startswith("cfg_days_"):
        days = int(data.split("_")[-1])
        rc.set_nested("scraping.days_back", days)
        current, _, max_m = _settings_state()
        await query.edit_message_reply_markup(reply_markup=settings_keyboard(current, days, max_m))
    elif data.startswith("cfg_max_"):
        max_m = int(data.split("_")[-1])
        rc.set_nested("filtering.max_matches_per_run", max_m)
        current, days, _ = _settings_state()
        await query.edit_message_reply_markup(reply_markup=settings_keyboard(current, days, max_m))
    elif data == "cfg_noop":
        pass
    elif data == "cmd_find":
        await query.edit_message_text("Starting pipeline... (Almaty)")
        asyncio.create_task(_run_pipeline_and_report(ctx.bot, query.message.message_id, location_filter="almaty"))
    elif data == "cmd_health":
        await query.edit_message_text(_health_text(), reply_markup=main_menu_keyboard())
    elif data == "cmd_queue":
        async with AsyncSessionLocal() as s:
            waiting = await s.scalar(select(func.count()).select_from(Match).where(Match.status == "waiting"))
        await query.edit_message_text(f"Queue: {waiting} waiting.", reply_markup=main_menu_keyboard())
    elif data == "cmd_stats":
        await query.edit_message_text("Use /stats for full stats.", reply_markup=main_menu_keyboard())
    elif data == "cmd_settings":
        sources, days, max_m = _settings_state()
        await query.edit_message_text("Settings:", reply_markup=settings_keyboard(sources, days, max_m))
    elif data == "cmd_logs":
        await query.edit_message_text(await _logs_text(), reply_markup=main_menu_keyboard())
    elif data == "cmd_back":
        await query.edit_message_text("Job Agent online.", reply_markup=main_menu_keyboard())


# ── Handler registration ──────────────────────────────────────────────────────

def register_handlers(app: Application) -> None:
    global _app
    _app = app

    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("find",    cmd_find))
    app.add_handler(CommandHandler("findall", cmd_findall))
    app.add_handler(CommandHandler("queue",   cmd_queue))
    app.add_handler(CommandHandler("stats",   cmd_stats))
    app.add_handler(CommandHandler("health",  cmd_health))
    app.add_handler(CommandHandler("logs",    cmd_logs))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("set",     cmd_set))
    app.add_handler(CommandHandler("rejected", cmd_rejected))
    app.add_handler(CommandHandler("errors",  cmd_errors))
    app.add_handler(CommandHandler("seniors", cmd_seniors))
    app.add_handler(CommandHandler("export",  cmd_export))
    app.add_handler(CommandHandler("stop",    cmd_stop))
    app.add_handler(CommandHandler("resume",  cmd_resume))
    app.add_handler(CommandHandler("config",  cmd_config))
    app.add_handler(CommandHandler("cv",      cmd_cv))
    app.add_handler(CommandHandler("setcv",   cmd_setcv))
    app.add_handler(CommandHandler("help",    cmd_help))
    app.add_handler(CallbackQueryHandler(gate1_callback))
    app.add_handler(MessageHandler(filters.Document.PDF, handle_pdf_upload))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
