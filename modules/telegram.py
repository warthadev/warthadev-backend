# modules/telegram.py
import os
import asyncio
from typing import Dict, List
from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse
from telethon import TelegramClient, errors
from telethon.sessions import StringSession

# ========== KONFIGURASI ==========
API_ID = int(os.environ.get("TELEGRAM_API_ID", 0))
API_HASH = os.environ.get("TELEGRAM_API_HASH", "")
SESSION_STRING = os.environ.get("TELEGRAM_SESSION_STRING", "")

if not API_ID or not API_HASH or not SESSION_STRING:
    print("⚠️ WARNING: TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_SESSION_STRING must be set")

# In-memory client
telegram_client = TelegramClient(
    StringSession(SESSION_STRING),
    API_ID,
    API_HASH,
    connection_retries=3,
    retry_delay=2,
)

# Cache untuk daftar file per chat
files_cache: Dict[int, List[dict]] = {}

# Router
router = APIRouter(prefix="/telegram", tags=["telegram"])

# ========== LIFECYCLE HOOKS ==========
async def start_client():
    """Panggil saat aplikasi startup"""
    if API_ID and API_HASH and SESSION_STRING:
        if not telegram_client.is_connected():
            await telegram_client.start()
            me = await telegram_client.get_me()
            print(f"✅ Telegram client started as: {me.first_name} ({me.id})")
    else:
        print("❌ Telegram client not started: missing credentials")

async def shutdown_client():
    """Panggil saat aplikasi shutdown"""
    if telegram_client.is_connected():
        await telegram_client.disconnect()
        print("✅ Telegram client disconnected")

# ========== HELPER: AMBIL FILE DENGAN DUA LANGKAH ==========
async def load_chat_files(chat_id: int, limit: int = 500) -> List[dict]:
    """Ambil daftar media dari chat dengan pendekatan dua langkah untuk menghindari expired reference."""
    if chat_id in files_cache:
        return files_cache[chat_id]

    files = []
    # Langkah 1: Ambil semua pesan sebagai list terlebih dahulu
    all_messages = []
    try:
        print(f"🔄 Mengambil {limit} pesan dari chat {chat_id}...")
        async for message in telegram_client.iter_messages(chat_id, limit=limit, wait_time=0.5):
            all_messages.append(message)
        print(f"✅ Berhasil mengambil {len(all_messages)} pesan. Memproses file...")

        # Langkah 2: Proses setiap pesan yang mengandung media
        for idx, message in enumerate(all_messages):
            if not message.media:
                continue

            # Inisialisasi variabel
            mtype = None
            name = None
            size = 0
            duration = None
            width = height = None
            file_id = None
            date = message.date.timestamp() if message.date else None

            if message.video:
                mtype = "video"
                video = message.video
                name = video.file_name or f"video_{message.id}.mp4"
                size = video.size
                duration = video.duration
                width = video.width
                height = video.height
                file_id = str(video.id)
            elif message.audio:
                mtype = "audio"
                audio = message.audio
                name = audio.file_name or f"audio_{message.id}.mp3"
                size = audio.size
                duration = audio.duration
                file_id = str(audio.id)
            elif message.photo:
                mtype = "image"
                photo = message.photo
                name = f"photo_{message.id}.jpg"
                size = photo.size
                width = photo.width
                height = photo.height
                file_id = str(photo.id)
            elif message.document:
                doc = message.document
                mime = doc.mime_type or ""
                # Tentukan tipe berdasarkan mime atau ekstensi
                if mime.startswith('video/'):
                    mtype = "video"
                elif mime.startswith('audio/'):
                    mtype = "audio"
                elif mime.startswith('image/'):
                    mtype = "image"
                else:
                    # Cek ekstensi file
                    fname = doc.file_name or f"file_{message.id}"
                    ext = fname.split('.')[-1].lower() if '.' in fname else ''
                    if ext in ['mp4', 'mkv', 'avi', 'mov', 'webm']:
                        mtype = "video"
                    elif ext in ['mp3', 'm4a', 'wav', 'ogg', 'flac']:
                        mtype = "audio"
                    elif ext in ['jpg', 'jpeg', 'png', 'gif', 'webp']:
                        mtype = "image"
                    else:
                        mtype = "document"
                    name = fname
                if not name:
                    name = doc.file_name or f"document_{message.id}"
                size = doc.size
                # Durasi mungkin ada di atribut
                for attr in doc.attributes:
                    if hasattr(attr, 'duration'):
                        duration = attr.duration
                        break
                file_id = str(doc.id)
            else:
                continue

            files.append({
                "id": message.id,
                "name": name,
                "size": size,
                "media_type": mtype,
                "file_id": file_id,
                "duration": duration,
                "width": width,
                "height": height,
                "date": date,
            })

            # Jeda kecil setiap 50 pesan agar tidak kena rate limit
            if idx % 50 == 0 and idx > 0:
                await asyncio.sleep(1)

        # Urutkan dari yang terbaru ke terlama (atau sebaliknya, sesuaikan kebutuhan)
        files.reverse()
        files_cache[chat_id] = files
        print(f"✅ Loaded {len(files)} files from chat {chat_id}")

    except errors.RPCError as e:
        print(f"❌ Telethon RPC error in load_chat_files: {e}")
        files = []
    except Exception as e:
        print(f"❌ Unexpected error in load_chat_files: {e}")
        import traceback
        traceback.print_exc()
        files = []

    return files

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
    """Daftar semua channel/grup yang bisa diakses"""
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
            # Lewati private chat, bot, dll.
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
    """Ambil daftar file dari chat tertentu"""
    if not telegram_client.is_connected():
        raise HTTPException(503, "Telegram client not ready")

    try:
        files = await load_chat_files(chat_id)
        return {"files": files, "total": len(files), "chat_id": chat_id}
    except Exception as e:
        print(f"Error in get_chat_files: {e}")
        return {"files": [], "total": 0, "chat_id": chat_id, "error": str(e)}

@router.get("/stream/{chat_id}/{message_id}")
async def stream_file(chat_id: int, message_id: int):
    """Redirect ke direct download link dari CDN Telegram"""
    if not telegram_client.is_connected():
        raise HTTPException(503, "Telegram client not ready")

    try:
        message = await telegram_client.get_messages(chat_id, ids=message_id)
        if not message or not message.media:
            raise HTTPException(404, "Media not found")

        direct_url = await telegram_client.get_direct_download_link(message.media)
        if not direct_url:
            raise HTTPException(500, "Could not generate direct download link")

        return RedirectResponse(url=direct_url, status_code=302)

    except errors.RPCError as e:
        raise HTTPException(500, f"Telegram RPC error: {str(e)}")
    except Exception as e:
        raise HTTPException(500, f"Unexpected error: {str(e)}")

@router.get("/download/{chat_id}/{message_id}")
async def download_file(chat_id: int, message_id: int):
    """Redirect ke direct link (sama seperti stream)"""
    return await stream_file(chat_id, message_id)