# modules/telegram.py
import os
from typing import Dict, List, Optional
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import StreamingResponse, Response, RedirectResponse
from pyrogram import Client, enums

router = APIRouter(prefix="/telegram", tags=["telegram"])

# ========== KONFIGURASI ==========
SESSION_STRING = os.environ.get("TELEGRAM_SESSION_STRING", "")
if not SESSION_STRING:
    print("WARNING: TELEGRAM_SESSION_STRING not set!")

# Client GLOBAL
telegram_client = Client(
    "telegram_manager",
    session_string=SESSION_STRING,
    in_memory=True,
    no_updates=True,
)

# Cache untuk file dan ukuran total per chat
files_cache: Dict[int, List[dict]] = {}
chat_total_size_cache: Dict[int, int] = {}

# ========== LIFECYCLE ==========
async def start_client():
    if SESSION_STRING and not telegram_client.is_connected:
        await telegram_client.start()
        me = await telegram_client.get_me()
        print(f"Telegram client started as: {me.first_name}")
    elif not SESSION_STRING:
        print("Telegram client not started: missing SESSION_STRING")

async def shutdown_client():
    if telegram_client.is_connected:
        await telegram_client.stop()
        print("Telegram client stopped")

def register_telegram_events(app):
    app.add_event_handler("startup", start_client)
    app.add_event_handler("shutdown", shutdown_client)

# ========== CLIENT READINESS ==========
async def ensure_client_ready():
    if not SESSION_STRING:
        raise HTTPException(500, detail="Telegram not configured (missing TELEGRAM_SESSION_STRING)")
    if not telegram_client.is_connected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Telegram client is connecting, please retry",
            headers={"Retry-After": "2"}
        )

# ========== HELPER ==========
def format_size(bytes_size: int) -> str:
    if not bytes_size:
        return "0 B"
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
    total_size = 0
    try:
        async for msg in telegram_client.get_chat_history(chat_id, limit=limit):
            if msg.video:
                size = msg.video.file_size or 0
                files.append({
                    "id": msg.id,
                    "name": msg.video.file_name or f"video_{msg.id}.mp4",
                    "size": size,
                    "file_id": msg.video.file_id,
                    "media_type": "video",
                    "duration": msg.video.duration,
                    "width": msg.video.width,
                    "height": msg.video.height,
                    "date": msg.date.timestamp() if msg.date else None,
                })
                total_size += size
            elif msg.audio:
                size = msg.audio.file_size or 0
                files.append({
                    "id": msg.id,
                    "name": msg.audio.file_name or f"audio_{msg.id}.mp3",
                    "size": size,
                    "file_id": msg.audio.file_id,
                    "media_type": "audio",
                    "duration": msg.audio.duration,
                    "date": msg.date.timestamp() if msg.date else None,
                })
                total_size += size
            elif msg.photo:
                photo = msg.photo[-1]
                size = photo.file_size or 0
                files.append({
                    "id": msg.id,
                    "name": f"photo_{msg.id}.jpg",
                    "size": size,
                    "file_id": photo.file_id,
                    "media_type": "image",
                    "width": photo.width,
                    "height": photo.height,
                    "date": msg.date.timestamp() if msg.date else None,
                })
                total_size += size
            elif msg.document and msg.document.file_name:
                name = msg.document.file_name
                ext = name.split('.')[-1].lower()
                if ext in ['mp4', 'mkv', 'avi', 'mov', 'webm']:
                    media_type = "video"
                elif ext in ['mp3', 'm4a', 'wav', 'ogg', 'flac']:
                    media_type = "audio"
                elif ext in ['jpg', 'jpeg', 'png', 'gif', 'webp']:
                    media_type = "image"
                else:
                    media_type = "document"
                size = msg.document.file_size or 0
                files.append({
                    "id": msg.id,
                    "name": name,
                    "size": size,
                    "file_id": msg.document.file_id,
                    "media_type": media_type,
                    "date": msg.date.timestamp() if msg.date else None,
                })
                total_size += size
        files.reverse()
        files_cache[chat_id] = files
        chat_total_size_cache[chat_id] = total_size
        print(f"Loaded {len(files)} files from chat {chat_id}, total size: {format_size(total_size)}")
    except Exception as e:
        print(f"Error loading files from chat {chat_id}: {e}")
        files = []
        total_size = 0
    return files

async def get_chat_total_size(chat_id: int) -> int:
    if chat_id in chat_total_size_cache:
        return chat_total_size_cache[chat_id]
    await load_chat_files(chat_id)
    return chat_total_size_cache.get(chat_id, 0)

# ========== R2 INTEGRATION ==========
def is_r2_configured() -> bool:
    return all([
        os.environ.get("R2_ACCESS_KEY_ID"),
        os.environ.get("R2_SECRET_ACCESS_KEY"),
        os.environ.get("R2_ACCOUNT_ID"),
        os.environ.get("R2_BUCKET_NAME")
    ])

async def check_r2_and_redirect(chat_id: int, message_id: int):
    try:
        from modules.r2 import get_r2_client, generate_presigned_url, upload_telegram_to_r2, R2_BUCKET_NAME
    except ImportError:
        print("R2 module not available, skipping R2 integration")
        return None
    
    if not is_r2_configured():
        print("R2 not configured, skipping R2 integration")
        return None
    
    try:
        r2 = get_r2_client()
        prefix = f"telegram/{chat_id}/{message_id}/"
        resp = r2.list_objects_v2(Bucket=R2_BUCKET_NAME, Prefix=prefix)
        objects = resp.get("Contents", [])
        
        if objects:
            key = objects[0]["Key"]
            url = generate_presigned_url(r2, key, expires=3600)
            return RedirectResponse(url=url, status_code=302)
        else:
            print(f"Uploading {chat_id}/{message_id} to R2...")
            await upload_telegram_to_r2(telegram_client, chat_id, message_id)
            return RedirectResponse(url=f"/r2/stream/{chat_id}/{message_id}", status_code=302)
    except Exception as e:
        print(f"R2 operation failed: {e}, falling back to direct stream")
        return None

# ========== ENDPOINTS ==========
@router.get("/health")
async def health():
    return {
        "status": "healthy",
        "configured": bool(SESSION_STRING),
        "client_connected": telegram_client.is_connected if SESSION_STRING else False,
        "r2_enabled": is_r2_configured()
    }

@router.get("/dialogs")
async def get_dialogs():
    await ensure_client_ready()
    dialogs = []
    try:
        async for dialog in telegram_client.get_dialogs():
            chat = dialog.chat
            if chat.type in [enums.ChatType.CHANNEL, enums.ChatType.GROUP, enums.ChatType.SUPERGROUP]:
                total_size_bytes = await get_chat_total_size(chat.id)
                dialogs.append({
                    "id": chat.id,
                    "name": chat.title,
                    "type": str(chat.type).split('.')[-1].lower(),
                    "unread_count": dialog.unread_messages_count or 0,
                    "total_size_bytes": total_size_bytes,
                    "total_size_human": format_size(total_size_bytes)
                })
    except Exception as e:
        raise HTTPException(500, f"Failed to fetch dialogs: {str(e)}")
    return {"dialogs": dialogs, "total": len(dialogs)}

@router.get("/chat/{chat_id}/files")
async def get_chat_files(chat_id: int):
    await ensure_client_ready()
    try:
        files = await load_chat_files(chat_id)
        total_size = chat_total_size_cache.get(chat_id, 0)
        return {
            "files": files,
            "total": len(files),
            "chat_id": chat_id,
            "total_size_bytes": total_size,
            "total_size_human": format_size(total_size)
        }
    except Exception as e:
        print(f"Error in get_chat_files: {e}")
        return {"files": [], "total": 0, "chat_id": chat_id, "error": str(e), "total_size_bytes": 0, "total_size_human": "0 B"}

@router.get("/stream/{chat_id}/{message_id}")
async def stream_file(request: Request, chat_id: int, message_id: int):
    await ensure_client_ready()
    
    # Coba R2 dulu
    r2_redirect = await check_r2_and_redirect(chat_id, message_id)
    if r2_redirect:
        return r2_redirect
    
    # Fallback direct stream dari Telegram
    msg = await telegram_client.get_messages(chat_id, message_id)
    if not msg:
        raise HTTPException(404, "Message not found")
    
    if msg.video:
        file_id = msg.video.file_id
        file_size = msg.video.file_size
        mime_type = "video/mp4"
    elif msg.audio:
        file_id = msg.audio.file_id
        file_size = msg.audio.file_size
        mime_type = "audio/mpeg"
    elif msg.photo:
        file_id = msg.photo[-1].file_id
        file_size = msg.photo[-1].file_size
        mime_type = "image/jpeg"
    elif msg.document:
        file_id = msg.document.file_id
        file_size = msg.document.file_size
        mime_type = msg.document.mime_type or "video/mp4"
    else:
        raise HTTPException(404, "No media found")
    
    range_header = request.headers.get("range")
    
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

@router.get("/download/{chat_id}/{message_id}")
async def download_file(chat_id: int, message_id: int):
    await ensure_client_ready()
    msg = await telegram_client.get_messages(chat_id, message_id)
    if not msg:
        raise HTTPException(404, "Message not found")
    
    if msg.video:
        file_id = msg.video.file_id
        filename = msg.video.file_name or f"video_{message_id}.mp4"
        mime_type = "video/mp4"
    elif msg.audio:
        file_id = msg.audio.file_id
        filename = msg.audio.file_name or f"audio_{message_id}.mp3"
        mime_type = "audio/mpeg"
    elif msg.photo:
        file_id = msg.photo[-1].file_id
        filename = f"photo_{message_id}.jpg"
        mime_type = "image/jpeg"
    elif msg.document:
        file_id = msg.document.file_id
        filename = msg.document.file_name or f"file_{message_id}"
        mime_type = msg.document.mime_type or "application/octet-stream"
    else:
        raise HTTPException(404, "No downloadable media")
    
    async def generate():
        async for chunk in telegram_client.stream_media(file_id, limit=0):
            yield chunk
    return StreamingResponse(
        generate(),
        media_type=mime_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )