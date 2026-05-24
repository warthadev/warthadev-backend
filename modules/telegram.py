# modules/telegram.py
import os
import asyncio
from typing import Dict, Tuple
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse, Response
from pyrogram import Client
import nest_asyncio

nest_asyncio.apply()

router = APIRouter(prefix="/telegram", tags=["telegram"])

# === Konfigurasi dari environment variables ===
API_ID = int(os.environ.get("TELEGRAM_API_ID", 0))
API_HASH = os.environ.get("TELEGRAM_API_HASH", "")
SESSION_STRING = os.environ.get("TELEGRAM_SESSION_STRING", "")

if not API_ID or not API_HASH or not SESSION_STRING:
    raise RuntimeError(
        "Missing Telegram credentials. Set TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_SESSION_STRING"
    )

# Cache untuk menyimpan info file (file_id -> (chat_id, message_id, size))
_file_cache: Dict[str, Tuple[int, int, int]] = {}


def _cache_file_info(file_id: str, chat_id: int, message_id: int, size: int) -> None:
    _file_cache[file_id] = (chat_id, message_id, size)


async def get_client() -> Client:
    """Buat instance Pyrogram client (in‑memory)."""
    return Client(
        "telegram_manager",
        api_id=API_ID,
        api_hash=API_HASH,
        session_string=SESSION_STRING,
        in_memory=True,
    )


@router.get("/dialogs")
async def get_dialogs():
    """Daftar semua grup/channel yang bisa diakses."""
    async with await get_client() as client:
        dialogs = []
        async for dialog in client.get_dialogs():
            chat = dialog.chat
            if chat.type in ["group", "supergroup", "channel"]:
                dialogs.append(
                    {
                        "id": chat.id,
                        "title": chat.title,
                        "type": str(chat.type),
                        "unread_count": dialog.unread_count or 0,
                    }
                )
        return {"dialogs": dialogs}


@router.get("/chat/{chat_id}/files")
async def get_chat_files(chat_id: int, limit: int = 100):
    """
    Ambil daftar file dari sebuah chat (dokumen, video, audio, foto).
    Sekaligus mengisi cache untuk keperluan streaming.
    """
    async with await get_client() as client:
        files = []
        async for message in client.get_chat_history(chat_id, limit=limit):
            # Dokumen
            if message.document:
                doc = message.document
                _cache_file_info(doc.file_id, chat_id, message.id, doc.file_size)
                files.append(
                    {
                        "id": str(message.id),
                        "name": doc.file_name or f"file_{message.id}",
                        "size": doc.file_size,
                        "file_id": doc.file_id,
                        "mime_type": doc.mime_type or "application/octet-stream",
                        "date": message.date.timestamp(),
                        "chat_id": chat_id,
                    }
                )
            # Video
            elif message.video:
                vid = message.video
                _cache_file_info(vid.file_id, chat_id, message.id, vid.file_size)
                files.append(
                    {
                        "id": str(message.id),
                        "name": vid.file_name or f"video_{message.id}.mp4",
                        "size": vid.file_size,
                        "file_id": vid.file_id,
                        "mime_type": "video/mp4",
                        "date": message.date.timestamp(),
                        "chat_id": chat_id,
                    }
                )
            # Audio
            elif message.audio:
                aud = message.audio
                _cache_file_info(aud.file_id, chat_id, message.id, aud.file_size)
                files.append(
                    {
                        "id": str(message.id),
                        "name": aud.file_name or f"audio_{message.id}.mp3",
                        "size": aud.file_size,
                        "file_id": aud.file_id,
                        "mime_type": "audio/mpeg",
                        "date": message.date.timestamp(),
                        "chat_id": chat_id,
                    }
                )
            # Foto
            elif message.photo:
                photo = message.photo[-1]  # ambil resolusi terbesar
                _cache_file_info(photo.file_id, chat_id, message.id, photo.file_size)
                files.append(
                    {
                        "id": str(message.id),
                        "name": f"photo_{message.id}.jpg",
                        "size": photo.file_size,
                        "file_id": photo.file_id,
                        "mime_type": "image/jpeg",
                        "date": message.date.timestamp(),
                        "chat_id": chat_id,
                    }
                )
        return {"files": files}


@router.get("/stream/{file_id}")
async def stream_telegram_file(request: Request, file_id: str):
    """
    Stream file dengan dukungan seeking (jika ukuran file diketahui dari cache).
    Jika tidak ada di cache, tetap stream tanpa seeking.
    """
    async with await get_client() as client:
        # Coba ambil ukuran file dari cache
        cached = _file_cache.get(file_id)
        file_size = cached[2] if cached else None

        range_header = request.headers.get("range")

        # === SEEKING MODE (jika cache ada) ===
        if range_header and range_header.startswith("bytes=") and file_size:
            try:
                range_value = range_header.replace("bytes=", "")
                parts = range_value.split("-")
                start_byte = int(parts[0])
                end_byte = int(parts[1]) if parts[1] else file_size - 1

                if start_byte >= file_size or end_byte >= file_size:
                    return Response(status_code=416)

                requested_bytes = end_byte - start_byte + 1
                CHUNK_SIZE = 1024 * 1024
                start_chunk = start_byte // CHUNK_SIZE
                end_chunk = (end_byte // CHUNK_SIZE) + 1
                chunks_needed = end_chunk - start_chunk

                async def seek_generator():
                    async with await get_client() as stream_client:
                        streamed = 0
                        async for chunk in stream_client.stream_media(
                            file_id, offset=start_chunk, limit=chunks_needed
                        ):
                            if streamed >= requested_bytes:
                                break
                            chunk_start = start_byte - (start_chunk * CHUNK_SIZE)
                            if streamed == 0 and chunk_start > 0:
                                chunk = chunk[chunk_start:]
                            if streamed + len(chunk) > requested_bytes:
                                chunk = chunk[: requested_bytes - streamed]
                            yield chunk
                            streamed += len(chunk)

                return StreamingResponse(
                    seek_generator(),
                    status_code=206,
                    media_type="video/mp4",
                    headers={
                        "Content-Range": f"bytes {start_byte}-{end_byte}/{file_size}",
                        "Accept-Ranges": "bytes",
                        "Content-Length": str(requested_bytes),
                        "Cache-Control": "no-cache",
                    },
                )
            except Exception as e:
                print(f"Seeking error: {e}")
                # fallback ke mode normal

        # === NORMAL STREAM (tanpa seeking atau tanpa ukuran file) ===
        async def generate_chunks():
            async with await get_client() as stream_client:
                async for chunk in stream_client.stream_media(file_id, limit=0):
                    yield chunk

        headers = {
            "Content-Type": "video/mp4",
            "Accept-Ranges": "bytes",
            "Cache-Control": "no-cache",
        }
        if file_size:
            headers["Content-Length"] = str(file_size)

        return StreamingResponse(
            generate_chunks(),
            status_code=200,
            media_type="video/mp4",
            headers=headers,
        )