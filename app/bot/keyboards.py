from telegram import InlineKeyboardButton, InlineKeyboardMarkup

ALL_SOURCES = ["hh", "linkedin", "telegram", "remote"]


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔍 Find Jobs", callback_data="cmd_find"),
            InlineKeyboardButton("📋 Queue", callback_data="cmd_queue"),
        ],
        [
            InlineKeyboardButton("⚙️ Settings", callback_data="cmd_settings"),
            InlineKeyboardButton("❤️ Health", callback_data="cmd_health"),
        ],
        [
            InlineKeyboardButton("📊 Stats", callback_data="cmd_stats"),
            InlineKeyboardButton("📜 Logs", callback_data="cmd_logs"),
        ],
    ])


def settings_keyboard(current_sources: list, days: int, max_matches: int) -> InlineKeyboardMarkup:
    source_buttons = []
    for src in ALL_SOURCES:
        active = src in current_sources
        label = f"{'✅' if active else '☐'} {src.upper()}"
        source_buttons.append(
            InlineKeyboardButton(label, callback_data=f"cfg_src_{src}")
        )

    rows = [source_buttons[i:i+2] for i in range(0, len(source_buttons), 2)]

    rows.append([
        InlineKeyboardButton("✅ All sources", callback_data="cfg_src_all"),
        InlineKeyboardButton("☐ Clear all", callback_data="cfg_src_none"),
    ])

    rows.append([
        InlineKeyboardButton(f"📅 Days back: {days}", callback_data="cfg_noop"),
    ])
    rows.append([
        InlineKeyboardButton("1d", callback_data="cfg_days_1"),
        InlineKeyboardButton("3d", callback_data="cfg_days_3"),
        InlineKeyboardButton("7d", callback_data="cfg_days_7"),
        InlineKeyboardButton("14d", callback_data="cfg_days_14"),
    ])

    rows.append([
        InlineKeyboardButton(f"🎯 Max matches: {max_matches}", callback_data="cfg_noop"),
    ])
    rows.append([
        InlineKeyboardButton("5", callback_data="cfg_max_5"),
        InlineKeyboardButton("10", callback_data="cfg_max_10"),
        InlineKeyboardButton("20", callback_data="cfg_max_20"),
        InlineKeyboardButton("50", callback_data="cfg_max_50"),
    ])

    rows.append([InlineKeyboardButton("📄 View config.yaml", callback_data="cfg_show_config")])
    rows.append([InlineKeyboardButton("« Back", callback_data="cmd_back")])
    return InlineKeyboardMarkup(rows)
