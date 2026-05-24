# modules/telegram.py
import os
from typing import Dict, Tuple, Optional, List, Any
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse, Response
from pyrogram import Client

router = APIRouter(prefix="/telegram", tags=["telegram"])

SESSION_STRING = os.environ.get("TELEGRAM_SESSION_STRING", "")

if not SESSION_STRING:
    print("⚠️ WARNING: TELEGRAM_SESSION_STRING not set!")

# Cache
_file_cache: Dict[str, Tuple[int, int, int, str, str]] = {}
_dialogs_cache: List[Dict] = []
_dialogs_loaded = False

async def get_client() -> Client:
    """Buat instance Pyrogram client"""
    return Client(
        "telegram_manager",
        session_string=SESSION_STRING,
        in_memory=True,
        no_updates=True,
        workdir="."
    )

def get_media_type(filename: str, mime_type: str = "") -> str:
    """Tentukan tipe media berdasarkan ekstensi file"""
    ext = filename.split('.')[-1].lower() if '.' in filename else ''
    
    video_exts = ['mp4', 'mkv', 'avi', 'mov', 'webm', 'flv', 'm4v', '3gp', 'wmv', 'ts']
    audio_exts = ['mp3', 'm4a', 'wav', 'ogg', 'flac', 'aac', 'opus', 'wma']
    image_exts = ['jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp', 'svg', 'ico']
    archive_exts = ['zip', 'rar', '7z', 'tar', 'gz', 'bz2']
    
    if ext in video_exts:
        return 'video'
    if ext in audio_exts:
        return 'audio'
    if ext in image_exts:
        return 'image'
    if ext in archive_exts:
        return 'archive'
    return 'file'

def get_mime_type(filename: str) -> str:
    """Dapatkan MIME type dari file"""
    ext = filename.split('.')[-1].lower() if '.' in filename else ''
    mime_map = {
        'mp4': 'video/mp4', 'mkv': 'video/x-matroska', 'avi': 'video/x-msvideo',
        'mov': 'video/quicktime', 'webm': 'video/webm', 'mp3': 'audio/mpeg',
        'm4a': 'audio/mp4', 'wav': 'audio/wav', 'ogg': 'audio/ogg',
        'flac': 'audio/flac', 'jpg': 'image/jpeg', 'jpeg': 'image/jpeg',
        'png': 'image/png', 'gif': 'image/gif', 'webp': 'image/webp',
        'zip': 'application/zip', 'rar': 'application/vnd.rar',
        '7z': 'application/x-7z-compressed', 'pdf': 'application/pdf',
        'txt': 'text/plain', 'json': 'application/json'
    }
    return mime_map.get(ext, 'application/octet-stream')

# ============ ENDPOINTS ============

@router.get("/health")
async def health():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "configured": bool(SESSION_STRING),
        "session_length": len(SESSION_STRING) if SESSION_STRING else 0
    }

@router.get("/dialogs")
async def get_dialogs():
    """Dapatkan daftar semua grup/channel"""
    global _dialogs_cache, _dialogs_loaded
    
    if not SESSION_STRING:
        raise HTTPException(500, "TELEGRAM_SESSION_STRING not configured")
    
    if _dialogs_loaded:
        return {"dialogs": _dialogs_cache}
    
    try:
        async with await get_client() as client:
            dialogs = []
            async for dialog in client.get_dialogs():
                chat = dialog.chat
                if chat.type in ["group", "supergroup", "channel"]:
                    dialogs.append({
                        "id": chat.id,
                        "name": chat.title,
                        "type": str(chat.type).split('.')[-1].lower(),
                        "unread_count": dialog.unread_count or 0
                    })
            _dialogs_cache = dialogs
            _dialogs_loaded = True
            return {"dialogs": dialogs}
    except Exception as e:
        print(f"Error in get_dialogs: {e}")
        raise HTTPException(500, detail=str(e))

@router.get("/chat/{chat_id}/files")
async def get_chat_files(chat_id: int, limit: int = 200):
    """Dapatkan daftar file dari chat tertentu"""
    if not SESSION_STRING:
        raise HTTPException(500, "TELEGRAM_SESSION_STRING not configured")
    
    files = []
    
    try:
        async with await get_client() as client:
            async for msg in client.get_chat_history(chat_id, limit=limit):
                file_item = None
                
                # Dokumen
                if msg.document:
                    doc = msg.document
                    file_item = {
                        "id": msg.id,
                        "name": doc.file_name or f"document_{msg.id}",
                        "size": doc.file_size,
                        "file_id": doc.file_id,
                        "media_type": get_media_type(doc.file_name or "", doc.mime_type or ""),
                        "mime_type": doc.mime_type or get_mime_type(doc.file_name or ""),
                        "caption": msg.caption or "",
                        "date": msg.date.timestamp()
                    }
                    _file_cache[doc.file_id] = (chat_id, msg.id, doc.file_size, file_item["mime_type"], file_item["media_type"])
                
                # Video
                elif msg.video:
                    vid = msg.video
                    file_item = {
                        "id": msg.id,
                        "name": vid.file_name or f"video_{msg.id}.mp4",
                        "size": vid.file_size,
                        "file_id": vid.file_id,
                        "media_type": "video",
                        "mime_type": "video/mp4",
                        "caption": msg.caption or "",
                        "duration": vid.duration,
                        "width": vid.width,
                        "height": vid.height,
                        "date": msg.date.timestamp()
                    }
                    _file_cache[vid.file_id] = (chat_id, msg.id, vid.file_size, "video/mp4", "video")
                
                # Audio
                elif msg.audio:
                    aud = msg.audio
                    file_item = {
                        "id": msg.id,
                        "name": aud.file_name or f"audio_{msg.id}.mp3",
                        "size": aud.file_size,
                        "file_id": aud.file_id,
                        "media_type": "audio",
                        "mime_type": "audio/mpeg",
                        "caption": msg.caption or "",
                        "duration": aud.duration,
                        "date": msg.date.timestamp()
                    }
                    _file_cache[aud.file_id] = (chat_id, msg.id, aud.file_size, "audio/mpeg", "audio")
                
                # Foto
                elif msg.photo:
                    photo = msg.photo[-1]
                    file_item = {
                        "id": msg.id,
                        "name": f"photo_{msg.id}.jpg",
                        "size": photo.file_size,
                        "file_id": photo.file_id,
                        "media_type": "image",
                        "mime_type": "image/jpeg",
                        "caption": msg.caption or "",
                        "width": photo.width,
                        "height": photo.height,
                        "date": msg.date.timestamp()
                    }
                    _file_cache[photo.file_id] = (chat_id, msg.id, photo.file_size, "image/jpeg", "image")
                
                # Voice Note
                elif msg.voice:
                    voice = msg.voice
                    file_item = {
                        "id": msg.id,
                        "name": f"voice_{msg.id}.ogg",
                        "size": voice.file_size,
                        "file_id": voice.file_id,
                        "media_type": "audio",
                        "mime_type": "audio/ogg",
                        "duration": voice.duration,
                        "date": msg.date.timestamp()
                    }
                    _file_cache[voice.file_id] = (chat_id, msg.id, voice.file_size, "audio/ogg", "audio")
                
                # Video Note
                elif msg.video_note:
                    vn = msg.video_note
                    file_item = {
                        "id": msg.id,
                        "name": f"videonote_{msg.id}.mp4",
                        "size": vn.file_size,
                        "file_id": vn.file_id,
                        "media_type": "video",
                        "mime_type": "video/mp4",
                        "duration": vn.duration,
                        "date": msg.date.timestamp()
                    }
                    _file_cache[vn.file_id] = (chat_id, msg.id, vn.file_size, "video/mp4", "video")
                
                # Sticker
                elif msg.sticker:
                    sticker = msg.sticker
                    file_item = {
                        "id": msg.id,
                        "name": f"sticker_{msg.id}.webp",
                        "size": sticker.file_size,
                        "file_id": sticker.file_id,
                        "media_type": "image",
                        "mime_type": "image/webp",
                        "date": msg.date.timestamp()
                    }
                    _file_cache[sticker.file_id] = (chat_id, msg.id, sticker.file_size, "image/webp", "image")
                
                # Animation (GIF)
                elif msg.animation:
                    anim = msg.animation
                    file_item = {
                        "id": msg.id,
                        "name": anim.file_name or f"gif_{msg.id}.mp4",
                        "size": anim.file_size,
                        "file_id": anim.file_id,
                        "media_type": "video",
                        "mime_type": "video/mp4",
                        "duration": anim.duration,
                        "width": anim.width,
                        "height": anim.height,
                        "date": msg.date.timestamp()
                    }
                    _file_cache[anim.file_id] = (chat_id, msg.id, anim.file_size, "video/mp4", "video")
                
                if file_item:
                    files.append(file_item)
            
            files.reverse()
            return {"files": files, "chat_id": chat_id, "count": len(files)}
            
    except Exception as e:
        print(f"Error in get_chat_files for chat {chat_id}: {e}")
        return {"files": [], "chat_id": chat_id, "count": 0}

@router.get("/stream/{chat_id}/{message_id}")
async def stream_file(request: Request, chat_id: int, message_id: int):
    """Stream file dengan dukungan seeking untuk video/audio"""
    if not SESSION_STRING:
        raise HTTPException(500, "TELEGRAM_SESSION_STRING not configured")
    
    try:
        async with await get_client() as client:
            msg = await client.get_messages(chat_id, message_id)
            if not msg:
                raise HTTPException(404, "Message not found")
            
            file_id = None
            file_size = None
            mime_type = None
            filename = None
            
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
            elif msg.voice:
                file_id = msg.voice.file_id
                file_size = msg.voice.file_size
                mime_type = "audio/ogg"
                filename = f"voice_{message_id}.ogg"
            elif msg.video_note:
                file_id = msg.video_note.file_id
                file_size = msg.video_note.file_size
                mime_type = "video/mp4"
                filename = f"videonote_{message_id}.mp4"
            elif msg.photo:
                photo = msg.photo[-1]
                file_id = photo.file_id
                file_size = photo.file_size
                mime_type = "image/jpeg"
                filename = f"photo_{message_id}.jpg"
            elif msg.sticker:
                sticker = msg.sticker
                file_id = sticker.file_id
                file_size = sticker.file_size
                mime_type = "image/webp"
                filename = f"sticker_{message_id}.webp"
            elif msg.animation:
                anim = msg.animation
                file_id = anim.file_id
                file_size = anim.file_size
                mime_type = "video/mp4"
                filename = anim.file_name or f"gif_{message_id}.mp4"
            else:
                raise HTTPException(400, "This message type cannot be streamed")
            
            range_header = request.headers.get("range")
            
            async def generate():
                async for chunk in client.stream_media(file_id, limit=0):
                    yield chunk
            
            # Untuk gambar
            if mime_type.startswith('image/'):
                return StreamingResponse(
                    generate(), 
                    media_type=mime_type, 
                    headers={"Cache-Control": "public, max-age=86400"}
                )
            
            # Untuk video/audio dengan seeking support
            if range_header and file_size:
                try:
                    range_val = range_header.replace("bytes=", "")
                    parts = range_val.split("-")
                    start = int(parts[0])
                    end = int(parts[1]) if parts[1] else file_size - 1
                    
                    if start >= file_size or end >= file_size:
                        return Response(status_code=416)
                    
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
                            "Cache-Control": "no-cache",
                        }
                    )
                except Exception as e:
                    print(f"Seeking error: {e}")
            
            return StreamingResponse(
                generate(), 
                media_type=mime_type, 
                headers={
                    "Accept-Ranges": "bytes",
                    "Content-Length": str(file_size) if file_size else "",
                    "Cache-Control": "no-cache"
                }
            )
            
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error in stream_file: {e}")
        raise HTTPException(500, detail=str(e))

@router.get("/download/{chat_id}/{message_id}")
async def download_file(chat_id: int, message_id: int):
    """Download file asli dari Telegram"""
    if not SESSION_STRING:
        raise HTTPException(500, "TELEGRAM_SESSION_STRING not configured")
    
    try:
        async with await get_client() as client:
            msg = await client.get_messages(chat_id, message_id)
            if not msg:
                raise HTTPException(404, "Message not found")
            
            file_id = None
            filename = None
            
            if msg.document:
                file_id = msg.document.file_id
                filename = msg.document.file_name or f"file_{message_id}"
            elif msg.video:
                file_id = msg.video.file_id
                filename = msg.video.file_name or f"video_{message_id}.mp4"
            elif msg.audio:
                file_id = msg.audio.file_id
                filename = msg.audio.file_name or f"audio_{message_id}.mp3"
            elif msg.voice:
                file_id = msg.voice.file_id
                filename = f"voice_{message_id}.ogg"
            elif msg.video_note:
                file_id = msg.video_note.file_id
                filename = f"videonote_{message_id}.mp4"
            elif msg.photo:
                file_id = msg.photo[-1].file_id
                filename = f"photo_{message_id}.jpg"
            elif msg.sticker:
                file_id = msg.sticker.file_id
                filename = f"sticker_{message_id}.webp"
            elif msg.animation:
                file_id = msg.animation.file_id
                filename = msg.animation.file_name or f"gif_{message_id}.mp4"
            else:
                raise HTTPException(400, "This message type cannot be downloaded")
            
            async def generate():
                async for chunk in client.stream_media(file_id, limit=0):
                    yield chunk
            
            return StreamingResponse(
                generate(),
                media_type="application/octet-stream",
                headers={"Content-Disposition": f"attachment; filename=\"{filename}\""}
            )
            
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error in download_file: {e}")
        raise HTTPException(500, detail=str(e))

@router.get("/chat/{chat_id}/has-files")
async def chat_has_files(chat_id: int):
    """Cek apakah chat memiliki file tanpa return semua file"""
    if not SESSION_STRING:
        return {"has_files": False, "chat_id": chat_id}
    
    try:
        async with await get_client() as client:
            async for msg in client.get_chat_history(chat_id, limit=10):
                if msg.document or msg.video or msg.audio or msg.photo:
                    return {"has_files": True, "chat_id": chat_id}
            return {"has_files": False, "chat_id": chat_id}
    except Exception as e:
        print(f"Error checking chat {chat_id}: {e}")
        return {"has_files": False, "chat_id": chat_id}