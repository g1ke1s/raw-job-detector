"""Run once to register bot command menu with Telegram."""
import asyncio
import os
from dotenv import load_dotenv

load_dotenv()

async def main():
    from telegram import Bot, BotCommand
    bot = Bot(token=os.environ["TELEGRAM_BOT_TOKEN"])
    commands = [
        BotCommand("start", "Show menu"),
        BotCommand("find", "Search jobs in Almaty"),
        BotCommand("findall", "Search jobs across all Kazakhstan"),
        BotCommand("rejected", "Show last 20 bot-filtered close calls"),
        BotCommand("errors", "Show last 20 scraper errors"),
        BotCommand("show", "Send next waiting job (any source)"),
        BotCommand("show_hh", "Send next waiting hh.kz job"),
        BotCommand("show_tg", "Send next waiting Telegram job"),
        BotCommand("show_linkedin", "Send next waiting LinkedIn job"),
        BotCommand("show_remote", "Send next waiting remote job"),
        BotCommand("queue", "Show pending approvals count"),
        BotCommand("stats", "Run statistics"),
        BotCommand("health", "LLM provider status"),
        BotCommand("logs", "Recent events"),
        BotCommand("settings", "Toggle sources / days / max matches"),
        BotCommand("config", "Show current config.yaml"),
        BotCommand("set", "Edit config value (e.g. /set days 3)"),
        BotCommand("setcover", "Save cover letter template"),
        BotCommand("covers", "List saved cover templates"),
        BotCommand("stop", "Pause the pipeline"),
        BotCommand("resume", "Resume the pipeline"),
    ]
    await bot.set_my_commands(commands)
    print("Bot commands registered.")
    await bot.close()

if __name__ == "__main__":
    asyncio.run(main())
