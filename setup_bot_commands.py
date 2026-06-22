import asyncio
import os
from dotenv import load_dotenv

load_dotenv()

async def main():
    from telegram import Bot, BotCommand
    bot = Bot(token=os.environ["TELEGRAM_BOT_TOKEN"])
    commands = [
        BotCommand("find", "Scrape jobs in Almaty"),
        BotCommand("findall", "Scrape jobs across all Kazakhstan"),
        BotCommand("show", "Manually send next waiting job"),
        BotCommand("queue", "Waiting / in-flight count"),
        BotCommand("seniors", "Senior-tagged jobs (not auto-sent)"),
        BotCommand("rejected", "System-rejected close calls"),
        BotCommand("export", "Download all matches as CSV"),
        BotCommand("cv", "CV status and tailor button"),
        BotCommand("setcv", "Paste CV as plain text (if PDF fails)"),
        BotCommand("settings", "Toggle sources / days / max matches"),
        BotCommand("set", "Edit runtime config (e.g. /set days 3)"),
        BotCommand("config", "Show config.yaml"),
        BotCommand("stats", "Pipeline statistics"),
        BotCommand("health", "LLM provider status"),
        BotCommand("logs", "Recent events"),
        BotCommand("errors", "Scraper errors"),
        BotCommand("stop", "Pause the pipeline"),
        BotCommand("resume", "Resume the pipeline"),
        BotCommand("help", "All commands by category"),
        BotCommand("start", "Show menu"),
    ]
    await bot.set_my_commands(commands)
    print("Bot commands registered.")
    await bot.close()

if __name__ == "__main__":
    asyncio.run(main())
