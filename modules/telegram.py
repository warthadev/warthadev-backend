# modules/telegram.py
import os
from typing import Dict, List, Optional
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse, Response
from pyrogram import Client, enums
from pyrogram.errors import PeerIdInvalid, ChannelInvalid
import asyncio

router = APIRouter(prefix="/telegram", tags=["telegram"])

# ========== KONFIGURASI ==========
SESSION_STRING = os.environ.get("TELEGRAM_SESSION_STRING", "")
if not SESSION_STRING:
    print("WARNING: TELEGRAM_SESSION_STRING not set!")

# Client GLOBAL (dibuat sekali, dipakai berkali-kali)
telegram_client = Client(
    "telegram_manager",
    session_string=SESSION_STRING,
    in_memory=True,
    no_updates=True,
)

# Cache
file_sizes: Dict[str, int] = {}
videos_cache: Dict[int, List[dict]] = {}  # chat_id -> list video

# ========== LIFECYCLE ==========
async def start_client():
    """Start client saat aplikasi mulai"""
    if SESSION_STRING:
        await telegram_client.start()
        me = await telegram_client.get_me()
        print(f"✅ Telegram client started as: {me.first_name}")
    else:
        print("❌ Telegram client not started: missing SESSION_STRING")

async def shutdown_client():
    """Stop client saat aplikasi berhenti"""
    if telegram_client.is_connected:
        await telegram_client.stop()
        print("✅ Telegram client stopped")

# Fungsi ini akan dipanggil dari main.py (FastAPI startup/shutdown)
def register_telegram_events(app):
    app.add_event_handler("startup", start_client)
    app.add_event_handler("shutdown", shutdown_client)

# ========== HELPER ==========
async def get_chat_videos(chat_id: int, limit: int = 500) -> List[dict]:
    """Ambil daftar video dari chat (cache 5 menit)"""
    cache_key = chat_id
    if cache_key in videos_cache:
        return videos_cache[cache_key]
    
    videos = []
    try:
        async for msg in telegram_client.get_chat_history(chat_id, limit=limit):
            # Video message
            if msg.video:
                name = msg.video.file_name or f"video_{msg.id}.mp4"
                videos.append({
                    "id": msg.id,
                    "name": name,
                    "size": msg.video.file_size,
                    "file_id": msg.video.file_id,
                    "type": "video"
                })
                file_sizes[msg.video.file_id] = msg.video.file_size
            # Document (video file)
            elif msg.document and msg.document.file_name:
                ext = msg.document.file_name.split('.')[-1].lower()
                if ext in ['mp4', 'mkv', 'avi', 'mov', 'webm']:
                    videos.append({
                        "id": msg.id,
                        "name": msg.document.file_name,
                        "size": msg.document.file_size,
                        "file_id": msg.document.file_id,
                        "type": "video"
                    })
                    file_sizes[msg.document.file_id] = msg.document.file_size
    except Exception as e:
        print(f"Error loading videos from chat {chat_id}: {e}")
    
    videos.reverse()  # oldest first
    videos_cache[cache_key] = videos
    return videos

# ========== ENDPOINTS ==========
@router.get("/health")
async def health():
    return {
        "status": "healthy",
        "configured": bool(SESSION_STRING),
        "client_connected": telegram_client.is_connected if SESSION_STRING else False
    }

@router.get("/chats")
async def get_chats():
    """Daftar semua channel/grup yang bisa diakses"""
    if not SESSION_STRING or not telegram_client.is_connected:
        raise HTTPException(500, "Telegram client not ready")
    
    dialogs = []
    async for dialog in telegram_client.get_dialogs():
        chat = dialog.chat
        if chat.type in [enums.ChatType.CHANNEL, enums.ChatType.GROUP, enums.ChatType.SUPERGROUP]:
            dialogs.append({
                "id": chat.id,
                "name": chat.title,
                "type": str(chat.type).split('.')[-1].lower(),
            })
    return {"chats": dialogs, "total": len(dialogs)}

@router.get("/chat/{chat_id}/videos")
async def get_videos(chat_id: int):
    """Daftar video dalam chat tertentu"""
    if not SESSION_STRING or not telegram_client.is_connected:
        raise HTTPException(500, "Telegram client not ready")
    
    try:
        videos = await get_chat_videos(chat_id)
        return {"videos": videos, "total": len(videos), "chat_id": chat_id}
    except Exception as e:
        print(f"Error: {e}")
        return {"videos": [], "total": 0, "chat_id": chat_id, "error": str(e)}

@router.get("/stream/{chat_id}/{message_id}")
async def stream_video(request: Request, chat_id: int, message_id: int):
    """Stream video dengan dukungan seeking (sama seperti di Colab)"""
    if not SESSION_STRING or not telegram_client.is_connected:
        raise HTTPException(500, "Telegram client not ready")
    
    # Ambil message
    msg = await telegram_client.get_messages(chat_id, message_id)
    if not msg or (not msg.video and not msg.document):
        raise HTTPException(404, "Video not found")
    
    # Ambil file_id dan ukuran
    if msg.video:
        file_id = msg.video.file_id
        file_size = msg.video.file_size
        mime_type = "video/mp4"
    else:
        file_id = msg.document.file_id
        file_size = msg.document.file_size
        mime_type = msg.document.mime_type or "video/mp4"
    
    range_header = request.headers.get("range")
    
    # ========== SEEKING MODE ==========
    if range_header and range_header.startswith("bytes=") and file_size:
        try:
            range_val = range_header.replace("bytes=", "")
            parts = range_val.split("-")
            start_byte = int(parts[0])
            end_byte = int(parts[1]) if parts[1] else file_size - 1
            
            if start_byte >= file_size or end_byte >= file_size:
                return Response(status_code=416)
            
            requested_bytes = end_byte - start_byte + 1
            CHUNK_SIZE = 1024 * 1024
            start_chunk = start_byte // CHUNK_SIZE
            end_chunk = (end_byte // CHUNK_SIZE) + 1
            chunks_needed = end_chunk - start_chunk
            
            print(f"🎯 Seeking: {start_byte}-{end_byte} (chunks {start_chunk}-{end_chunk})")
            
            async def seek_generator():
                streamed = 0
                async for chunk in telegram_client.stream_media(file_id, offset=start_chunk, limit=chunks_needed):
                    if streamed >= requested_bytes:
                        break
                    chunk_start = start_byte - (start_chunk * CHUNK_SIZE)
                    if streamed == 0 and chunk_start > 0 and chunk_start < len(chunk):
                        chunk = chunk[chunk_start:]
                    if streamed + len(chunk) > requested_bytes:
                        chunk = chunk[:requested_bytes - streamed]
                    yield chunk
                    streamed += len(chunk)
            
            return StreamingResponse(
                seek_generator(),
                status_code=206,
                media_type=mime_type,
                headers={
                    "Content-Range": f"bytes {start_byte}-{end_byte}/{file_size}",
                    "Accept-Ranges": "bytes",
                    "Content-Length": str(requested_bytes),
                    "Cache-Control": "no-cache",
                }
            )
        except Exception as e:
            print(f"Seeking error: {e}")
            # Fallback ke normal stream
    
    # ========== NORMAL STREAM ==========
    async def generate_chunks():
        async for chunk in telegram_client.stream_media(file_id, limit=0):
            yield chunk
    
    headers = {
        "Content-Type": mime_type,
        "Accept-Ranges": "bytes",
        "Cache-Control": "no-cache",
    }
    if file_size:
        headers["Content-Length"] = str(file_size)
    
    return StreamingResponse(generate_chunks(), status_code=200, media_type=mime_type, headers=headers)