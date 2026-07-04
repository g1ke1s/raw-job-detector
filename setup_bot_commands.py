import asyncio
import os
from dotenv import load_dotenv

load_dotenv()

async def main():
    from telegram import Bot, BotCommand
    bot = Bot(token=os.environ["TELEGRAM_BOT_TOKEN"])
    commands = [
        BotCommand("find",     "Scrape Almaty jobs, open browse session"),
        BotCommand("findall",  "Scrape all Kazakhstan, open browse session"),
        BotCommand("queue",    "Waiting and in-flight count"),
        BotCommand("seniors",  "Senior-tagged jobs (if no active session)"),
        BotCommand("rejected", "System-rejected close calls"),
        BotCommand("export",   "Download all matches as CSV"),
        BotCommand("cv",       "CV status and tailoring"),
        BotCommand("setcv",    "Paste CV as plain text (fallback)"),
        BotCommand("settings", "Toggle sources, days, max matches"),
        BotCommand("set",      "Edit a config value — e.g. /set days 3"),
        BotCommand("health",   "LLM providers and pipeline status"),
        BotCommand("stats",    "Application statistics"),
        BotCommand("logs",     "Recent events and errors"),
        BotCommand("stop",     "Pause scraping"),
        BotCommand("resume",   "Resume scraping"),
        BotCommand("help",     "All commands"),
    ]
    await bot.set_my_commands(commands)
    print(f"Registered {len(commands)} bot commands.")
    await bot.close()

if __name__ == "__main__":
    asyncio.run(main())
