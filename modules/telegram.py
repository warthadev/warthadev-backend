import os
import logging
from typing import List, Dict, Optional
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse, Response
from pyrogram import Client, errors
import asyncio
import io

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/telegram")

# ========== KONFIGURASI (SALIN DARI COLAB KAMU) ==========
API_ID = 25590547
API_HASH = 'cea88887e3f1eca7b048bb85fe97f5be'
SESSION_STRING = 'BQGGexMAqHzxeFmGfH4k-uad-94AFpXM35QuPJq-eB7kUETvDKvCIEiL85u9M4kr_5D7HILV6iEVNB8D1gARoBvzDe3HzLp2ccF1gnrVr4t1zwEHgT1xV_Znevb3tRgUPwVHJCtyq84UBd4Nw4g8ubzsZsKsCalC5F0SqmAblHST7cqDUai89uKJ0j43OHtfq8KFZh4z3Mo4n3dMsXTnil_Kls7-CRgFwg7Xi2Sy9gCiJNWid1Cv0XfSjnO-xPFDPL1PvLSjD0oMNej0c7fnr1j_oKJeP-i5x1majizu9JAp2uXu6A8fMFiEYLj5Yzzay9wxZHl0YHGoN_4LqIMHgRVmQiQ6vgAAAAGQadPXAA'
RECIPIENT = "-1003671755437"  # ID channel tujuan

# Cache sederhana untuk menyimpan daftar file (optional)
files_cache = []
cache_timestamp = 0


async def get_client():
    """Membuat koneksi Pyrogram (in-memory agar tidak menyimpan file)"""
    return Client(
        "telegram_stream",
        api_id=API_ID,
        api_hash=API_HASH,
        session_string=SESSION_STRING,
        in_memory=True  # 🔥 Penting: tidak menyimpan session ke disk
    )


@router.get("/files", response_model=List[Dict])
async def get_files(limit: int = Query(100, ge=1, le=500)):
    """
    Mengambil daftar file video dari channel Telegram.
    Endpoint: GET /telegram/files?limit=100
    """
    try:
        async with await get_client() as client:
            messages = []
            async for message in client.get_chat_history(RECIPIENT, limit=limit):
                # Cek apakah ini file document dan mime_type-nya video
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
            return messages
    except errors.Unauthorized:
        raise HTTPException(status_code=401, detail="Session string tidak valid")
    except Exception as e:
        logger.error(f"Gagal mengambil daftar file: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/stream/{file_id}")
async def stream_video(file_id: str, range: Optional[str] = None):
    """
    Streaming video dengan dukungan HTTP Range (bisa seek/lompat).
    Endpoint: GET /telegram/stream/{file_id}
    
    Client bisa request dengan header:
    Range: bytes=0-1024
    """
    try:
        async with await get_client() as client:
            # Download file ke memory (BytesIO)
            file_stream = io.BytesIO()
            await client.download_media(file_id, file=file_stream)
            file_stream.seek(0)
            
            file_size = file_stream.getbuffer().nbytes
            
            # Parsing Range header
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
                # Tidak ada range header → kirim seluruh file
                return StreamingResponse(
                    file_stream,
                    media_type="video/mp4",
                    headers={
                        "Accept-Ranges": "bytes",
                        "Content-Length": str(file_size)
                    }
                )
    except errors.exceptions.not_acceptable_406.MediaEmpty:
        raise HTTPException(status_code=404, detail="File tidak ditemukan")
    except Exception as e:
        logger.error(f"Streaming error: {e}")
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
                "username": me.username
            }
    except Exception as e:
        return {
            "status": "error",
            "message": str(e)
        }