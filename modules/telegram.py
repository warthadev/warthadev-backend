# modules/telegram.py
import os
import asyncio
from typing import Dict, List, Optional
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse, Response
from telethon import TelegramClient, errors
from telethon.sessions import StringSession
from telethon.tl.types import InputMessagesFilterDocument

API_ID = int(os.environ.get("TELEGRAM_API_ID", 0))
API_HASH = os.environ.get("TELEGRAM_API_HASH", "")
SESSION_STRING = os.environ.get("TELEGRAM_SESSION_STRING", "")

if not API_ID or not API_HASH or not SESSION_STRING:
    print("⚠️ WARNING: TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_SESSION_STRING must be set")

telegram_client = TelegramClient(
    StringSession(SESSION_STRING),
    API_ID,
    API_HASH,
    connection_retries=5,
    retry_delay=3,
)

files_cache: Dict[int, List[dict]] = {}
router = APIRouter(prefix="/telegram", tags=["telegram"])


async def start_client():
    if API_ID and API_HASH and SESSION_STRING and not telegram_client.is_connected():
        await telegram_client.start()
        me = await telegram_client.get_me()
        print(f"✅ Telegram client started as: {me.first_name} ({me.id})")


async def shutdown_client():
    if telegram_client.is_connected():
        await telegram_client.disconnect()
        print("Telegram client disconnected")


def format_file_size(bytes_size: int) -> str:
    if bytes_size < 1024:
        return f"{bytes_size} B"
    elif bytes_size < 1024 * 1024:
        return f"{bytes_size / 1024:.1f} KB"
    elif bytes_size < 1024 * 1024 * 1024:
        return f"{bytes_size / (1024 * 1024):.1f} MB"
    else:
        return f"{bytes_size / (1024 * 1024 * 1024):.2f} GB"


async def load_chat_files(chat_id: int, limit: int = 500) -> List[dict]:
    if chat_id in files_cache:
        return files_cache[chat_id]

    files = []
    try:
        print(f"🔄 Scanning chat {chat_id} for media files...")
        async for message in telegram_client.iter_messages(
            chat_id,
            limit=limit,
            filter=InputMessagesFilterDocument
        ):
            if message.media and message.file:
                file = message.file
                name = file.name if file.name else f"file_{message.id}"
                size = file.size or 0
                mime = file.mime_type or ""
                ext = name.split('.')[-1].lower() if '.' in name else ''

                if mime.startswith('video/') or ext in ('mp4', 'mkv', 'avi', 'mov', 'webm', 'flv', '3gp'):
                    media_type = "video"
                elif mime.startswith('audio/') or ext in ('mp3', 'm4a', 'wav', 'ogg', 'flac', 'aac'):
                    media_type = "audio"
                elif mime.startswith('image/') or ext in ('jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp'):
                    media_type = "image"
                else:
                    media_type = "document"

                files.append({
                    "id": message.id,
                    "name": name,
                    "size": size,
                    "size_formatted": format_file_size(size),
                    "media_type": media_type,
                    "file_id": str(file.id),
                    "duration": getattr(file, 'duration', None),
                    "width": getattr(file, 'width', None),
                    "height": getattr(file, 'height', None),
                    "date": message.date.timestamp() if message.date else None,
                    "caption": message.text if message.text else None,
                })
        files_cache[chat_id] = files
        print(f"✅ Found {len(files)} media files from chat {chat_id}")
    except Exception as e:
        print(f"Error loading files: {e}")
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
        chat_type = None
        if hasattr(chat, 'broadcast') and chat.broadcast:
            chat_type = "channel"
        elif hasattr(chat, 'megagroup') and chat.megagroup:
            chat_type = "group"
        elif hasattr(chat, 'group') and chat.group:
            chat_type = "group"
        else:
            continue
        dialogs.append({
            "id": chat.id,
            "name": chat.title,
            "type": chat_type,
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
async def stream_file(request: Request, chat_id: int, message_id: int):
    """
    Stream file dengan dukungan range (seek) menggunakan iter_download dari Telethon.
    Hanya mengambil chunk yang diminta oleh klien, menghemat bandwidth server.
    """
    if not telegram_client.is_connected():
        raise HTTPException(503, "Telegram client not ready")

    try:
        msg = await telegram_client.get_messages(chat_id, ids=message_id)
        if not msg or not msg.media:
            raise HTTPException(404, "Media not found")

        # Informasi file
        file_size = msg.file.size if msg.file else 0
        mime_type = msg.file.mime_type if msg.file else "application/octet-stream"
        file_name = msg.file.name if msg.file and msg.file.name else f"file_{message_id}"

        # Header range dari browser
        range_header = request.headers.get("range")
        print(f"📺 Stream request: chat={chat_id}, msg={message_id}, range={range_header}")

        # Jika tidak ada range, kirim header awal saja (browser akan minta range selanjutnya)
        if not range_header:
            return Response(
                status_code=200,
                headers={
                    "Accept-Ranges": "bytes",
                    "Content-Length": str(file_size),
                    "Content-Type": mime_type,
                    "Content-Disposition": f'inline; filename="{file_name}"'
                }
            )

        # Parse range
        range_val = range_header.replace("bytes=", "")
        parts = range_val.split("-")
        start_byte = int(parts[0])
        end_byte = int(parts[1]) if parts[1] else file_size - 1

        if start_byte >= file_size or end_byte >= file_size:
            return Response(status_code=416)

        requested_length = end_byte - start_byte + 1
        print(f"🎯 Seeking: {start_byte}-{end_byte} (size {requested_length})")

        # Gunakan iter_download dengan offset (dalam bytes, bukan chunk index)
        # Telethon mendukung offset dan request_size dalam bytes
        async def generate_chunk():
            downloaded = 0
            async for chunk in telegram_client.iter_download(
                msg.media,
                offset=start_byte,
                request_size=requested_length,  # minta hanya yang diperlukan
            ):
                # Potong jika chunk melebihi yang diminta
                if downloaded + len(chunk) > requested_length:
                    chunk = chunk[:requested_length - downloaded]
                yield chunk
                downloaded += len(chunk)
                if downloaded >= requested_length:
                    break

        return StreamingResponse(
            generate_chunk(),
            status_code=206,
            media_type=mime_type,
            headers={
                "Content-Range": f"bytes {start_byte}-{end_byte}/{file_size}",
                "Accept-Ranges": "bytes",
                "Content-Length": str(requested_length),
                "Cache-Control": "no-cache",
            }
        )

    except errors.RPCError as e:
        print(f"RPC error: {e}")
        raise HTTPException(500, f"Telegram RPC error: {str(e)}")
    except Exception as e:
        print(f"Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(500, f"Unexpected error: {str(e)}")


@router.get("/download/{chat_id}/{message_id}")
async def download_file(chat_id: int, message_id: int):
    """
    Download full file (bisa juga menggunakan streaming penuh).
    """
    if not telegram_client.is_connected():
        raise HTTPException(503, "Telegram client not ready")

    try:
        msg = await telegram_client.get_messages(chat_id, ids=message_id)
        if not msg or not msg.media:
            raise HTTPException(404, "Media not found")

        file_name = msg.file.name if msg.file and msg.file.name else f"file_{message_id}"
        mime_type = msg.file.mime_type if msg.file else "application/octet-stream"

        async def generate_full():
            async for chunk in telegram_client.iter_download(msg.media):
                yield chunk

        return StreamingResponse(
            generate_full(),
            media_type=mime_type,
            headers={"Content-Disposition": f'attachment; filename="{file_name}"'}
        )
    except Exception as e:
        raise HTTPException(500, f"Download error: {str(e)}")