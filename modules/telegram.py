import logging
from typing import List, Dict, Optional
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse, Response
from pyrogram import Client, errors
import io

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/telegram")

# ========== KONFIGURASI ==========
API_ID = 25590547
API_HASH = 'cea88887e3f1eca7b048bb85fe97f5be'
SESSION_STRING = 'BQGGexMATYDTitbHfX-xdLrAob2StdELEBSI281hi7tzYqM2F9IANhltWG9pU2eFNch-dwLAWBsJGTacHUlzWl3EHw2Gt2hzH7M1Uya74QyquOm7lGa3Zfz2iIfl4CmZkQ4taZkM1Tr2pBfSWNtIEoRLArgGfrl0-jDdsPx_kKPOpEftdgFidrPmVUv9rS1OHLKXCrGF3KhV9AZIqNw5cS5TqiHTtiubkD-ECSYL9RtcG-wbY3flfXyRjel5X1SULwzBQBC2PyhTdHwZCENa-FzodMv9Wcym6NQV9tsqyQ19o_lEstkQ2mWiFR6zJR4S9bwDtVJaJ4aYOeW4VzG_OvjfGoAyrwAAAAGQadPXAA'

# 🔥 GANTI DENGAN USERNAME ATAU ID CHANNEL KAMU
# Cara 1: Pakai username channel (contoh: "warthavideo" tanpa @)
# Cara 2: Pakai ID numerik dari Colab (contoh: -1002466984537)
RECIPIENT = "-1002466984537"  # <-- GANTI SESUAI


async def get_client():
    """Membuat koneksi Pyrogram (in-memory)"""
    return Client(
        "telegram_stream",
        api_id=API_ID,
        api_hash=API_HASH,
        session_string=SESSION_STRING,
        in_memory=True
    )


@router.get("/files")
async def get_files(limit: int = Query(100, ge=1, le=500)):
    """Mengambil daftar file video dari channel Telegram"""
    try:
        async with await get_client() as client:
            logger.info(f"Mencoba mengakses channel: {RECIPIENT}")
            
            # Coba resolve channel
            try:
                # Jika RECIPIENT numeric string, konversi ke int
                if RECIPIENT.startswith('-'):
                    chat_id = int(RECIPIENT)
                else:
                    chat_id = RECIPIENT
                    
                chat = await client.get_chat(chat_id)
                logger.info(f"✅ Channel ditemukan: {chat.title} (ID: {chat.id})")
            except errors.PeerIdInvalid:
                # Coba cari dari daftar dialog
                logger.info("Mencari dari daftar dialog...")
                found = False
                async for dialog in client.get_dialogs():
                    if str(dialog.chat.id) == RECIPIENT or dialog.chat.username == RECIPIENT:
                        chat = dialog.chat
                        found = True
                        logger.info(f"✅ Ditemukan dari dialog: {chat.title}")
                        break
                
                if not found:
                    raise HTTPException(
                        status_code=404,
                        detail=f"Channel {RECIPIENT} tidak ditemukan. Pastikan akun sudah join channel."
                    )
            except Exception as e:
                raise HTTPException(
                    status_code=404,
                    detail=f"Channel tidak dapat diakses: {str(e)}"
                )
            
            # Ambil pesan dari channel
            messages = []
            async for message in client.get_chat_history(chat.id, limit=limit):
                if message.document and message.document.mime_type:
                    mime = message.document.mime_type.lower()
                    if mime.startswith('video/'):
                        messages.append({
                            "id": message.id,
                            "name": message.document.file_name or f"video_{message.id}.mp4",
                            "size": message.document.file_size,
                            "mime_type": mime,
                            "file_id": message.document.file_id,
                            "date": message.date.timestamp() if message.date else None,
                            "caption": message.caption or ""
                        })
            
            logger.info(f"✅ Ditemukan {len(messages)} video")
            return messages
            
    except HTTPException:
        raise
    except errors.Unauthorized:
        raise HTTPException(status_code=401, detail="Session string tidak valid. Generate ulang di Colab.")
    except Exception as e:
        logger.error(f"❌ Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/stream/{file_id}")
async def stream_video(file_id: str, range: Optional[str] = None):
    """Streaming video dengan dukungan HTTP Range"""
    try:
        async with await get_client() as client:
            logger.info(f"📥 Streaming file: {file_id}")
            
            # Download file ke memory
            file_stream = io.BytesIO()
            await client.download_media(file_id, file=file_stream)
            file_stream.seek(0)
            file_size = file_stream.getbuffer().nbytes
            
            logger.info(f"File size: {file_size} bytes")
            
            # Handle range request (untuk seeking video)
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
                # Kirim seluruh file
                return StreamingResponse(
                    file_stream,
                    media_type="video/mp4",
                    headers={
                        "Accept-Ranges": "bytes",
                        "Content-Length": str(file_size)
                    }
                )
                
    except errors.exceptions.not_acceptable_406.MediaEmpty:
        logger.error(f"❌ File tidak ditemukan: {file_id}")
        raise HTTPException(status_code=404, detail="File tidak ditemukan")
    except Exception as e:
        logger.error(f"❌ Streaming error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/health")
async def health_check():
    """Cek koneksi ke Telegram"""
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
        return {
            "status": "error",
            "message": str(e)
        }