# modules/telegram.py
import os
from typing import Dict, Tuple, Optional, AsyncGenerator
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse, Response
from pyrogram import Client
from pyrogram.enums import ChatType, MessagesFilter
import asyncio

router = APIRouter(prefix="/telegram", tags=["telegram"])

SESSION_STRING = os.environ.get("TELEGRAM_SESSION_STRING", "")

if not SESSION_STRING:
    print("WARNING: TELEGRAM_SESSION_STRING not set!")

# Cache: message_id -> (chat_id, file_id, file_size, mime_type)
_file_cache: Dict[str, Tuple[int, str, int, str]] = {}

def make_client() -> Client:
    """Buat instance Pyrogram client"""
    return Client(
        "telegram_manager",
        session_string=SESSION_STRING,
        in_memory=True,
        no_updates=True,
    )

def chat_type_str(chat_type: ChatType) -> str:
    mapping = {
        ChatType.GROUP: "group",
        ChatType.SUPERGROUP: "supergroup",
        ChatType.CHANNEL: "channel",
        ChatType.PRIVATE: "private",
        ChatType.BOT: "bot",
    }
    return mapping.get(chat_type, str(chat_type).split(".")[-1].lower())

def detect_media_type(message) -> Optional[str]:
    """Deteksi tipe media dari message"""
    if message.video:
        return "video"
    if message.audio:
        return "audio"
    if message.voice:
        return "voice"
    if message.photo:
        return "image"
    if message.document:
        mime = (message.document.mime_type or "").lower()
        if mime.startswith("video/"):
            return "video"
        if mime.startswith("audio/"):
            return "audio"
        if mime.startswith("image/"):
            return "image"
        if mime in ("application/zip", "application/x-rar-compressed",
                    "application/x-7z-compressed", "application/x-tar",
                    "application/gzip"):
            return "archive"
        return "document"
    if message.sticker:
        return "sticker"
    if message.animation:
        return "gif"
    if message.video_note:
        return "video"
    if message.contact:
        return "contact"
    if message.location or message.venue:
        return "location"
    if message.poll:
        return "poll"
    return None

def extract_file_info(message, chat_id: int) -> Optional[dict]:
    """Extract file info dari message, return dict atau None"""
    msg_id = str(message.id)
    date_ts = int(message.date.timestamp()) if message.date else None

    if message.video:
        v = message.video
        mime = v.mime_type or "video/mp4"
        _file_cache[msg_id] = (chat_id, v.file_id, v.file_size or 0, mime)
        return {
            "id": message.id,
            "name": v.file_name or f"video_{message.id}.mp4",
            "size": v.file_size or 0,
            "file_id": v.file_id,
            "media_type": "video",
            "mime_type": mime,
            "caption": message.caption or "",
            "duration": v.duration,
            "width": v.width,
            "height": v.height,
            "date": date_ts,
        }

    if message.audio:
        a = message.audio
        mime = a.mime_type or "audio/mpeg"
        _file_cache[msg_id] = (chat_id, a.file_id, a.file_size or 0, mime)
        return {
            "id": message.id,
            "name": a.file_name or f"audio_{message.id}.mp3",
            "size": a.file_size or 0,
            "file_id": a.file_id,
            "media_type": "audio",
            "mime_type": mime,
            "caption": message.caption or "",
            "duration": a.duration,
            "date": date_ts,
        }

    if message.voice:
        v = message.voice
        mime = v.mime_type or "audio/ogg"
        _file_cache[msg_id] = (chat_id, v.file_id, v.file_size or 0, mime)
        return {
            "id": message.id,
            "name": f"voice_{message.id}.ogg",
            "size": v.file_size or 0,
            "file_id": v.file_id,
            "media_type": "voice",
            "mime_type": mime,
            "caption": message.caption or "",
            "duration": v.duration,
            "date": date_ts,
        }

    if message.photo:
        # Ambil ukuran terbesar
        photo = message.photo
        mime = "image/jpeg"
        _file_cache[msg_id] = (chat_id, photo.file_id, photo.file_size or 0, mime)
        return {
            "id": message.id,
            "name": f"photo_{message.id}.jpg",
            "size": photo.file_size or 0,
            "file_id": photo.file_id,
            "media_type": "image",
            "mime_type": mime,
            "caption": message.caption or "",
            "width": photo.width,
            "height": photo.height,
            "date": date_ts,
        }

    if message.document:
        doc = message.document
        mime = doc.mime_type or "application/octet-stream"
        media_type = detect_media_type(message)
        _file_cache[msg_id] = (chat_id, doc.file_id, doc.file_size or 0, mime)
        return {
            "id": message.id,
            "name": doc.file_name or f"file_{message.id}",
            "size": doc.file_size or 0,
            "file_id": doc.file_id,
            "media_type": media_type or "document",
            "mime_type": mime,
            "caption": message.caption or "",
            "date": date_ts,
        }

    if message.animation:
        a = message.animation
        mime = a.mime_type or "video/mp4"
        _file_cache[msg_id] = (chat_id, a.file_id, a.file_size or 0, mime)
        return {
            "id": message.id,
            "name": a.file_name or f"gif_{message.id}.mp4",
            "size": a.file_size or 0,
            "file_id": a.file_id,
            "media_type": "gif",
            "mime_type": mime,
            "caption": message.caption or "",
            "date": date_ts,
        }

    if message.sticker:
        s = message.sticker
        mime = s.mime_type or "image/webp"
        _file_cache[msg_id] = (chat_id, s.file_id, s.file_size or 0, mime)
        return {
            "id": message.id,
            "name": f"sticker_{message.id}.webp",
            "size": s.file_size or 0,
            "file_id": s.file_id,
            "media_type": "sticker",
            "mime_type": mime,
            "caption": "",
            "date": date_ts,
        }

    return None


# ─── ROUTES ───────────────────────────────────────────────

@router.get("/health")
async def health():
    return {
        "status": "healthy",
        "configured": bool(SESSION_STRING),
        "session_length": len(SESSION_STRING) if SESSION_STRING else 0,
    }


@router.get("/dialogs")
async def get_dialogs():
    """Ambil semua dialog (group/supergroup/channel)"""
    if not SESSION_STRING:
        raise HTTPException(status_code=500, detail="TELEGRAM_SESSION_STRING not configured")

    try:
        async with make_client() as client:
            dialogs = []
            async for dialog in client.get_dialogs():
                chat = dialog.chat
                # Hanya tampilkan group/supergroup/channel
                if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP, ChatType.CHANNEL):
                    continue

                dialogs.append({
                    "id": chat.id,
                    "name": chat.title or chat.username or f"Chat {chat.id}",  # FIX: pakai "name" bukan "title"
                    "type": chat_type_str(chat.type),
                    "unread_count": dialog.unread_messages_count or 0,
                })

            return {"dialogs": dialogs, "total": len(dialogs)}

    except Exception as e:
        print(f"[telegram] Error in get_dialogs: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/chat/{chat_id}/files")
async def get_chat_files(chat_id: int, limit: int = 200):
    """Ambil semua file media dari chat tertentu"""
    if not SESSION_STRING:
        raise HTTPException(status_code=500, detail="TELEGRAM_SESSION_STRING not configured")

    try:
        async with make_client() as client:
            files = []

            # Iterate semua pesan (lebih reliable dari filter spesifik)
            async for message in client.get_chat_history(chat_id, limit=limit):
                file_info = extract_file_info(message, chat_id)
                if file_info:
                    files.append(file_info)

            return {"files": files, "total": len(files), "chat_id": chat_id}

    except Exception as e:
        print(f"[telegram] Error in get_chat_files: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/stream/{chat_id}/{message_id}")
async def stream_file(request: Request, chat_id: int, message_id: int):
    """
    Stream file dari Telegram.
    Frontend memanggil: /telegram/stream/{chat_id}/{message_id}
    """
    if not SESSION_STRING:
        raise HTTPException(status_code=500, detail="TELEGRAM_SESSION_STRING not configured")

    msg_id_str = str(message_id)

    try:
        # Cek cache dulu
        cached = _file_cache.get(msg_id_str)
        if cached:
            _, file_id, file_size, mime_type = cached
        else:
            # Kalau tidak ada di cache, fetch message-nya dulu
            async with make_client() as client:
                msg = await client.get_messages(chat_id, message_id)
                if not msg:
                    raise HTTPException(status_code=404, detail="Message not found")
                file_info = extract_file_info(msg, chat_id)
                if not file_info:
                    raise HTTPException(status_code=404, detail="No media in this message")
                cached = _file_cache.get(msg_id_str)
                if not cached:
                    raise HTTPException(status_code=404, detail="File not found in cache after fetch")
                _, file_id, file_size, mime_type = cached

        # Support HTTP Range requests (untuk video seek)
        range_header = request.headers.get("range")
        
        async def generate_chunks():
            async with make_client() as client:
                # Stream menggunakan file_id langsung
                async for chunk in client.stream_media(file_id):
                    yield chunk

        if range_header and file_size:
            # Parse Range header: "bytes=start-end"
            try:
                range_val = range_header.replace("bytes=", "")
                start_str, end_str = range_val.split("-")
                start = int(start_str)
                end = int(end_str) if end_str else file_size - 1
                content_length = end - start + 1

                headers = {
                    "Content-Range": f"bytes {start}-{end}/{file_size}",
                    "Accept-Ranges": "bytes",
                    "Content-Length": str(content_length),
                    "Content-Type": mime_type,
                }

                # Pyrogram stream_media dengan offset
                async def generate_ranged():
                    async with make_client() as client:
                        offset = start // (1024 * 1024)  # chunk offset
                        yielded = 0
                        async for chunk in client.stream_media(file_id, offset=offset):
                            if yielded >= content_length:
                                break
                            remaining = content_length - yielded
                            if len(chunk) > remaining:
                                yield chunk[:remaining]
                                break
                            yield chunk
                            yielded += len(chunk)

                return StreamingResponse(
                    generate_ranged(),
                    status_code=206,
                    headers=headers,
                    media_type=mime_type,
                )
            except Exception:
                pass  # Fallback ke full stream jika range parse gagal

        # Full stream tanpa Range
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Type": mime_type,
        }
        if file_size:
            headers["Content-Length"] = str(file_size)

        return StreamingResponse(
            generate_chunks(),
            status_code=200,
            headers=headers,
            media_type=mime_type,
        )

    except HTTPException:
        raise
    except Exception as e:
        print(f"[telegram] Error in stream_file: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/download/{chat_id}/{message_id}")
async def download_file(chat_id: int, message_id: int):
    """Download file (dengan Content-Disposition attachment)"""
    if not SESSION_STRING:
        raise HTTPException(status_code=500, detail="TELEGRAM_SESSION_STRING not configured")

    msg_id_str = str(message_id)

    try:
        cached = _file_cache.get(msg_id_str)
        if cached:
            _, file_id, file_size, mime_type = cached
            # Cari nama file dari cache (tidak disimpan, pakai fallback)
            filename = f"file_{message_id}"
        else:
            async with make_client() as client:
                msg = await client.get_messages(chat_id, message_id)
                if not msg:
                    raise HTTPException(status_code=404, detail="Message not found")
                file_info = extract_file_info(msg, chat_id)
                if not file_info:
                    raise HTTPException(status_code=404, detail="No media in this message")
                cached = _file_cache.get(msg_id_str)
                if not cached:
                    raise HTTPException(status_code=404, detail="Cache error")
                _, file_id, file_size, mime_type = cached
                filename = file_info.get("name", f"file_{message_id}")

        async def generate():
            async with make_client() as client:
                async for chunk in client.stream_media(file_id):
                    yield chunk

        headers = {
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Type": mime_type,
        }
        if file_size:
            headers["Content-Length"] = str(file_size)

        return StreamingResponse(generate(), headers=headers, media_type=mime_type)

    except HTTPException:
        raise
    except Exception as e:
        print(f"[telegram] Error in download_file: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/thumbnail/{chat_id}/{message_id}")
async def get_thumbnail(chat_id: int, message_id: int):
    """Ambil thumbnail untuk video (jika ada)"""
    if not SESSION_STRING:
        raise HTTPException(status_code=500, detail="TELEGRAM_SESSION_STRING not configured")

    try:
        async with make_client() as client:
            msg = await client.get_messages(chat_id, message_id)
            if not msg or not msg.video:
                raise HTTPException(status_code=404, detail="No video/thumbnail found")

            thumbs = msg.video.thumbs
            if not thumbs:
                raise HTTPException(status_code=404, detail="No thumbnail available")

            thumb = thumbs[0]
            data = await client.download_media(thumb.file_id, in_memory=True)
            return Response(content=bytes(data), media_type="image/jpeg")

    except HTTPException:
        raise
    except Exception as e:
        print(f"[telegram] Error in get_thumbnail: {e}")
        raise HTTPException(status_code=500, detail=str(e))
