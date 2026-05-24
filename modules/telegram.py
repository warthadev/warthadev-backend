# modules/telegram.py
import os
from typing import Dict, Tuple, Optional
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse, Response
from pyrogram import Client

router = APIRouter(prefix="/telegram", tags=["telegram"])

SESSION_STRING = os.environ.get("TELEGRAM_SESSION_STRING", "")

_file_cache: Dict[str, Tuple[int, int, int, str, str]] = {}  # file_id -> (chat_id, msg_id, size, mime, media_type)

async def get_client() -> Client:
    return Client("telegram_manager", session_string=SESSION_STRING, in_memory=True, no_updates=True)

def get_media_type(filename: str) -> str:
    ext = filename.split('.')[-1].lower()
    video = ['mp4', 'mkv', 'avi', 'mov', 'webm', 'flv', 'm4v', '3gp', 'wmv', 'ts']
    audio = ['mp3', 'm4a', 'wav', 'ogg', 'flac', 'aac', 'opus', 'wma']
    image = ['jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp', 'svg', 'ico']
    archive = ['zip', 'rar', '7z', 'tar', 'gz']
    if ext in video: return 'video'
    if ext in audio: return 'audio'
    if ext in image: return 'image'
    if ext in archive: return 'archive'
    return 'file'

def get_mime_type(filename: str) -> str:
    ext = filename.split('.')[-1].lower()
    mime_map = {
        'mp4': 'video/mp4', 'mkv': 'video/x-matroska', 'avi': 'video/x-msvideo',
        'mp3': 'audio/mpeg', 'm4a': 'audio/mp4', 'wav': 'audio/wav', 'ogg': 'audio/ogg',
        'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'png': 'image/png', 'gif': 'image/gif',
        'webp': 'image/webp', 'zip': 'application/zip', 'pdf': 'application/pdf',
        'txt': 'text/plain', 'json': 'application/json'
    }
    return mime_map.get(ext, 'application/octet-stream')

@router.get("/health")
async def health():
    return {"status": "healthy", "configured": bool(SESSION_STRING)}

@router.get("/dialogs")
async def get_dialogs():
    if not SESSION_STRING:
        raise HTTPException(500, "No session")
    async with await get_client() as client:
        dialogs = []
        async for dialog in client.get_dialogs():
            chat = dialog.chat
            if chat.type in ["group", "supergroup", "channel"]:
                dialogs.append({
                    "id": chat.id,
                    "name": chat.title,
                    "type": str(chat.type).split('.')[-1]
                })
        return {"dialogs": dialogs}

@router.get("/chat/{chat_id}/files")
async def get_chat_files(chat_id: int, limit: int = 200):
    if not SESSION_STRING:
        raise HTTPException(500, "No session")
    async with await get_client() as client:
        files = []
        async for msg in client.get_chat_history(chat_id, limit=limit):
            file_info = None
            if msg.document:
                file_info = {
                    "id": msg.id,
                    "name": msg.document.file_name or f"file_{msg.id}",
                    "size": msg.document.file_size,
                    "file_id": msg.document.file_id,
                    "media_type": get_media_type(msg.document.file_name or ""),
                    "mime_type": msg.document.mime_type or get_mime_type(msg.document.file_name or "")
                }
                _file_cache[msg.document.file_id] = (chat_id, msg.id, msg.document.file_size, file_info["mime_type"], file_info["media_type"])
            elif msg.video:
                file_info = {
                    "id": msg.id,
                    "name": msg.video.file_name or f"video_{msg.id}.mp4",
                    "size": msg.video.file_size,
                    "file_id": msg.video.file_id,
                    "media_type": "video",
                    "mime_type": "video/mp4"
                }
                _file_cache[msg.video.file_id] = (chat_id, msg.id, msg.video.file_size, "video/mp4", "video")
            elif msg.audio:
                file_info = {
                    "id": msg.id,
                    "name": msg.audio.file_name or f"audio_{msg.id}.mp3",
                    "size": msg.audio.file_size,
                    "file_id": msg.audio.file_id,
                    "media_type": "audio",
                    "mime_type": "audio/mpeg"
                }
                _file_cache[msg.audio.file_id] = (chat_id, msg.id, msg.audio.file_size, "audio/mpeg", "audio")
            elif msg.photo:
                photo = msg.photo[-1]
                file_info = {
                    "id": msg.id,
                    "name": f"photo_{msg.id}.jpg",
                    "size": photo.file_size,
                    "file_id": photo.file_id,
                    "media_type": "image",
                    "mime_type": "image/jpeg"
                }
                _file_cache[photo.file_id] = (chat_id, msg.id, photo.file_size, "image/jpeg", "image")
            
            if file_info:
                files.append(file_info)
        files.reverse()
        return {"files": files, "chat_id": chat_id, "count": len(files)}

@router.get("/stream/{chat_id}/{message_id}")
async def stream_file(request: Request, chat_id: int, message_id: int):
    if not SESSION_STRING:
        raise HTTPException(500, "No session")
    
    async with await get_client() as client:
        msg = await client.get_messages(chat_id, message_id)
        if not msg:
            raise HTTPException(404, "File not found")
        
        # Ambil file dari berbagai tipe
        if msg.document:
            file_id = msg.document.file_id
            file_size = msg.document.file_size
            mime_type = msg.document.mime_type or get_mime_type(msg.document.file_name or "")
            filename = msg.document.file_name
        elif msg.video:
            file_id = msg.video.file_id
            file_size = msg.video.file_size
            mime_type = "video/mp4"
            filename = msg.video.file_name or f"video_{message_id}.mp4"
        elif msg.audio:
            file_id = msg.audio.file_id
            file_size = msg.audio.file_size
            mime_type = "audio/mpeg"
            filename = msg.audio.file_name or f"audio_{message_id}.mp3"
        elif msg.photo:
            photo = msg.photo[-1]
            file_id = photo.file_id
            file_size = photo.file_size
            mime_type = "image/jpeg"
            filename = f"photo_{message_id}.jpg"
        else:
            raise HTTPException(404, "No media found")
        
        range_header = request.headers.get("range")
        
        async def generate():
            async for chunk in client.stream_media(file_id, limit=0):
                yield chunk
        
        # Untuk preview gambar, set header yang sesuai
        if mime_type.startswith('image/'):
            return StreamingResponse(generate(), media_type=mime_type, headers={"Cache-Control": "public, max-age=3600"})
        
        # Untuk video/audio dengan seeking
        if range_header and file_size:
            try:
                range_val = range_header.replace("bytes=", "")
                start, end = range_val.split("-")
                start = int(start)
                end = int(end) if end else file_size - 1
                length = end - start + 1
                
                CHUNK_SIZE = 1024 * 1024
                start_chunk = start // CHUNK_SIZE
                end_chunk = (end // CHUNK_SIZE) + 1
                
                async def seek_generator():
                    streamed = 0
                    async for chunk in client.stream_media(file_id, offset=start_chunk, limit=end_chunk - start_chunk):
                        if streamed >= length:
                            break
                        chunk_start = start - (start_chunk * CHUNK_SIZE)
                        if streamed == 0 and chunk_start > 0:
                            chunk = chunk[chunk_start:]
                        if streamed + len(chunk) > length:
                            chunk = chunk[:length - streamed]
                        yield chunk
                        streamed += len(chunk)
                
                return StreamingResponse(
                    seek_generator(),
                    status_code=206,
                    media_type=mime_type,
                    headers={
                        "Content-Range": f"bytes {start}-{end}/{file_size}",
                        "Accept-Ranges": "bytes",
                        "Content-Length": str(length),
                    }
                )
            except:
                pass
        
        return StreamingResponse(generate(), media_type=mime_type, headers={"Accept-Ranges": "bytes"})

@router.get("/download/{chat_id}/{message_id}")
async def download_file(chat_id: int, message_id: int):
    if not SESSION_STRING:
        raise HTTPException(500, "No session")
    
    async with await get_client() as client:
        msg = await client.get_messages(chat_id, message_id)
        if not msg:
            raise HTTPException(404, "File not found")
        
        if msg.document:
            file_id = msg.document.file_id
            filename = msg.document.file_name or f"file_{message_id}"
        elif msg.video:
            file_id = msg.video.file_id
            filename = msg.video.file_name or f"video_{message_id}.mp4"
        elif msg.audio:
            file_id = msg.audio.file_id
            filename = msg.audio.file_name or f"audio_{message_id}.mp3"
        elif msg.photo:
            file_id = msg.photo[-1].file_id
            filename = f"photo_{message_id}.jpg"
        else:
            raise HTTPException(404, "No file found")
        
        async def generate():
            async for chunk in client.stream_media(file_id, limit=0):
                yield chunk
        
        return StreamingResponse(
            generate(),
            media_type="application/octet-stream",
            headers={"Content-Disposition": f"attachment; filename=\"{filename}\""}
        )