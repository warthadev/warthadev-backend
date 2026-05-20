import logging
from typing import List, Dict, Optional
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse, Response
from pyrogram import Client, errors
import io
import os

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/telegram")

# ========== KONFIGURASI ==========
API_ID = 25590547
API_HASH = 'cea88887e3f1eca7b048bb85fe97f5be'
SESSION_STRING = 'BQGGexMATYDTitbHfX-xdLrAob2StdELEBSI281hi7tzYqM2F9IANhltWG9pU2eFNch-dwLAWBsJGTacHUlzWl3EHw2Gt2hzH7M1Uya74QyquOm7lGa3Zfz2iIfl4CmZkQ4taZkM1Tr2pBfSWNtIEoRLArgGfrl0-jDdsPx_kKPOpEftdgFidrPmVUv9rS1OHLKXCrGF3KhV9AZIqNw5cS5TqiHTtiubkD-ECSYL9RtcG-wbY3flfXyRjel5X1SULwzBQBC2PyhTdHwZCENa-FzodMv9Wcym6NQV9tsqyQ19o_lEstkQ2mWiFR6zJR4S9bwDtVJaJ4aYOeW4VzG_OvjfGoAyrwAAAAGQadPXAA'
RECIPIENT = -1002466984537  # ID channel private kamu

# Flag untuk tracking apakah sudah pernah kirim pesan
PESAN_TERKIRIM_FILE = "/tmp/telegram_pesan_terkirim.txt"


async def get_client():
    return Client(
        "telegram_stream",
        api_id=API_ID,
        api_hash=API_HASH,
        session_string=SESSION_STRING,
        in_memory=True
    )


async def ensure_channel_recognized(client):
    """Pastikan channel sudah 'dikenal' dengan mengirim pesan sekali saja"""
    # Cek apakah sudah pernah kirim pesan
    if os.path.exists(PESAN_TERKIRIM_FILE):
        logger.info("✅ Sudah pernah kirim pesan ke channel sebelumnya")
        return
    
    try:
        logger.info(f"📤 Mencoba mengirim pesan test ke channel {RECIPIENT}...")
        await client.send_message(RECIPIENT, "Connection test from Wartha Sensei API")
        
        # Tandai sudah pernah kirim
        with open(PESAN_TERKIRIM_FILE, "w") as f:
            f.write("done")
        logger.info("✅ Pesan test berhasil dikirim. Channel sekarang dikenal!")
    except Exception as e:
        logger.warning(f"⚠️ Gagal kirim pesan test: {e}")
        # Lanjutkan saja, mungkin channel sudah dikenal


@router.get("/files", response_model=List[Dict])
async def get_files(limit: int = Query(100, ge=1, le=500)):
    try:
        async with await get_client() as client:
            # 🔥 KRUSIAL: Pastikan channel dikenal
            await ensure_channel_recognized(client)
            
            # Sekarang ambil chat
            chat = await client.get_chat(RECIPIENT)
            logger.info(f"✅ Channel ditemukan: {chat.title}")
            
            # Ambil pesan
            messages = []
            async for message in client.get_chat_history(chat.id, limit=limit):
                if message.document and message.document.mime_type and message.document.mime_type.startswith('video/'):
                    messages.append({
                        "id": message.id,
                        "name": message.document.file_name or f"video_{message.id}.mp4",
                        "size": message.document.file_size,
                        "file_id": message.document.file_id,
                        "date": message.date.timestamp() if message.date else None,
                    })
            
            logger.info(f"✅ Ditemukan {len(messages)} video")
            return messages
            
    except errors.PeerIdInvalid:
        raise HTTPException(
            status_code=404,
            detail="Channel tidak dikenal. Coba jalankan ulang atau pastikan akun Telegram adalah admin channel."
        )
    except Exception as e:
        logger.error(f"❌ Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/stream/{file_id}")
async def stream_video(file_id: str, range: Optional[str] = None):
    try:
        async with await get_client() as client:
            file_stream = io.BytesIO()
            await client.download_media(file_id, file=file_stream)
            file_stream.seek(0)
            file_size = file_stream.getbuffer().nbytes

            if range and range.startswith("bytes="):
                range_value = range.replace("bytes=", "")
                parts = range_value.split("-")
                start = int(parts[0])
                end = int(parts[1]) if parts[1] and parts[1].strip() else file_size - 1

                if start >= file_size or end >= file_size:
                    raise HTTPException(status_code=416, detail="Range Not Satisfiable")

                chunk_size = end - start + 1
                file_stream.seek(start)
                data = file_stream.read(chunk_size)

                headers = {
                    "Content-Range": f"bytes {start}-{end}/{file_size}",
                    "Accept-Ranges": "bytes",
                    "Content-Length": str(chunk_size),
                    "Content-Type": "video/mp4",
                }
                return Response(content=data, status_code=206, headers=headers)
            else:
                return StreamingResponse(
                    file_stream,
                    media_type="video/mp4",
                    headers={"Accept-Ranges": "bytes", "Content-Length": str(file_size)}
                )
    except Exception as e:
        logger.error(f"❌ Streaming error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/health")
async def health_check():
    try:
        async with await get_client() as client:
            me = await client.get_me()
            return {
                "status": "connected",
                "user": me.first_name,
                "username": me.username,
                "user_id": me.id
            }
    except Exception as e:
        return {"status": "error", "message": str(e)}