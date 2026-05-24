# modules/telegram.py
import os
from typing import Dict, Tuple
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse, Response
from pyrogram import Client
import nest_asyncio

nest_asyncio.apply()

router = APIRouter(prefix="/telegram", tags=["telegram"])

SESSION_STRING = os.environ.get("TELEGRAM_SESSION_STRING", "")

if not SESSION_STRING:
    print("WARNING: TELEGRAM_SESSION_STRING not set!")

_file_cache: Dict[str, Tuple[int, int, int, str]] = {}

async def get_client() -> Client:
    return Client("telegram_manager", session_string=SESSION_STRING, in_memory=True)

@router.get("/health")
async def health():
    return {
        "status": "healthy",
        "configured": bool(SESSION_STRING),
        "session_length": len(SESSION_STRING)
    }

@router.get("/dialogs")
async def get_dialogs():
    if not SESSION_STRING:
        raise HTTPException(status_code=500, detail="TELEGRAM_SESSION_STRING not configured")
    
    try:
        async with await get_client() as client:
            dialogs = []
            async for dialog in client.get_dialogs():
                chat = dialog.chat
                if chat.type in ["group", "supergroup", "channel"]:
                    dialogs.append({
                        "id": chat.id,
                        "title": chat.title,
                        "type": str(chat.type),
                        "unread_count": dialog.unread_count or 0,
                    })
            return {"dialogs": dialogs}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/chat/{chat_id}/files")
async def get_chat_files(chat_id: int, limit: int = 100):
    if not SESSION_STRING:
        raise HTTPException(status_code=500, detail="TELEGRAM_SESSION_STRING not configured")
    
    try:
        async with await get_client() as client:
            files = []
            async for message in client.get_chat_history(chat_id, limit=limit):
                if message.document:
                    doc = message.document
                    _file_cache[doc.file_id] = (chat_id, message.id, doc.file_size, doc.mime_type or "application/octet-stream")
                    files.append({
                        "id": str(message.id),
                        "name": doc.file_name or f"file_{message.id}",
                        "size": doc.file_size,
                        "file_id": doc.file_id,
                        "mime_type": doc.mime_type or "application/octet-stream",
                        "date": message.date.timestamp(),
                        "chat_id": chat_id,
                    })
                elif message.video:
                    vid = message.video
                    _file_cache[vid.file_id] = (chat_id, message.id, vid.file_size, "video/mp4")
                    files.append({
                        "id": str(message.id),
                        "name": vid.file_name or f"video_{message.id}.mp4",
                        "size": vid.file_size,
                        "file_id": vid.file_id,
                        "mime_type": "video/mp4",
                        "date": message.date.timestamp(),
                        "chat_id": chat_id,
                    })
                elif message.audio:
                    aud = message.audio
                    _file_cache[aud.file_id] = (chat_id, message.id, aud.file_size, "audio/mpeg")
                    files.append({
                        "id": str(message.id),
                        "name": aud.file_name or f"audio_{message.id}.mp3",
                        "size": aud.file_size,
                        "file_id": aud.file_id,
                        "mime_type": "audio/mpeg",
                        "date": message.date.timestamp(),
                        "chat_id": chat_id,
                    })
                elif message.photo:
                    photo = message.photo[-1]
                    _file_cache[photo.file_id] = (chat_id, message.id, photo.file_size, "image/jpeg")
                    files.append({
                        "id": str(message.id),
                        "name": f"photo_{message.id}.jpg",
                        "size": photo.file_size,
                        "file_id": photo.file_id,
                        "mime_type": "image/jpeg",
                        "date": message.date.timestamp(),
                        "chat_id": chat_id,
                    })
            return {"files": files}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/stream/{file_id}")
async def stream_file(request: Request, file_id: str):
    if not SESSION_STRING:
        raise HTTPException(status_code=500, detail="TELEGRAM_SESSION_STRING not configured")
    
    try:
        cached = _file_cache.get(file_id)
        file_size = cached[2] if cached else None
        mime_type = cached[3] if cached else "video/mp4"
        
        async def generate():
            async with await get_client() as client:
                async for chunk in client.stream_media(file_id, limit=0):
                    yield chunk
        
        return StreamingResponse(generate(), media_type=mime_type)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))