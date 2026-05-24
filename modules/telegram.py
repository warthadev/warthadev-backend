# modules/telegram.py
import os
import asyncio
from typing import Dict, List
from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse
from telethon import TelegramClient, errors
from telethon.sessions import StringSession
from telethon.tl.types import Message

# ========== KONFIGURASI ==========
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

# ========== LIFECYCLE ==========
async def start_client():
    if API_ID and API_HASH and SESSION_STRING:
        if not telegram_client.is_connected():
            await telegram_client.start()
            me = await telegram_client.get_me()
            print(f"✅ Telegram client started as: {me.first_name} ({me.id})")
    else:
        print("❌ Telegram client not started: missing credentials")

async def shutdown_client():
    if telegram_client.is_connected():
        await telegram_client.disconnect()
        print("✅ Telegram client disconnected")

# ========== HELPER: AMBIL FILE DENGAN PAGINASI ==========
async def load_chat_files(chat_id: int, limit: int = 200) -> List[dict]:
    """Ambil media dari chat dengan batch request untuk menghindari expired reference."""
    if chat_id in files_cache:
        return files_cache[chat_id]

    files = []
    try:
        print(f"🔄 Mengambil file dari chat {chat_id}...")
        offset_id = 0
        fetched = 0
        batch_size = 50  # Ambil 50 pesan per request

        while fetched < limit:
            # Ambil batch pesan
            messages = await telegram_client.get_messages(
                chat_id,
                limit=min(batch_size, limit - fetched),
                offset_id=offset_id,
                reverse=False  # dari terbaru ke lama
            )
            if not messages:
                break

            for msg in messages:
                if not msg.media:
                    continue

                # Proses media (sama seperti sebelumnya)
                file_info = extract_file_info(msg)
                if file_info:
                    files.append(file_info)

            # Update offset untuk batch berikutnya
            offset_id = messages[-1].id if messages else 0
            fetched += len(messages)
            print(f"📦 Batch: {len(messages)} pesan, total file sementara: {len(files)}")
            await asyncio.sleep(1)  # Jeda antar batch

        files.reverse()  # Urutan dari lama ke baru (opsional)
        files_cache[chat_id] = files
        print(f"✅ Selesai. Total {len(files)} file dari chat {chat_id}")

    except errors.RPCError as e:
        print(f"❌ Telethon RPC error: {e}")
    except Exception as e:
        print(f"❌ Unexpected error: {e}")
        import traceback
        traceback.print_exc()

    return files

def extract_file_info(msg: Message) -> dict | None:
    """Ekstrak informasi dari message yang berisi media."""
    if not msg.media:
        return None

    date = msg.date.timestamp() if msg.date else None
    file_id = None
    name = None
    size = 0
    duration = None
    width = height = None
    mtype = None

    if msg.video:
        mtype = "video"
        v = msg.video
        name = v.file_name or f"video_{msg.id}.mp4"
        size = v.size
        duration = v.duration
        width = v.width
        height = v.height
        file_id = str(v.id)
    elif msg.audio:
        mtype = "audio"
        a = msg.audio
        name = a.file_name or f"audio_{msg.id}.mp3"
        size = a.size
        duration = a.duration
        file_id = str(a.id)
    elif msg.photo:
        mtype = "image"
        p = msg.photo
        name = f"photo_{msg.id}.jpg"
        size = p.size
        width = p.width
        height = p.height
        file_id = str(p.id)
    elif msg.document:
        doc = msg.document
        mime = doc.mime_type or ""
        fname = doc.file_name or f"file_{msg.id}"
        ext = fname.split('.')[-1].lower() if '.' in fname else ''

        # Tentukan tipe berdasarkan mime atau ekstensi
        if mime.startswith('video/') or ext in ['mp4', 'mkv', 'avi', 'mov', 'webm', 'flv', '3gp']:
            mtype = "video"
        elif mime.startswith('audio/') or ext in ['mp3', 'm4a', 'wav', 'ogg', 'flac', 'aac']:
            mtype = "audio"
        elif mime.startswith('image/') or ext in ['jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp']:
            mtype = "image"
        else:
            mtype = "document"

        name = fname
        size = doc.size
        for attr in doc.attributes:
            if hasattr(attr, 'duration'):
                duration = attr.duration
                break
        file_id = str(doc.id)
    else:
        return None

    return {
        "id": msg.id,
        "name": name,
        "size": size,
        "media_type": mtype,
        "file_id": file_id,
        "duration": duration,
        "width": width,
        "height": height,
        "date": date,
    }

# ========== ENDPOINTS ==========
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
async def get_chat_files(chat_id: int):
    if not telegram_client.is_connected():
        raise HTTPException(503, "Telegram client not ready")
    files = await load_chat_files(chat_id)
    return {"files": files, "total": len(files), "chat_id": chat_id}

@router.get("/stream/{chat_id}/{message_id}")
async def stream_file(chat_id: int, message_id: int):
    if not telegram_client.is_connected():
        raise HTTPException(503, "Telegram client not ready")
    msg = await telegram_client.get_messages(chat_id, ids=message_id)
    if not msg or not msg.media:
        raise HTTPException(404, "Media not found")
    direct_url = await telegram_client.get_direct_download_link(msg.media)
    if not direct_url:
        raise HTTPException(500, "Could not generate direct download link")
    return RedirectResponse(url=direct_url, status_code=302)

@router.get("/download/{chat_id}/{message_id}")
async def download_file(chat_id: int, message_id: int):
    return await stream_file(chat_id, message_id)