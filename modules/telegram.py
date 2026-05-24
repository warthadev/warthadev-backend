# modules/telegram.py
import os
import asyncio
import zipfile
import tempfile
import shutil
import mimetypes
from io import BytesIO
from typing import Dict, Any, List, Optional
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse, Response
from pyrogram import Client
from pyrogram.types import Message

# Optional imports untuk archive
try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

try:
    import rarfile
    HAS_RARFILE = True
except ImportError:
    HAS_RARFILE = False

try:
    import py7zr
    HAS_PY7ZR = True
except ImportError:
    HAS_PY7ZR = False

router = APIRouter(prefix="/telegram", tags=["telegram"])

# ========== KONFIGURASI ==========
SESSION_STRING = os.environ.get("TELEGRAM_SESSION_STRING", "")

if not SESSION_STRING:
    print("⚠️ WARNING: TELEGRAM_SESSION_STRING not set!")

# Global client (singleton)
_client: Optional[Client] = None

async def get_client() -> Client:
    global _client
    if _client is None:
        _client = Client(
            "telegram_manager",
            session_string=SESSION_STRING,
            in_memory=True,
            no_updates=True
        )
        await _client.start()
        print("✅ Pyrogram client started")
    return _client

# Cache
chat_file_cache: Dict[tuple, Dict] = {}
dialogs_cache: List[Dict] = []
dialogs_loaded = False
archive_cache: Dict[tuple, Dict] = {}

# ========== UTILITY FUNCTIONS ==========
def format_bytes(size: int) -> str:
    if not size:
        return '0 B'
    if size < 1024:
        return f'{size} B'
    if size < 1024*1024:
        return f'{size/1024:.1f} KB'
    if size < 1024*1024*1024:
        return f'{size/(1024*1024):.1f} MB'
    return f'{size/(1024*1024*1024):.2f} GB'

def get_media_type(filename: str) -> str:
    ext = filename.split('.')[-1].lower()
    
    video_exts = ['mp4', 'mkv', 'avi', 'mov', 'webm', 'flv', 'm4v', '3gp', 'wmv', 'ts']
    audio_exts = ['mp3', 'm4a', 'wav', 'ogg', 'flac', 'aac', 'opus', 'wma', 'amr']
    image_exts = ['jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp', 'svg', 'ico', 'tiff']
    archive_exts = ['zip', 'rar', '7z']
    
    if ext in video_exts:
        return 'video'
    elif ext in audio_exts:
        return 'audio'
    elif ext in image_exts:
        return 'image'
    elif ext in archive_exts:
        return 'archive'
    else:
        return 'file'

def get_mime_type(filename: str) -> str:
    ext = filename.split('.')[-1].lower()
    mime_types = {
        'mp4': 'video/mp4', 'mkv': 'video/x-matroska', 'avi': 'video/x-msvideo',
        'mov': 'video/quicktime', 'webm': 'video/webm', 'mp3': 'audio/mpeg',
        'm4a': 'audio/mp4', 'wav': 'audio/wav', 'ogg': 'audio/ogg',
        'flac': 'audio/flac', 'jpg': 'image/jpeg', 'jpeg': 'image/jpeg',
        'png': 'image/png', 'gif': 'image/gif', 'webp': 'image/webp',
        'pdf': 'application/pdf', 'txt': 'text/plain', 'json': 'application/json',
        'zip': 'application/zip', 'rar': 'application/vnd.rar', '7z': 'application/x-7z-compressed'
    }
    return mime_types.get(ext, 'application/octet-stream')

# ========== CORE FUNCTIONS ==========
async def get_dialogs():
    global dialogs_cache, dialogs_loaded
    if dialogs_loaded:
        return dialogs_cache
    
    client = await get_client()
    dialogs = []
    async for dialog in client.get_dialogs():
        chat = dialog.chat
        dialogs.append({
            "id": chat.id,
            "name": chat.title or chat.first_name or "Unknown",
            "type": str(chat.type).split('.')[-1]
        })
    dialogs_cache = dialogs
    dialogs_loaded = True
    return dialogs

async def get_chat_files(chat_id: int, limit: int = 200):
    files = []
    client = await get_client()
    
    async for msg in client.get_chat_history(chat_id, limit=limit):
        if msg.document:
            name = msg.document.file_name
            if not name:
                continue
            files.append({
                "id": msg.id,
                "name": name,
                "size": msg.document.file_size,
                "file_id": msg.document.file_id,
                "media_type": get_media_type(name)
            })
        elif msg.video:
            vid = msg.video
            name = vid.file_name or f"video_{msg.id}.mp4"
            files.append({
                "id": msg.id,
                "name": name,
                "size": vid.file_size,
                "file_id": vid.file_id,
                "media_type": "video"
            })
        elif msg.audio:
            aud = msg.audio
            name = aud.file_name or f"audio_{msg.id}.mp3"
            files.append({
                "id": msg.id,
                "name": name,
                "size": aud.file_size,
                "file_id": aud.file_id,
                "media_type": "audio"
            })
        elif msg.photo:
            photo = msg.photo[-1]
            files.append({
                "id": msg.id,
                "name": f"photo_{msg.id}.jpg",
                "size": photo.file_size,
                "file_id": photo.file_id,
                "media_type": "image"
            })
    
    files.reverse()
    return files

async def get_file_info(chat_id: int, message_id: int):
    key = (chat_id, message_id)
    if key in chat_file_cache:
        return chat_file_cache[key]
    
    client = await get_client()
    msg = await client.get_messages(chat_id, message_id)
    
    if not msg:
        return None
    
    info = None
    if msg.document:
        info = {
            "id": message_id,
            "name": msg.document.file_name,
            "size": msg.document.file_size,
            "file_id": msg.document.file_id,
            "media_type": get_media_type(msg.document.file_name),
            "mime_type": get_mime_type(msg.document.file_name)
        }
    elif msg.video:
        info = {
            "id": message_id,
            "name": msg.video.file_name or f"video_{message_id}.mp4",
            "size": msg.video.file_size,
            "file_id": msg.video.file_id,
            "media_type": "video",
            "mime_type": "video/mp4"
        }
    elif msg.audio:
        info = {
            "id": message_id,
            "name": msg.audio.file_name or f"audio_{message_id}.mp3",
            "size": msg.audio.file_size,
            "file_id": msg.audio.file_id,
            "media_type": "audio",
            "mime_type": "audio/mpeg"
        }
    elif msg.photo:
        photo = msg.photo[-1]
        info = {
            "id": message_id,
            "name": f"photo_{message_id}.jpg",
            "size": photo.file_size,
            "file_id": photo.file_id,
            "media_type": "image",
            "mime_type": "image/jpeg"
        }
    
    if info:
        chat_file_cache[key] = info
    return info

# ========== STREAMING ==========
@app.get("/stream/{chat_id}/{message_id}")
async def stream_file(request: Request, chat_id: int, message_id: int):
    info = await get_file_info(chat_id, message_id)
    if not info:
        raise HTTPException(404, "File not found")
    
    file_id = info['file_id']
    file_size = info['size']
    mime_type = info['mime_type']
    range_header = request.headers.get("range")
    
    if range_header and range_header.startswith("bytes=") and file_size:
        try:
            range_val = range_header.replace("bytes=", "")
            parts = range_val.split("-")
            start = int(parts[0])
            end = int(parts[1]) if parts[1] else file_size - 1
            
            if start >= file_size or end >= file_size:
                return Response(status_code=416)
            
            length = end - start + 1
            CHUNK_SIZE = 1024 * 1024
            start_chunk = start // CHUNK_SIZE
            end_chunk = (end // CHUNK_SIZE) + 1
            chunks_needed = end_chunk - start_chunk
            
            client = await get_client()
            
            async def seek_generator():
                streamed = 0
                async for chunk in client.stream_media(file_id, offset=start_chunk, limit=chunks_needed):
                    if streamed >= length:
                        break
                    chunk_start = start - (start_chunk * CHUNK_SIZE)
                    if streamed == 0 and chunk_start > 0:
                        chunk = chunk[chunk_start:]
                    if streamed + len(chunk) > length:
                        chunk = chunk[:length - streamed]
                    yield chunk
                    streamed += len(chunk)
            
            return StreamingResponse(
                seek_generator(),
                status_code=206,
                media_type=mime_type,
                headers={
                    "Content-Range": f"bytes {start}-{end}/{file_size}",
                    "Accept-Ranges": "bytes",
                    "Content-Length": str(length),
                    "Cache-Control": "no-cache",
                }
            )
        except Exception as e:
            print(f"Seeking error: {e}")
    
    # Normal stream
    client = await get_client()
    
    async def generate():
        async for chunk in client.stream_media(file_id, limit=0):
            yield chunk
    
    headers = {
        "Content-Type": mime_type,
        "Accept-Ranges": "bytes",
        "Cache-Control": "no-cache",
    }
    if file_size:
        headers["Content-Length"] = str(file_size)
    
    return StreamingResponse(generate(), media_type=mime_type, headers=headers)

# ========== ENDPOINTS ==========
@router.get("/health")
async def health():
    return {
        "status": "healthy",
        "configured": bool(SESSION_STRING),
        "session_length": len(SESSION_STRING) if SESSION_STRING else 0
    }

@router.get("/dialogs")
async def list_dialogs():
    dialogs = await get_dialogs()
    return {"dialogs": dialogs}

@router.get("/chat/{chat_id}/files")
async def list_chat_files(chat_id: int, limit: int = 200):
    files = await get_chat_files(chat_id, limit)
    return {"files": files, "chat_id": chat_id, "count": len(files)}

@router.get("/file/{chat_id}/{message_id}")
async def file_info(chat_id: int, message_id: int):
    info = await get_file_info(chat_id, message_id)
    if not info:
        raise HTTPException(404, "File not found")
    return info

@router.get("/download/{chat_id}/{message_id}")
async def download_file(chat_id: int, message_id: int):
    info = await get_file_info(chat_id, message_id)
    if not info:
        raise HTTPException(404, "File not found")
    
    file_id = info['file_id']
    filename = info['name']
    client = await get_client()
    
    async def generate():
        async for chunk in client.stream_media(file_id, limit=0):
            yield chunk
    
    return StreamingResponse(
        generate(),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f"attachment; filename=\"{filename}\""}
    ) 