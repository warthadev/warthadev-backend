# modules/telegram.py
import os
from typing import Dict, Tuple, Optional
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse, Response
from pyrogram import Client

router = APIRouter(prefix="/telegram", tags=["telegram"])

SESSION_STRING = os.environ.get("TELEGRAM_SESSION_STRING", "")

if not SESSION_STRING:
    print("WARNING: TELEGRAM_SESSION_STRING not set!")

# Cache file_id -> file_size
file_sizes: Dict[str, int] = {}
files_cache: Dict[int, list] = {}  # chat_id -> list of files
cache_loaded: Dict[int, bool] = {}

async def get_client() -> Client:
    return Client(
        "telegram_manager",
        session_string=SESSION_STRING,
        in_memory=True,
        no_updates=True,
    )

async def get_file_size(client: Client, chat_id: int, file_id: str) -> Optional[int]:
    """Dapatkan ukuran file (cached) seperti di Colab"""
    if file_id in file_sizes:
        return file_sizes[file_id]
    
    try:
        messages = await client.get_messages(chat_id, file_ids=file_id)
        if messages:
            if messages.video:
                file_sizes[file_id] = messages.video.file_size
                return messages.video.file_size
            elif messages.document:
                file_sizes[file_id] = messages.document.file_size
                return messages.document.file_size
    except Exception as e:
        print(f"Error getting file size: {e}")
    return None

async def load_chat_files(chat_id: int):
    """Load semua file dari chat (mirip dengan Colab)"""
    global files_cache, cache_loaded
    
    if cache_loaded.get(chat_id, False):
        return files_cache.get(chat_id, [])
    
    async with await get_client() as client:
        files_found = []
        async for message in client.get_chat_history(chat_id):
            # Video
            if message.video:
                file_name = message.video.file_name or f"video_{message.id}.mp4"
                files_found.append({
                    "id": message.id,
                    "name": file_name,
                    "size": message.video.file_size,
                    "file_id": message.video.file_id,
                    "media_type": "video",
                })
                file_sizes[message.video.file_id] = message.video.file_size
            
            # Audio
            elif message.audio:
                files_found.append({
                    "id": message.id,
                    "name": message.audio.file_name or f"audio_{message.id}.mp3",
                    "size": message.audio.file_size,
                    "file_id": message.audio.file_id,
                    "media_type": "audio",
                })
                file_sizes[message.audio.file_id] = message.audio.file_size
            
            # Photo
            elif message.photo:
                photo = message.photo[-1]
                files_found.append({
                    "id": message.id,
                    "name": f"photo_{message.id}.jpg",
                    "size": photo.file_size,
                    "file_id": photo.file_id,
                    "media_type": "image",
                })
                file_sizes[photo.file_id] = photo.file_size
            
            # Document (termasuk video dalam archive)
            elif message.document:
                file_name = message.document.file_name
                if file_name:
                    ext = file_name.split('.')[-1].lower()
                    media_type = "video" if ext in ['mp4', 'mkv', 'avi', 'mov', 'webm'] else "document"
                    files_found.append({
                        "id": message.id,
                        "name": file_name,
                        "size": message.document.file_size,
                        "file_id": message.document.file_id,
                        "media_type": media_type,
                    })
                    file_sizes[message.document.file_id] = message.document.file_size
        
        files_found.reverse()
        print(f"✅ Chat {chat_id}: {len(files_found)} files ready")
        files_cache[chat_id] = files_found
        cache_loaded[chat_id] = True
        return files_found

# ============ ENDPOINTS ============

@router.get("/health")
async def health():
    return {
        "status": "healthy",
        "configured": bool(SESSION_STRING),
        "session_length": len(SESSION_STRING) if SESSION_STRING else 0
    }

@router.get("/dialogs")
async def get_dialogs():
    """Ambil daftar chat (tanpa validasi akses file)"""
    if not SESSION_STRING:
        raise HTTPException(500, "TELEGRAM_SESSION_STRING not configured")
    
    try:
        async with await get_client() as client:
            dialogs = []
            async for dialog in client.get_dialogs():
                chat = dialog.chat
                if chat.type in ["group", "supergroup", "channel"]:
                    dialogs.append({
                        "id": chat.id,
                        "name": chat.title,
                        "type": str(chat.type).split('.')[-1].lower(),
                        "unread_count": dialog.unread_messages_count or 0,
                    })
            return {"dialogs": dialogs, "total": len(dialogs)}
    except Exception as e:
        print(f"Error in get_dialogs: {e}")
        raise HTTPException(500, detail=str(e))

@router.get("/chat/{chat_id}/files")
async def get_chat_files(chat_id: int):
    """Ambil file dari chat - LANGSUNG akses tanpa validasi"""
    if not SESSION_STRING:
        raise HTTPException(500, "TELEGRAM_SESSION_STRING not configured")
    
    try:
        files = await load_chat_files(chat_id)
        return {"files": files, "total": len(files), "chat_id": chat_id}
    except Exception as e:
        print(f"Error loading files for chat {chat_id}: {e}")
        # Tetap return array kosong, jangan error
        return {"files": [], "total": 0, "chat_id": chat_id, "error": str(e)}

@router.get("/stream/{chat_id}/{file_id}")
async def stream_file(request: Request, chat_id: int, file_id: str):
    """Stream file - seperti di Colab"""
    if not SESSION_STRING:
        raise HTTPException(500, "TELEGRAM_SESSION_STRING not configured")
    
    async with await get_client() as client:
        file_size = await get_file_size(client, chat_id, file_id)
        range_header = request.headers.get("range")
        
        # Seeking mode (sama persis seperti Colab)
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
                        async for chunk in stream_client.stream_media(file_id, offset=start_chunk, limit=chunks_needed):
                            if streamed >= requested_bytes:
                                break
                            chunk_start = start_byte - (start_chunk * CHUNK_SIZE)
                            if streamed == 0 and chunk_start > 0:
                                chunk = chunk[chunk_start:]
                            if streamed + len(chunk) > requested_bytes:
                                chunk = chunk[:requested_bytes - streamed]
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
                    }
                )
            except Exception as e:
                print(f"Seeking error: {e}")
        
        # Normal stream
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
        
        return StreamingResponse(generate_chunks(), status_code=200, media_type="video/mp4", headers=headers)