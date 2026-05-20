import logging
from typing import List, Dict, Optional
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse, Response
from pyrogram import Client, errors
import io

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/telegram")

# ========== KONFIGURASI (SALIN DARI COLAB) ==========
API_ID = 25590547
API_HASH = 'cea88887e3f1eca7b048bb85fe97f5be'
SESSION_STRING = 'BQGGexMATYDTitbHfX-xdLrAob2StdELEBSI281hi7tzYqM2F9IANhltWG9pU2eFNch-dwLAWBsJGTacHUlzWl3EHw2Gt2hzH7M1Uya74QyquOm7lGa3Zfz2iIfl4CmZkQ4taZkM1Tr2pBfSWNtIEoRLArgGfrl0-jDdsPx_kKPOpEftdgFidrPmVUv9rS1OHLKXCrGF3KhV9AZIqNw5cS5TqiHTtiubkD-ECSYL9RtcG-wbY3flfXyRjel5X1SULwzBQBC2PyhTdHwZCENa-FzodMv9Wcym6NQV9tsqyQ19o_lEstkQ2mWiFR6zJR4S9bwDtVJaJ4aYOeW4VzG_OvjfGoAyrwAAAAGQadPXAA'

# 🔥 PAKAI CHANNEL_ID DARI COLAB (tanpa tanda kutip)
RECIPIENT = -1002466984537


async def get_client():
    return Client(
        "telegram_stream",
        api_id=API_ID,
        api_hash=API_HASH,
        session_string=SESSION_STRING,
        in_memory=True
    )


@router.get("/files", response_model=List[Dict])
async def get_files(limit: int = Query(100, ge=1, le=500)):
    try:
        async with await get_client() as client:
            logger.info(f"Mencoba mengakses channel ID: {RECIPIENT}")
            
            # Cek apakah channel bisa diakses
            try:
                chat = await client.get_chat(RECIPIENT)
                logger.info(f"✅ Channel ditemukan: {chat.title} (ID: {chat.id})")
            except Exception as e:
                logger.error(f"❌ Gagal akses channel: {e}")
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
        raise HTTPException(status_code=401, detail="Session string tidak valid")
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