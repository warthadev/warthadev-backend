# modules/telegram.py
import os
from typing import Dict, List, Optional
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from telethon import TelegramClient, errors
from telethon.sessions import StringSession

# ========== KONFIGURASI ==========
API_ID = int(os.environ.get("TELEGRAM_API_ID", 0))
API_HASH = os.environ.get("TELEGRAM_API_HASH", "")
SESSION_STRING = os.environ.get("TELEGRAM_SESSION_STRING", "")

# ========== INISIALISASI CLIENT ==========
telegram_client = TelegramClient(
    StringSession(SESSION_STRING),
    API_ID,
    API_HASH,
    connection_retries=5,
    retry_delay=3
)

# Cache untuk daftar file
files_cache: Dict[int, List[dict]] = {}
router = APIRouter(prefix="/telegram", tags=["telegram"])

# ========== LIFECYCLE & FUNGSI BANTUAN ==========
async def start_client():
    if API_ID and API_HASH and SESSION_STRING and not telegram_client.is_connected():
        await telegram_client.start()
        me = await telegram_client.get_me()
        print(f"✅ Telegram client started as: {me.first_name}")

async def shutdown_client():
    if telegram_client.is_connected():
        await telegram_client.disconnect()
        print("Telegram client disconnected")

def format_file_size(bytes_size: int) -> str:
    # ... (fungsi format ukuran, sama seperti sebelumnya)
    pass

async def load_chat_files(chat_id: int, limit: int = 500) -> List[dict]:
    # ... (fungsi untuk memuat daftar file dari chat, sama seperti sebelumnya)
    pass

# ========== ENDPOINT STREAMING UTAMA ==========
@router.get("/stream/{chat_id}/{message_id}")
async def stream_file(request: Request, chat_id: int, message_id: int):
    # 1. Cek koneksi
    if not telegram_client.is_connected():
        raise HTTPException(503, "Telegram client not ready")
    
    # 2. Ambil pesan berdasarkan ID
    msg = await telegram_client.get_messages(chat_id, ids=message_id)
    if not msg or not msg.media:
        raise HTTPException(404, "Media not found")
    
    # 3. Dapatkan informasi file (ukuran, ID, dll.)
    file_size = msg.file.size if msg.file else 0
    file_id = msg.file.id if msg.file else None
    mime_type = msg.file.mime_type if msg.file else "application/octet-stream"
    file_name = msg.file.name if msg.file and msg.file.name else f"file_{message_id}"

    # 4. Proses permintaan HTTP range header
    range_header = request.headers.get("range")
    
    if not range_header:
        # Mode default: Kirim header informasi ukuran file (tanpa data)
        return StreamingResponse(
            content=iter(()),  # generator kosong
            status_code=200,
            headers={
                "Accept-Ranges": "bytes",
                "Content-Length": str(file_size),
                "Content-Type": mime_type,
                "Content-Disposition": f'inline; filename="{file_name}"'
            }
        )
    
    # 5. Mode seeking: Ambil dan kirim potongan data yang diminta
    # Contoh: parse "bytes=0-1023" -> start=0, end=1023
    range_val = range_header.replace("bytes=", "")
    start_str, end_str = range_val.split("-")
    start_byte = int(start_str)
    end_byte = int(end_str) if end_str else file_size - 1
    requested_length = end_byte - start_byte + 1
    
    # Konversi ke chunk Telethon (offset dalam 4096 byte)
    CHUNK_SIZE = 4096  # Minimum chunk size untuk API Telegram
    start_chunk = start_byte // CHUNK_SIZE
    # offset_start = start_byte % CHUNK_SIZE  # untuk slicing nanti
    
    async def generate_chunk():
        # Minta hanya satu chunk data dari Telegram
        async for chunk in telegram_client.iter_download(
            msg.media,
            offset=start_chunk,
            request_size=requested_length
        ):
            # Kirim potongan yang diminta oleh browser
            # Potong jika hanya diperlukan sebagian dari chunk ini
            # yield chunk[start_offset:start_offset+requested_length]
            yield chunk[:requested_length]
            break  # Hanya ambil satu chunk yang cukup
        return

    return StreamingResponse(
        generate_chunk(),
        status_code=206,
        media_type=mime_type,
        headers={
            "Content-Range": f"bytes {start_byte}-{end_byte}/{file_size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(requested_length),
            "Content-Type": mime_type,
            "Cache-Control": "no-cache"
        }
    )