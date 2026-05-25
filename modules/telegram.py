# modules/telegram.py
import os
from typing import Dict, List, Optional
from fastapi import APIRouter, HTTPException, Request, BackgroundTasks
from fastapi.responses import RedirectResponse, JSONResponse
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

# Cache untuk files dan ukuran total per chat (tetap dipertahankan untuk listing)
files_cache: Dict[int, List[dict]] = {}
chat_total_size_cache: Dict[int, int] = {}

# Set untuk mencegah upload ganda untuk file yang sama
_uploading_lock = set()

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
            status_code=503,
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

# ========== R2 INTEGRATION (TANPA FALLBACK) ==========
def is_r2_configured() -> bool:
    return all([
        os.environ.get("R2_ACCESS_KEY_ID"),
        os.environ.get("R2_SECRET_ACCESS_KEY"),
        os.environ.get("R2_ACCOUNT_ID"),
        os.environ.get("R2_BUCKET_NAME")
    ])

async def get_r2_stream_url(chat_id: int, message_id: int) -> Optional[str]:
    """
    Cek apakah file sudah ada di R2.
    Return presigned URL jika ada, None jika tidak atau R2 tidak dikonfigurasi.
    """
    if not is_r2_configured():
        return None
    try:
        from modules.r2 import get_r2_client, generate_presigned_url, R2_BUCKET_NAME
        r2 = get_r2_client()
        prefix = f"telegram/{chat_id}/{message_id}/"
        resp = r2.list_objects_v2(Bucket=R2_BUCKET_NAME, Prefix=prefix, MaxKeys=1)
        objects = resp.get("Contents", [])
        if objects:
            key = objects[0]["Key"]
            return generate_presigned_url(r2, key, expires=3600)
        return None
    except Exception as e:
        print(f"R2 check error: {e}")
        return None

async def trigger_upload_to_r2(chat_id: int, message_id: int):
    """
    Background task: upload file dari Telegram ke R2.
    Menggunakan lock agar tidak ada upload ganda untuk file yang sama.
    """
    key = f"{chat_id}/{message_id}"
    if key in _uploading_lock:
        print(f"Upload already in progress for {key}")
        return
    _uploading_lock.add(key)
    try:
        from modules.r2 import upload_telegram_to_r2
        await upload_telegram_to_r2(telegram_client, chat_id, message_id)
        print(f"Background upload completed for {key}")
    except Exception as e:
        print(f"Background upload failed for {key}: {e}")
    finally:
        _uploading_lock.discard(key)

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
async def stream_file(chat_id: int, message_id: int, background_tasks: BackgroundTasks):
    """
    Redirect ke R2 jika file sudah ada.
    Jika belum, trigger upload asinkron dan return 202 Accepted.
    Tidak ada fallback streaming dari server.
    """
    await ensure_client_ready()
    
    r2_url = await get_r2_stream_url(chat_id, message_id)
    if r2_url:
        return RedirectResponse(url=r2_url, status_code=302)
    
    # File belum ada di R2: mulai upload background
    background_tasks.add_task(trigger_upload_to_r2, chat_id, message_id)
    return JSONResponse(
        status_code=202,
        content={
            "status": "uploading",
            "message": "File is being uploaded to R2. Please retry in a few seconds.",
            "retry_after": 5
        },
        headers={"Retry-After": "5"}
    )

@router.get("/download/{chat_id}/{message_id}")
async def download_file(chat_id: int, message_id: int, background_tasks: BackgroundTasks):
    """
    Redirect ke R2 (presigned URL) untuk download langsung.
    Jika belum ada, trigger upload asinkron dan return 202.
    """
    await ensure_client_ready()
    
    r2_url = await get_r2_stream_url(chat_id, message_id)
    if r2_url:
        # Redirect ke R2, browser akan langsung mendownload file
        return RedirectResponse(url=r2_url, status_code=302)
    
    background_tasks.add_task(trigger_upload_to_r2, chat_id, message_id)
    return JSONResponse(
        status_code=202,
        content={
            "status": "uploading",
            "message": "File is being prepared. Please try again later.",
            "retry_after": 5
        },
        headers={"Retry-After": "5"}
    )