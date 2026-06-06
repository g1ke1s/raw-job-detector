"""
Run once LOCALLY (not in Docker) to generate TELEGRAM_SESSION_STRING.
Paste the output into .env.

Usage:
    pip install telethon python-dotenv
    python generate_session.py
"""
import asyncio
import os

from telethon import TelegramClient
from telethon.sessions import StringSession


async def main():
    api_id = int(input("TELEGRAM_API_ID: ").strip())
    api_hash = input("TELEGRAM_API_HASH: ").strip()
    phone = input("TELEGRAM_PHONE (+7...): ").strip()

    client = TelegramClient(
        StringSession(),
        api_id,
        api_hash,
        lang_code="ru",
        system_lang_code="ru-RU",
    )
    await client.start(phone=phone)
    session_string = client.session.save()
    print("\nAdd this to your .env:")
    print(f"TELEGRAM_SESSION_STRING={session_string}")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
