# modules/telegram.py
import os
import asyncio
from typing import Dict, List
from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse
from telethon import TelegramClient, errors
from telethon.sessions import StringSession

API_ID = int(os.environ.get("TELEGRAM_API_ID", 0))
API_HASH = os.environ.get("TELEGRAM_API_HASH", "")
SESSION_STRING = os.environ.get("TELEGRAM_SESSION_STRING", "")

telegram_client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH,
                                 connection_retries=5, retry_delay=3)

files_cache: Dict[int, List[dict]] = {}
router = APIRouter(prefix="/telegram", tags=["telegram"])

async def start_client():
    if API_ID and API_HASH and SESSION_STRING and not telegram_client.is_connected():
        await telegram_client.start()
        me = await telegram_client.get_me()
        print(f"✅ Telegram client started as: {me.first_name}")

async def shutdown_client():
    if telegram_client.is_connected():
        await telegram_client.disconnect()

async def load_chat_files(chat_id: int, limit: int = 500) -> List[dict]:
    if chat_id in files_cache:
        return files_cache[chat_id]

    files = []
    try:
        print(f"🔄 Scanning chat {chat_id}...")
        async for message in telegram_client.iter_messages(chat_id, limit=limit):
            if message.media:
                file = message.file
                if file:
                    name = file.name if file.name else f"file_{message.id}"
                    size = file.size
                    mime = file.mime_type or ""
                    ext = name.split('.')[-1].lower() if '.' in name else ''
                    if mime.startswith('video/') or ext in ('mp4','mkv','avi','mov','webm','flv'):
                        mtype = "video"
                    elif mime.startswith('audio/') or ext in ('mp3','m4a','wav','ogg','flac'):
                        mtype = "audio"
                    elif mime.startswith('image/') or ext in ('jpg','jpeg','png','gif','webp'):
                        mtype = "image"
                    else:
                        mtype = "document"
                    
                    files.append({
                        "id": message.id,
                        "name": name,
                        "size": size,
                        "media_type": mtype,
                        "file_id": str(file.id),
                        "duration": getattr(file, 'duration', None),
                        "width": getattr(file, 'width', None),
                        "height": getattr(file, 'height', None),
                        "date": message.date.timestamp() if message.date else None,
                    })
        files.reverse()
        files_cache[chat_id] = files
        print(f"✅ Found {len(files)} files from chat {chat_id}")
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
    return files

@router.get("/health")
async def health():
    return {
        "status": "healthy",
        "client_connected": telegram_client.is_connected(),
        "has_session": bool(SESSION_STRING),
    }

@router.get("/dialogs")
async def get_dialogs():
    if not telegram_client.is_connected():
        raise HTTPException(503, "Telegram client not ready")
    dialogs = []
    async for dialog in telegram_client.iter_dialogs():
        chat = dialog.entity
        if hasattr(chat, 'broadcast') and chat.broadcast or hasattr(chat, 'megagroup') and chat.megagroup or hasattr(chat, 'group') and chat.group:
            dialogs.append({
                "id": chat.id,
                "name": chat.title,
                "type": "channel" if getattr(chat, 'broadcast', False) else "group",
                "unread_count": getattr(dialog, 'unread_count', 0),
            })
    return {"dialogs": dialogs, "total": len(dialogs)}

@router.get("/chat/{chat_id}/files")
async def get_chat_files(chat_id: int, limit: int = 500):
    if not telegram_client.is_connected():
        raise HTTPException(503, "Telegram client not ready")
    files = await load_chat_files(chat_id, limit=limit)
    return {"files": files, "total": len(files), "chat_id": chat_id}

@router.get("/stream/{chat_id}/{message_id}")
async def stream_file(chat_id: int, message_id: int):
    if not telegram_client.is_connected():
        raise HTTPException(503, "Telegram client not ready")
    try:
        message = await telegram_client.get_messages(chat_id, ids=message_id)
        if not message or not message.media:
            raise HTTPException(404, "Media not found")
        direct_url = await telegram_client.get_direct_download_link(message.media)
        if not direct_url:
            raise HTTPException(500, "Cannot generate direct link")
        return RedirectResponse(url=direct_url, status_code=302)
    except errors.RPCError as e:
        raise HTTPException(500, f"Telegram error: {str(e)}")
    except Exception as e:
        raise HTTPException(500, f"Unexpected error: {str(e)}")

@router.get("/download/{chat_id}/{message_id}")
async def download_file(chat_id: int, message_id: int):
    return await stream_file(chat_id, message_id)