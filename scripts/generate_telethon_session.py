# generate_telethon_session.py
import asyncio
from telethon import TelegramClient
from telethon.sessions import StringSession

API_ID = int(input("Enter API ID: "))
API_HASH = input("Enter API HASH: ")

async def main():
    async with TelegramClient(StringSession(), API_ID, API_HASH) as client:
        await client.start()
        session_str = client.session.save()
        print("\n=== COPY THIS SESSION STRING ===\n")
        print(session_str)
        print("\n================================\n")

asyncio.run(main())