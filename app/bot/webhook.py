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
from app.db.models import Match, Decision, RunLog, EventLog
from app.bot.keyboards import main_menu_keyboard, settings_keyboard, ALL_SOURCES
from app.monitoring.events import log_event
from app.runtime_config import rc

log = logging.getLogger(__name__)

_pending_cv_text: set[int] = set()
# PDF bytes waiting for user confirmation before being sent to recruiter
_pending_pdfs: dict[int, bytes] = {}
_app = None

# /find overview state — tracks the most recent find run so decisions update the overview
_find_overview_msg_id: int | None = None
_find_overview_matches: list[tuple[int, str, str, float]] = []  # (match_id, title, company, score)
_find_overview_decisions: dict[int, str] = {}  # match_id → "✅" / "🚀" / "❌"

# Imported at module level — used in handle_message for /setcv text paste
from app.cv.ingest import _SYSTEM as _SYSTEM_CV_PARSE


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


def _build_find_overview_keyboard():
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    rows = []
    for i, (mid, title, company, score) in enumerate(_find_overview_matches):
        icon = _find_overview_decisions.get(mid)
        if icon:
            label = f"{icon} {title[:30]} · {company[:20]}"
            rows.append([InlineKeyboardButton(label, callback_data=f"find_noop_{mid}")])
        else:
            pct = int(score * 100)
            label = f"{i + 1}. {title[:25]} · {company[:15]} · {pct}% 👁"
            rows.append([InlineKeyboardButton(label, callback_data=f"find_view_{mid}")])
    return InlineKeyboardMarkup(rows)


async def _update_find_overview(match_id: int, icon: str) -> None:
    """Edit the /find overview message to reflect a Gate 1 decision."""
    if _find_overview_msg_id is None:
        return
    if not any(mid == match_id for mid, *_ in _find_overview_matches):
        return
    _find_overview_decisions[match_id] = icon
    try:
        await _app.bot.edit_message_reply_markup(
            chat_id=settings.telegram_chat_id,
            message_id=_find_overview_msg_id,
            reply_markup=_build_find_overview_keyboard(),
        )
    except Exception:
        pass  # message >48h old or other Telegram error — silently ignore


async def _run_pipeline_and_report(bot, msg_id: int, location_filter: str = "almaty") -> None:
    global _find_overview_msg_id, _find_overview_matches, _find_overview_decisions

    from app.queue.processor import run_pipeline
    start_ts = datetime.utcnow()
    result = await run_pipeline(location_filter=location_filter)

    if "error" in result:
        await bot.edit_message_text(
            chat_id=settings.telegram_chat_id, message_id=msg_id,
            text=f"Pipeline error:\n{result['error']}",
        )
        return

    loc_tag = "" if location_filter == "almaty" else f" [{location_filter.upper()}]"

    if not result.get("enqueued"):
        await bot.edit_message_text(
            chat_id=settings.telegram_chat_id, message_id=msg_id,
            text=f"Done{loc_tag}. Scraped {result['scraped']}, nothing new.",
        )
        return

    # Fetch matches created during this run (waiting, non-senior) ordered by score
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
            text=f"Done{loc_tag}. Scraped {result['scraped']}, nothing new.",
        )
        return

    _find_overview_matches = [
        (m.id, _clean(m.title or ""), _clean(m.company or ""), m.rule_score or 0.0)
        for m in new_matches
    ]
    _find_overview_decisions = {}
    _find_overview_msg_id = msg_id

    await bot.edit_message_text(
        chat_id=settings.telegram_chat_id,
        message_id=msg_id,
        text=f"Found {len(new_matches)} jobs:",
        reply_markup=_build_find_overview_keyboard(),
    )


def _notify_dispatcher() -> None:
    """Kick the dispatcher to check the queue immediately after a decision."""
    from app.queue.dispatcher import notify_decision
    notify_decision()


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
        runs = (await s.execute(
            select(RunLog).order_by(RunLog.started_at.desc()).limit(5)
        )).scalars().all()
        approved = await s.scalar(select(func.count()).select_from(Decision).where(Decision.verdict == "approved"))
        rejected = await s.scalar(select(func.count()).select_from(Decision).where(Decision.verdict == "rejected"))
        night_total = await s.scalar(
            select(func.count()).select_from(Match).where(Match.night_run == True)
        )
        night_waiting = await s.scalar(
            select(func.count()).select_from(Match).where(
                Match.night_run == True, Match.status == "waiting"
            )
        )
        night_approved = await s.scalar(
            select(func.count()).select_from(Match).where(
                Match.night_run == True, Match.status == "approved"
            )
        )
        from sqlalchemy import text
        src_rows = (await s.execute(
            text("SELECT source, count(*) FROM matches GROUP BY source")
        )).fetchall()

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
        lines.append(
            f"  {r.started_at.strftime('%m-%d %H:%M')}{night} [{r.status}]{dur} "
            f"scraped={r.scraped} enqueued={r.enqueued} llm={r.llm_calls}"
        )
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
        "roles": ("scraping.roles", lambda v: [r.strip() for r in v.split(",")]),
        "sources": ("scraping.active_sources", lambda v: [s.strip() for s in v.split(",")]),
        "days": ("scraping.days_back", int),
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


# ── Gate 1 handlers ───────────────────────────────────────────────────────────

async def _run_apply_downstream(query, match_id: int, source: str, title: str,
                                company: str, url: str, handle: str | None) -> None:
    """Common downstream logic after Approve or Boost sets a match to approved."""
    text = f"✅ {title} · {company}"
    if url:
        text += f"\n🔗 {url}"
    await query.edit_message_text(text)
    _notify_dispatcher()


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
        source = match.source
        title = _clean(match.title or "")
        company = _clean(match.company or "")
        url = match.url or ""
        handle = match.recruiter_handle

    await log_event("gate1_approved", f"match_id={match_id} source={source}")
    await _update_find_overview(match_id, "✅")
    await _run_apply_downstream(query, match_id, source, title, company, url, handle)


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
        source = match.source
        title = _clean(match.title or "")
        company = _clean(match.company or "")
        url = match.url or ""
        handle = match.recruiter_handle

    await log_event("gate1_boosted", f"match_id={match_id} source={source}")
    await _update_find_overview(match_id, "🚀")

    # Background: generate cover letter + tailor CV, then send preview with confirm button
    if _app:
        asyncio.create_task(_boost_generate_and_preview(match_id, title, company))

    await _run_apply_downstream(query, match_id, source, title, company, url, handle)


async def _boost_generate_and_preview(match_id: int, title: str, company: str) -> None:
    """
    Background task after Boost:
      1. Generate cover letter (LLM)
      2. Tailor + compile LaTeX CV (LLM + tectonic)
      3. Send preview to user with "📤 Send now" confirmation button
    """
    cover_text: str | None = None
    pdf_bytes: bytes | None = None
    tailor_status = ""

    try:
        from app.cv.cover import generate_cover
        cover_text = await generate_cover(match_id)
    except Exception as e:
        log.error("Boost cover generation failed for match %d: %s", match_id, e)

    try:
        from app.cv.tailor import tailor_and_render
        pdf_bytes, tailor_status = await tailor_and_render(match_id)
    except Exception as e:
        log.error("Boost CV tailor failed for match %d: %s", match_id, e)
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

    # Message 1: letter text only — zero extra content, user can select-all and copy on mobile
    await _app.bot.send_message(
        chat_id=settings.telegram_chat_id,
        text=cover_text[:4000],
    )

    # Message 2: job header + confirm button only
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    action_lines = [f"{title} · {company}"]
    if not pdf_bytes and tailor_status:
        action_lines.append(f"CV: {tailor_status[:80]}")
    action_text = "\n".join(action_lines)

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("📤 Send now", callback_data=f"boost_send_{match_id}"),
    ]])
    await _app.bot.send_message(
        chat_id=settings.telegram_chat_id,
        text=action_text,
        reply_markup=kb,
    )


async def _handle_boost_send(query, match_id: int) -> None:
    """Send cover letter + tailored CV to the recruiter via the correct channel."""
    await query.edit_message_reply_markup(reply_markup=None)  # remove button

    async with AsyncSessionLocal() as s:
        match = await s.get(Match, match_id)
        if not match:
            await query.answer("Match not found.", show_alert=True)
            return
        cover_text = match.cover_text or ""
        handle = match.recruiter_handle
        source = match.source
        url = match.url or ""
        title = _clean(match.title or "")
        company = _clean(match.company or "")

    pdf_bytes = _pending_pdfs.pop(match_id, None)

    if not cover_text:
        await _app.bot.send_message(
            chat_id=settings.telegram_chat_id,
            text="No cover letter available. Re-run Boost to regenerate.",
        )
        return

    if not handle:
        # HH, LinkedIn, or any source without a direct recruiter contact
        msg = f"No direct recruiter channel for this job. Apply manually at:\n{url}"
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
        await _app.bot.send_message(chat_id=settings.telegram_chat_id, text=msg)
        return

    filename = f"CV_{title[:30].replace(' ', '_')}_{match_id}.pdf"

    if handle.startswith("@"):
        from app.appliers.tg_sender import send_dm, send_dm_with_document
        if pdf_bytes:
            ok = await send_dm_with_document(handle, cover_text, pdf_bytes, filename)
        else:
            ok = await send_dm(handle, cover_text)
        result = f"DM sent to {handle}." if ok else f"DM failed to {handle}. Check /errors."
    else:
        # Email address
        from app.appliers.email_sender import send_email, send_email_with_pdf
        subject = f"Application: {title} at {company}"
        if pdf_bytes:
            ok = await send_email_with_pdf(handle, subject, cover_text, pdf_bytes, filename)
        else:
            ok = await send_email(handle, subject, cover_text)
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
    await _update_find_overview(match_id, "❌")
    _notify_dispatcher()


async def _handle_find_view(query, match_id: int) -> None:
    """Send a full job card when user taps [👁] in the /find overview."""
    async with AsyncSessionLocal() as s:
        match = await s.get(Match, match_id)

    if not match:
        await query.answer("Job no longer available.", show_alert=True)
        return

    if match.status in ("approved", "rejected"):
        icon = "✅" if match.status == "approved" else "❌"
        await query.answer(f"{icon} Already decided.", show_alert=False)
        return

    from app.queue.dispatcher import send_card_direct
    try:
        msg_id = await send_card_direct(match)
        async with AsyncSessionLocal() as s:
            m = await s.get(Match, match_id)
            if m and m.status == "waiting":
                m.status = "sent_to_user"
                m.telegram_message_id = msg_id
                m.updated_at = datetime.utcnow()
                await s.commit()
    except Exception as e:
        log.error("find_view error for match %d: %s", match_id, e)
        await query.answer("Error showing job card.", show_alert=True)


# ── CV upload / text-paste handlers ──────────────────────────────────────────

async def handle_pdf_upload(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    doc = update.message.document
    if not doc or not doc.file_name or not doc.file_name.lower().endswith(".pdf"):
        return
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
    if not _authorized(update):
        return
    user_id = update.message.from_user.id

    if user_id in _pending_cv_text:
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

    if data.startswith("g1_approve_"):
        await _handle_approve(query, int(data.split("_")[-1]))
    elif data.startswith("g1_boost_"):
        await _handle_boost(query, int(data.split("_")[-1]))
    elif data.startswith("g1_skip_"):
        await _handle_skip(query, int(data.split("_")[-1]), "skipped")
    elif data.startswith("find_view_"):
        await _handle_find_view(query, int(data.split("_")[-1]))
    elif data.startswith("find_noop_"):
        mid = int(data.split("_")[-1])
        icon = _find_overview_decisions.get(mid, "?")
        await query.answer(f"Already decided: {icon}", show_alert=False)
    elif data.startswith("boost_send_"):
        await _handle_boost_send(query, int(data.split("_")[-1]))
    elif data.startswith("boost_tailor_"):
        await _handle_boost_tailor(query, int(data.split("_")[-1]))
    elif data.startswith("cfg_src_"):
        src = data[8:]
        current, days, max_m = _settings_state()
        if src == "all":
            current = list(ALL_SOURCES)
        elif src == "none":
            current = []
        elif src in current:
            current = [s for s in current if s != src]
        else:
            current.append(src)
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


# ── Information commands ──────────────────────────────────────────────────────

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


async def cmd_show(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update): return
    from app.queue.dispatcher import send_card_direct

    async with AsyncSessionLocal() as s:
        total = await s.scalar(select(func.count()).select_from(Match).where(Match.status == "waiting"))
        next_match = await s.scalar(
            select(Match).where(Match.status == "waiting").order_by(Match.created_at)
        )

    if not next_match:
        await update.message.reply_text("No waiting jobs.")
        return

    try:
        msg_id = await send_card_direct(next_match)
    except Exception as e:
        log.error("show send error: %s", e)
        await update.message.reply_text(f"Error sending job: {e}")
        return

    async with AsyncSessionLocal() as s:
        m = await s.get(Match, next_match.id)
        m.status = "sent_to_user"
        m.telegram_message_id = msg_id
        m.updated_at = datetime.utcnow()
        await s.commit()

    await update.message.reply_text(f"Sent. {max(0, total - 1)} more waiting.")


async def cmd_seniors(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update): return
    async with AsyncSessionLocal() as s:
        matches = (await s.execute(
            select(Match)
            .where(Match.seniority == "senior")
            .order_by(Match.created_at.desc())
            .limit(20)
        )).scalars().all()
    if not matches:
        await update.message.reply_text("No senior-tagged jobs yet.")
        return
    lines = [f"Last {len(matches)} senior-level roles (hidden from auto-queue):\n"]
    for m in matches:
        pub_date = (m.extra or {}).get("pub_date", "")
        line = f"[{m.source.upper()}] {_clean(m.title or '?')}"
        if m.company:
            line += f" @ {_clean(m.company)}"
        if pub_date:
            line += f" | {pub_date}"
        if m.url:
            line += f"\n{m.url}"
        lines.append(line)
    text = "\n\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n...(truncated)"
    await update.message.reply_text(text)


async def cmd_export(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update): return
    import csv, io
    async with AsyncSessionLocal() as s:
        matches = (await s.execute(
            select(Match).order_by(Match.created_at.desc()).limit(1000)
        )).scalars().all()
        decisions_rows = (await s.execute(select(Decision))).scalars().all()
    decisions = {d.match_id: d for d in decisions_rows}

    rows = []
    for m in matches:
        dec = decisions.get(m.id)
        if m.status == "applied":
            status_label = "applied"
        elif dec and dec.verdict == "rejected" and dec.reason == "skipped":
            status_label = "rejected_user"
        elif m.status == "rejected":
            status_label = "rejected_user" if dec else "rejected_system"
        elif m.seniority == "senior":
            status_label = "senior_hidden"
        else:
            status_label = m.status
        # fit_assessment_json kept for historical records from before this field was removed
        fit = m.fit_assessment_json or {}
        rows.append({
            "date":          m.created_at.strftime("%Y-%m-%d") if m.created_at else "",
            "title":         m.title or "",
            "company":       m.company or "",
            "source":        m.source or "",
            "score":         round(m.rule_score or 0, 2),
            "seniority":     m.seniority or "",
            "status":        status_label,
            "fit_score":     fit.get("score", ""),
            "fit_blockers":  "; ".join(fit.get("blockers", [])),
            "url":           m.url or "",
            "pub_date":      (m.extra or {}).get("pub_date", ""),
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
    """Manual tailor button from /cv — renders and sends PDF directly to this chat."""
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
    await update.message.reply_text(
        "Paste your CV as plain text in the next message.\n"
        "I'll parse it immediately."
    )


async def cmd_cv(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update): return
    from app.cv.ingest import get_active_profile
    profile = await get_active_profile()
    if not profile:
        await update.message.reply_text(
            "No CV on file.\n\nSend me a PDF of your CV and I'll parse it automatically."
        )
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
            select(Match)
            .join(Decision, Decision.match_id == Match.id)
            .where(Decision.reason == "boost")
            .order_by(Match.updated_at.desc())
        )
    if last_boosted:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                f"🔧 Tailor CV for: {(last_boosted.title or '')[:40]}",
                callback_data=f"boost_tailor_{last_boosted.id}",
            )
        ]])
        await update.message.reply_text("\n".join(lines), reply_markup=kb)
    else:
        await update.message.reply_text("\n".join(lines))


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update): return
    text = (
        "Job Agent commands\n\n"
        "Search\n"
        "/find — scrape Almaty (hh.kz + LinkedIn + Telegram + YC)\n"
        "/findall — scrape all Kazakhstan\n"
        "/show — manually send next waiting job\n\n"
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
        "/resume — unpause\n\n"
        "Gate 1 buttons\n"
        "Approve — mark approved, show details, auto-advance\n"
        "Boost — approve + cover letter + tailored LaTeX CV, then confirm to send\n"
        "Skip — reject immediately, auto-advance"
    )
    await update.message.reply_text(text)


# ── Handler registration ──────────────────────────────────────────────────────

def register_handlers(app: Application) -> None:
    global _app
    _app = app

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("find", cmd_find))
    app.add_handler(CommandHandler("findall", cmd_findall))
    app.add_handler(CommandHandler("queue", cmd_queue))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("health", cmd_health))
    app.add_handler(CommandHandler("logs", cmd_logs))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("set", cmd_set))
    app.add_handler(CommandHandler("show", cmd_show))
    app.add_handler(CommandHandler("rejected", cmd_rejected))
    app.add_handler(CommandHandler("errors", cmd_errors))
    app.add_handler(CommandHandler("seniors", cmd_seniors))
    app.add_handler(CommandHandler("export", cmd_export))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("config", cmd_config))
    app.add_handler(CommandHandler("cv", cmd_cv))
    app.add_handler(CommandHandler("setcv", cmd_setcv))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CallbackQueryHandler(gate1_callback))
    app.add_handler(MessageHandler(filters.Document.PDF, handle_pdf_upload))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
