# modules/telegram.py
import os
from typing import Dict, List
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse, Response
from telethon import TelegramClient, errors
from telethon.sessions import StringSession
from telethon.tl.types import InputMessagesFilterDocument

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
        print(f"Telegram client started as: {me.first_name}")


async def shutdown_client():
    if telegram_client.is_connected():
        await telegram_client.disconnect()


def format_file_size(s: int) -> str:
    if s < 1024:
        return f"{s} B"
    if s < 1024 ** 2:
        return f"{s / 1024:.1f} KB"
    if s < 1024 ** 3:
        return f"{s / 1024 ** 2:.1f} MB"
    return f"{s / 1024 ** 3:.2f} GB"


async def load_chat_files(chat_id: int, limit: int = 500) -> List[dict]:
    if chat_id in files_cache:
        return files_cache[chat_id]
    files = []
    try:
        async for msg in telegram_client.iter_messages(chat_id, limit=limit, filter=InputMessagesFilterDocument):
            if msg.media and msg.file:
                f = msg.file
                name = f.name or f"file_{msg.id}"
                ext = name.split('.')[-1].lower()
                if f.mime_type and f.mime_type.startswith('video/') or ext in ('mp4', 'mkv', 'avi', 'mov'):
                    mtype = "video"
                elif f.mime_type and f.mime_type.startswith('audio/') or ext in ('mp3', 'm4a', 'wav'):
                    mtype = "audio"
                elif f.mime_type and f.mime_type.startswith('image/') or ext in ('jpg', 'png', 'gif'):
                    mtype = "image"
                else:
                    mtype = "document"
                files.append({
                    "id": msg.id,
                    "name": name,
                    "size": f.size,
                    "size_formatted": format_file_size(f.size),
                    "media_type": mtype,
                    "file_id": str(f.id),
                    "duration": getattr(f, 'duration', None),
                    "width": getattr(f, 'width', None),
                    "height": getattr(f, 'height', None),
                    "date": msg.date.timestamp() if msg.date else None,
                    "caption": msg.text
                })
        files_cache[chat_id] = files
        print(f"Found {len(files)} files in chat {chat_id}")
    except Exception as e:
        print(f"Error loading files: {e}")
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
        raise HTTPException(503, "Client not ready")
    dialogs = []
    async for d in telegram_client.iter_dialogs():
        chat = d.entity
        if hasattr(chat, 'broadcast') and chat.broadcast:
            typ = "channel"
        elif hasattr(chat, 'megagroup') and chat.megagroup or hasattr(chat, 'group') and chat.group:
            typ = "group"
        else:
            continue
        dialogs.append({
            "id": chat.id,
            "name": chat.title,
            "type": typ,
            "unread_count": getattr(d, 'unread_count', 0)
        })
    return {"dialogs": dialogs, "total": len(dialogs)}


@router.get("/chat/{chat_id}/files")
async def get_chat_files(chat_id: int, limit: int = 500):
    if not telegram_client.is_connected():
        raise HTTPException(503, "Client not ready")
    files = await load_chat_files(chat_id, limit=limit)
    return {"files": files, "total": len(files), "chat_id": chat_id}


@router.get("/stream/{chat_id}/{message_id}")
async def stream_file(request: Request, chat_id: int, message_id: int):
    if not telegram_client.is_connected():
        raise HTTPException(503, "Client not ready")
    try:
        msg = await telegram_client.get_messages(chat_id, ids=message_id)
        if not msg or not msg.media:
            raise HTTPException(404, "Media not found")
        file_size = msg.file.size
        mime = msg.file.mime_type or "video/mp4"
        fname = msg.file.name or f"file_{message_id}"
        range_header = request.headers.get("range")
        if not range_header:
            return Response(
                headers={
                    "Accept-Ranges": "bytes",
                    "Content-Length": str(file_size),
                    "Content-Type": mime,
                    "Content-Disposition": f'inline; filename="{fname}"',
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Expose-Headers": "Content-Range, Accept-Ranges, Content-Length",
                }
            )
        range_val = range_header.replace("bytes=", "")
        start_str, end_str = range_val.split("-")
        start = int(start_str)
        end = int(end_str) if end_str else file_size - 1
        length = end - start + 1
        if start >= file_size or end >= file_size:
            return Response(status_code=416)

        async def generate():
            async for chunk in telegram_client.iter_download(msg.media, offset=start, request_size=length):
                yield chunk[:length]
                break

        return StreamingResponse(
            generate(),
            status_code=206,
            media_type=mime,
            headers={
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                "Accept-Ranges": "bytes",
                "Content-Length": str(length),
                "Cache-Control": "no-cache",
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Expose-Headers": "Content-Range, Accept-Ranges, Content-Length",
            }
        )
    except errors.RPCError as e:
        raise HTTPException(500, f"Telegram error: {str(e)}")
    except Exception as e:
        raise HTTPException(500, f"Server error: {str(e)}")


@router.get("/download/{chat_id}/{message_id}")
async def download_file(chat_id: int, message_id: int):
    if not telegram_client.is_connected():
        raise HTTPException(503, "Client not ready")
    msg = await telegram_client.get_messages(chat_id, ids=message_id)
    if not msg or not msg.media:
        raise HTTPException(404, "Media not found")
    fname = msg.file.name or f"file_{message_id}"
    mime = msg.file.mime_type or "application/octet-stream"

    async def gen():
        async for chunk in telegram_client.iter_download(msg.media):
            yield chunk

    return StreamingResponse(
        gen(),
        media_type=mime,
        headers={"Content-Disposition": f'attachment; filename="{fname}"'}
    )