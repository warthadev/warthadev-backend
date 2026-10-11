# modules/telegram.py
import os
import re
import time
import asyncio
import hashlib
from collections import OrderedDict
from typing import Dict, List, Optional, Tuple, Any
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import StreamingResponse, Response
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import (
    Channel, Chat, User,
    DocumentAttributeFilename,
    DocumentAttributeVideo, DocumentAttributeAudio,
    DocumentAttributeImageSize,
    MessageMediaPhoto, MessageMediaDocument,
    MessageMediaWebPage, MessageService,
)
from telethon.errors import PhoneCodeInvalidError, SessionPasswordNeededError, PhoneNumberInvalidError

from .telegram_client import get_telegram_client, disconnect_client
from .firestore_storage import init_firestore, get_files, save_file, get_max_message_id, delete_chat, delete_file

router = APIRouter(prefix="/telegram", tags=["telegram"])

pending_auth: Dict[str, dict] = {}

FILES_CACHE_MAX = 40
SIZE_CACHE_MAX = 80
HAS_FILES_CACHE_MAX = 200
ENTITY_CACHE_MAX = 200
DIALOGS_CACHE_TTL = 600

files_cache: OrderedDict = OrderedDict()
chat_total_size_cache: OrderedDict = OrderedDict()
chat_has_files_cache: OrderedDict = OrderedDict()
entity_cache: OrderedDict = OrderedDict()
dialogs_cache: Optional[List[dict]] = None
dialogs_cache_time: float = 0


def _session_key(session_string: str) -> str:
    if not session_string:
        return "anon"
    return hashlib.sha256(session_string.encode("utf-8")).hexdigest()[:16]


def _evict_lru(cache: OrderedDict, max_size: int) -> None:
    while len(cache) > max_size:
        cache.popitem(last=False)


def _cache_touch(cache: OrderedDict, key: Any) -> None:
    if key in cache:
        cache.move_to_end(key)


def _get_session_from_request_or_query(request: Request) -> str:
    session = request.headers.get("X-Telegram-Session")
    if session:
        return session
    session = request.query_params.get("tg_session")
    if session:
        return session
    raise HTTPException(status_code=401, detail="Telegram session not provided")


def format_size(bytes_size: int) -> str:
    if not bytes_size:
        return "0 B"
    if bytes_size < 1024:
        return f"{bytes_size} B"
    elif bytes_size < 1048576:
        return f"{bytes_size / 1024:.1f} KB"
    elif bytes_size < 1073741824:
        return f"{bytes_size / 1048576:.1f} MB"
    else:
        return f"{bytes_size / 1073741824:.2f} GB"


def _get_photo_size(photo) -> Tuple[int, int, int]:
    if not photo or not photo.sizes:
        return 0, None, None
    best_size = None
    best_pixels = 0
    for size in photo.sizes:
        w = getattr(size, 'w', 0) or 0
        h = getattr(size, 'h', 0) or 0
        pixels = w * h
        if pixels > best_pixels:
            best_pixels = pixels
            best_size = size
    if best_size:
        size_bytes = getattr(best_size, 'size', 0) or 0
        width = getattr(best_size, 'w', None)
        height = getattr(best_size, 'h', None)
        if size_bytes == 0 and width and height:
            estimated = int(width * height * 0.4)
            size_bytes = max(estimated, 1024)
        return size_bytes, width, height
    return 0, None, None


def resolve_media_info(msg) -> Optional[Tuple[Any, int, str, str, str, Optional[int], Optional[int], Optional[int]]]:
    if isinstance(msg, MessageService):
        return None
    if not msg.media:
        return None
    media = msg.media
    if isinstance(media, MessageMediaWebPage):
        return None
    if isinstance(media, MessageMediaPhoto) and media.photo:
        photo = media.photo
        size, width, height = _get_photo_size(photo)
        filename = f"photo_{msg.id}.jpg"
        return (media, size, "image/jpeg", filename, "image", None, width, height)
    if isinstance(media, MessageMediaDocument) and media.document:
        doc = media.document
        filename = None
        duration = None
        width = None
        height = None
        media_type = "document"
        mime = doc.mime_type or "application/octet-stream"
        for attr in doc.attributes:
            if isinstance(attr, DocumentAttributeFilename):
                filename = attr.file_name
            elif isinstance(attr, DocumentAttributeVideo):
                width = attr.w
                height = attr.h
                duration = int(attr.duration) if attr.duration else None
                if getattr(attr, 'nosound', False):
                    media_type = "gif"
                else:
                    media_type = "video"
            elif isinstance(attr, DocumentAttributeAudio):
                duration = int(attr.duration) if attr.duration else None
                if getattr(attr, 'voice', False):
                    media_type = "voice"
                else:
                    media_type = "audio"
            elif isinstance(attr, DocumentAttributeImageSize):
                width = attr.w
                height = attr.h
        if media_type == "document":
            ext = ""
            if filename and '.' in filename:
                ext = filename.rsplit('.', 1)[-1].lower()
            if 'video' in mime or ext in ['mp4', 'mkv', 'avi', 'mov', 'webm', 'm4v', '3gp']:
                media_type = "video"
                mime = mime or "video/mp4"
            elif 'audio' in mime or ext in ['mp3', 'm4a', 'wav', 'ogg', 'flac', 'aac', 'wma']:
                media_type = "audio"
                mime = mime or "audio/mpeg"
            elif 'image' in mime or ext in ['jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp', 'svg']:
                media_type = "image"
                mime = mime or "image/jpeg"
            elif ext in ['zip', 'rar', '7z', 'tar', 'gz', 'bz2', 'xz']:
                media_type = "archive"
            elif ext in ['pdf', 'doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx', 'txt', 'csv']:
                media_type = "document"
        if not filename:
            if media_type == "video":
                filename = f"video_{msg.id}.mp4"
            elif media_type == "audio":
                filename = f"audio_{msg.id}.mp3"
            elif media_type == "voice":
                filename = f"voice_{msg.id}.ogg"
            elif media_type == "gif":
                filename = f"gif_{msg.id}.mp4"
            elif media_type == "image":
                filename = f"image_{msg.id}.jpg"
            elif media_type == "archive":
                filename = f"archive_{msg.id}.zip"
            else:
                filename = f"file_{msg.id}"
        return (media, doc.size, mime, filename, media_type, duration, width, height)
    return None


async def resolve_entity(client: TelegramClient, chat_id: int):
    if chat_id in entity_cache:
        try:
            entity = entity_cache[chat_id]
            _cache_touch(entity_cache, chat_id)
            return entity
        except Exception:
            entity_cache.pop(chat_id, None)
    async for dialog in client.iter_dialogs():
        if dialog.entity.id == chat_id:
            entity_cache[chat_id] = dialog.entity
            _evict_lru(entity_cache, ENTITY_CACHE_MAX)
            return dialog.entity
    try:
        entity = await client.get_entity(chat_id)
        entity_cache[chat_id] = entity
        _evict_lru(entity_cache, ENTITY_CACHE_MAX)
        return entity
    except Exception as e:
        print(f"Could not resolve entity {chat_id}: {e}")
        return None


async def chat_has_any_file(client: TelegramClient, chat_id: int, scan_limit: int = 20) -> bool:
    session_key = _session_key(client.session.save() if client.session else "")
    cache_key = f"{chat_id}_{session_key}"
    if cache_key in chat_has_files_cache:
        _cache_touch(chat_has_files_cache, cache_key)
        return chat_has_files_cache[cache_key]
    try:
        entity = await resolve_entity(client, chat_id)
        if not entity:
            chat_has_files_cache[cache_key] = False
            _evict_lru(chat_has_files_cache, HAS_FILES_CACHE_MAX)
            return False
        media_count = 0
        async for msg in client.iter_messages(entity, limit=scan_limit):
            if isinstance(msg, MessageService):
                continue
            if msg.media and isinstance(msg.media, MessageMediaWebPage):
                continue
            info = resolve_media_info(msg)
            if info:
                media_count += 1
                if media_count >= 1:
                    break
        has_file = media_count >= 1
        chat_has_files_cache[cache_key] = has_file
        _evict_lru(chat_has_files_cache, HAS_FILES_CACHE_MAX)
        return has_file
    except Exception as e:
        print(f"Error checking chat {chat_id}: {e}")
        chat_has_files_cache[cache_key] = False
        _evict_lru(chat_has_files_cache, HAS_FILES_CACHE_MAX)
        return False


async def load_chat_files_full(client: TelegramClient, chat_id: int, limit: int = None) -> List[dict]:
    session_key = _session_key(client.session.save() if client.session else "")
    cache_key = f"{chat_id}_{session_key}"
    if cache_key in files_cache:
        _cache_touch(files_cache, cache_key)
        return files_cache[cache_key]
    files = []
    total_size = 0
    try:
        entity = await resolve_entity(client, chat_id)
        if not entity:
            return []
        chat_name = getattr(entity, 'title', getattr(entity, 'first_name', str(chat_id)))
        print(f"Full scan for {chat_name} (id={chat_id})...")
        async for msg in client.iter_messages(entity, limit=limit):
            if isinstance(msg, MessageService):
                continue
            if msg.media and isinstance(msg.media, MessageMediaWebPage):
                continue
            info = resolve_media_info(msg)
            if not info:
                continue
            _, size, mime_type, filename, media_type, duration, width, height = info
            files.append({
                "id": msg.id,
                "name": filename,
                "size": size,
                "media_type": media_type,
                "mime_type": mime_type,
                "duration": duration,
                "width": width,
                "height": height,
                "caption": msg.message or None,
                "date": int(msg.date.timestamp()) if msg.date else None,
            })
            total_size += size
        files.reverse()
        files_cache[cache_key] = files
        _evict_lru(files_cache, FILES_CACHE_MAX)
        chat_total_size_cache[cache_key] = total_size
        _evict_lru(chat_total_size_cache, SIZE_CACHE_MAX)
        print(f"Full scan complete: {len(files)} files ({format_size(total_size)}) from {chat_name}")
        if cache_key not in chat_has_files_cache:
            chat_has_files_cache[cache_key] = len(files) > 0
            _evict_lru(chat_has_files_cache, HAS_FILES_CACHE_MAX)
    except Exception as e:
        print(f"Error loading files from chat {chat_id}: {e}")
        files = []
        total_size = 0
    return files


async def get_dialogs_list(client: TelegramClient) -> List[dict]:
    global dialogs_cache, dialogs_cache_time
    now = time.time()
    if dialogs_cache is not None and (now - dialogs_cache_time) < DIALOGS_CACHE_TTL:
        return dialogs_cache
    dialogs = []
    skipped_empty = 0
    try:
        print("Fetching dialogs from Telegram...")
        session_key = _session_key(client.session.save() if client.session else "")
        async for dialog in client.iter_dialogs():
            entity = dialog.entity
            if entity is None:
                continue
            entity_id = entity.id
            entity_name = getattr(entity, 'title', None)
            entity_type = "unknown"
            if isinstance(entity, Channel):
                entity_name = entity.title
                if entity.megagroup:
                    entity_type = "supergroup"
                elif entity.broadcast:
                    entity_type = "channel"
                else:
                    entity_type = "channel"
            elif isinstance(entity, Chat):
                entity_name = entity.title
                entity_type = "group"
            elif isinstance(entity, User):
                entity_name = entity.first_name or "Unknown"
                if entity.last_name:
                    entity_name += f" {entity.last_name}"
                entity_type = "user"
            if not entity_name:
                entity_name = f"Chat {entity_id}"
            has_files = await chat_has_any_file(client, entity_id, scan_limit=20)
            if not has_files:
                skipped_empty += 1
                continue
            cache_key_size = f"{entity_id}_{session_key}"
            total_size_bytes = chat_total_size_cache.get(cache_key_size, 0)
            dialogs.append({
                "id": entity_id,
                "name": entity_name,
                "type": entity_type,
                "unread_count": getattr(dialog, 'unread_count', 0) or 0,
                "total_size_bytes": total_size_bytes,
                "total_size_human": format_size(total_size_bytes)
            })
        dialogs_cache = dialogs
        dialogs_cache_time = now
        print(f"Loaded {len(dialogs)} dialogs (skipped {skipped_empty} empty chats)")
    except Exception as e:
        print(f"Error fetching dialogs: {e}")
        if dialogs_cache is not None:
            return dialogs_cache
        raise
    return dialogs


@router.post("/validate_session")
async def validate_session(request: Request):
    body = await request.json()
    session_string = body.get("session_string")
    if not session_string:
        return {"valid": False, "detail": "Session string required"}

    api_id = os.environ.get("TELEGRAM_API_ID")
    api_hash = os.environ.get("TELEGRAM_API_HASH")
    if not api_id or not api_hash:
        return {"valid": False, "detail": "Telegram API not configured"}

    client = None
    try:
        client = TelegramClient(StringSession(session_string), int(api_id), api_hash)
        await client.connect()

        me = await client.get_me()
        if me:
            return {"valid": True, "user": {"id": me.id, "username": me.username, "first_name": me.first_name}}
        else:
            return {"valid": False, "detail": "Session expired or invalid"}
    except Exception as e:
        error_msg = str(e)
        if "AUTH_KEY_INVALID" in error_msg or "SESSION_REVOKED" in error_msg:
            return {"valid": False, "detail": "Session expired or revoked"}
        elif "AUTH_KEY_UNREGISTERED" in error_msg:
            return {"valid": False, "detail": "Session not registered"}
        return {"valid": False, "detail": error_msg}
    finally:
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass


@router.post("/check_phone")
async def check_phone(request: Request):
    body = await request.json()
    phone_number = body.get("phone_number")
    if not phone_number:
        return {"exists": False, "detail": "Phone number required"}

    api_id = os.environ.get("TELEGRAM_API_ID")
    api_hash = os.environ.get("TELEGRAM_API_HASH")
    if not api_id or not api_hash:
        return {"exists": False, "detail": "Telegram API not configured"}

    client = None
    try:
        client = TelegramClient(StringSession(), int(api_id), api_hash)
        await client.connect()

        try:
            result = await client.send_code_request(phone_number)
            if phone_number in pending_auth:
                try:
                    await pending_auth[phone_number]["client"].disconnect()
                except Exception:
                    pass
                del pending_auth[phone_number]
            return {"exists": True, "phone_code_hash": result.phone_code_hash}
        except PhoneNumberInvalidError:
            return {"exists": False, "detail": "Phone number not registered on Telegram"}
        except Exception as e:
            error_msg = str(e)
            if "PHONE_NUMBER_INVALID" in error_msg or "PHONE_NUMBER_UNREGISTERED" in error_msg:
                return {"exists": False, "detail": "Phone number not registered on Telegram"}
            return {"exists": False, "detail": error_msg}
    except Exception as e:
        return {"exists": False, "detail": str(e)}
    finally:
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass


@router.post("/send_code")
async def send_telegram_code(request: Request):
    body = await request.json()
    phone_number = body.get("phone_number")
    if not phone_number:
        raise HTTPException(400, "Phone number required")
    api_id = os.environ.get("TELEGRAM_API_ID")
    api_hash = os.environ.get("TELEGRAM_API_HASH")
    if not api_id or not api_hash:
        raise HTTPException(500, "Telegram API not configured")
    if phone_number in pending_auth:
        try:
            old = pending_auth[phone_number]
            await old["client"].disconnect()
        except Exception:
            pass
        del pending_auth[phone_number]
    client = TelegramClient(StringSession(), int(api_id), api_hash)
    await client.connect()
    try:
        result = await client.send_code_request(phone_number)
        pending_auth[phone_number] = {
            "client": client,
            "phone_code_hash": result.phone_code_hash,
            "timestamp": time.time()
        }
        return {"success": True, "phone_code_hash": result.phone_code_hash}
    except Exception as e:
        await client.disconnect()
        raise HTTPException(400, str(e))


@router.post("/sign_in")
async def telegram_sign_in(request: Request):
    body = await request.json()
    phone_number = body.get("phone_number")
    code = body.get("code")
    phone_code_hash = body.get("phone_code_hash")
    if not phone_number or not code:
        raise HTTPException(400, "Phone number and code required")
    pending = pending_auth.get(phone_number)
    if not pending:
        raise HTTPException(400, "No pending authentication. Please request code first.")
    client = pending["client"]
    hash_to_use = phone_code_hash or pending["phone_code_hash"]
    try:
        await client.sign_in(phone_number, code, phone_code_hash=hash_to_use)
        session_string = client.session.save()
        await client.disconnect()
        pending_auth.pop(phone_number, None)
        return {"success": True, "session_string": session_string}
    except SessionPasswordNeededError:
        await client.disconnect()
        pending_auth.pop(phone_number, None)
        raise HTTPException(400, "2FA password required. Please use manual session.")
    except PhoneCodeInvalidError:
        raise HTTPException(400, "Invalid code")
    except Exception as e:
        await client.disconnect()
        pending_auth.pop(phone_number, None)
        raise HTTPException(400, str(e))


@router.post("/logout")
async def telegram_logout(request: Request):
    session = _get_session_from_request_or_query(request)
    await disconnect_client(session)
    return {"status": "logged out"}


@router.get("/health")
async def health():
    api_id = os.environ.get("TELEGRAM_API_ID")
    api_hash = os.environ.get("TELEGRAM_API_HASH")
    return {
        "status": "healthy",
        "configured": bool(api_id and api_hash),
    }


@router.get("/dialogs")
async def get_dialogs(request: Request):
    session = _get_session_from_request_or_query(request)
    client = await get_telegram_client(session)
    try:
        dialogs = await get_dialogs_list(client)
        return {"dialogs": dialogs, "total": len(dialogs)}
    except Exception as e:
        raise HTTPException(500, f"Failed to fetch dialogs: {str(e)}")


@router.get("/chat/{chat_id}/files")
async def get_chat_files(request: Request, chat_id: int, limit: int = None):
    session = _get_session_from_request_or_query(request)
    client = await get_telegram_client(session)
    try:
        cached_files = get_files(chat_id)
        last_id = get_max_message_id(chat_id)
        entity = await resolve_entity(client, chat_id)
        if not entity:
            raise HTTPException(404, "Chat not found")
        new_files = []
        cached_ids = {f["id"] for f in (cached_files or [])}
        async for msg in client.iter_messages(entity, min_id=last_id, limit=20):
            if isinstance(msg, MessageService):
                continue
            if msg.media and isinstance(msg.media, MessageMediaWebPage):
                continue
            info = resolve_media_info(msg)
            if info:
                if msg.id in cached_ids:
                    continue
                _, size, mime_type, filename, media_type, duration, width, height = info
                file_data = {
                    "id": msg.id,
                    "name": filename,
                    "size": size,
                    "media_type": media_type,
                    "mime_type": mime_type,
                    "duration": duration,
                    "width": width,
                    "height": height,
                    "caption": msg.message or None,
                    "date": int(msg.date.timestamp()) if msg.date else None,
                }
                new_files.append(file_data)
                save_file(chat_id, msg.id, file_data)
        if not cached_files and not new_files:
            full_files = await load_chat_files_full(client, chat_id, limit=limit)
            for f in full_files:
                save_file(chat_id, f["id"], f)
            total_size = sum(f["size"] for f in full_files)
            return {
                "files": full_files,
                "total": len(full_files),
                "chat_id": chat_id,
                "total_size_bytes": total_size,
                "total_size_human": format_size(total_size),
                "source": "full_scan"
            }
        all_files = (cached_files or []) + new_files
        all_files.sort(key=lambda x: x["id"])
        total_size = sum(f["size"] for f in all_files)
        return {
            "files": all_files,
            "total": len(all_files),
            "chat_id": chat_id,
            "total_size_bytes": total_size,
            "total_size_human": format_size(total_size),
            "source": "cached_with_incremental",
            "new_files_found": len(new_files)
        }
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error in get_chat_files: {e}")
        return {
            "files": [], "total": 0, "chat_id": chat_id,
            "error": str(e), "total_size_bytes": 0, "total_size_human": "0 B"
        }


@router.post("/chat/{chat_id}/refresh")
async def refresh_chat_files(request: Request, chat_id: int):
    session = _get_session_from_request_or_query(request)
    client = await get_telegram_client(session)
    delete_chat(chat_id)
    session_key = _session_key(session)
    cache_key = f"{chat_id}_{session_key}"
    files_cache.pop(cache_key, None)
    chat_total_size_cache.pop(cache_key, None)
    chat_has_files_cache.pop(cache_key, None)
    files = await load_chat_files_full(client, chat_id, limit=None)
    for f in files:
        save_file(chat_id, f["id"], f)
    total_size = chat_total_size_cache.get(cache_key, 0)
    return {
        "files": files,
        "total": len(files),
        "chat_id": chat_id,
        "total_size_bytes": total_size,
        "total_size_human": format_size(total_size),
        "source": "refresh"
    }


@router.delete("/chat/{chat_id}/file/{message_id}")
async def delete_file_metadata(chat_id: int, message_id: int):
    delete_file(chat_id, message_id)
    return {"status": "deleted"}


@router.head("/stream/{chat_id}/{message_id}")
async def head_stream_file(request: Request, chat_id: int, message_id: int):
    session = _get_session_from_request_or_query(request)
    client = await get_telegram_client(session)
    entity = await resolve_entity(client, chat_id)
    if not entity:
        raise HTTPException(404, "Chat not found")
    msg = await client.get_messages(entity, ids=message_id)
    if not msg:
        delete_file(chat_id, message_id)
        raise HTTPException(410, "File has been deleted")
    info = resolve_media_info(msg)
    if not info:
        raise HTTPException(404, "No streamable media")
    _, file_size, mime_type, _, _, _, _, _ = info
    return Response(
        status_code=200,
        headers={
            "Content-Type": mime_type,
            "Content-Length": str(file_size) if file_size else "0",
            "Accept-Ranges": "bytes",
        }
    )


@router.get("/stream/{chat_id}/{message_id}")
async def stream_file(request: Request, chat_id: int, message_id: int):
    session = _get_session_from_request_or_query(request)
    client = await get_telegram_client(session)
    entity = await resolve_entity(client, chat_id)
    if not entity:
        raise HTTPException(404, "Chat not found")
    msg = await client.get_messages(entity, ids=message_id)
    if not msg:
        delete_file(chat_id, message_id)
        raise HTTPException(410, "File has been deleted")
    info = resolve_media_info(msg)
    if not info:
        raise HTTPException(404, "No streamable media")
    media_obj, file_size, mime_type, filename, media_type, duration, width, height = info
    range_header = request.headers.get("range")
    if range_header and range_header.startswith("bytes=") and file_size:
        try:
            range_val = range_header.replace("bytes=", "")
            parts = range_val.split("-")
            start_byte = int(parts[0])
            end_byte = int(parts[1]) if parts[1] else file_size - 1
            if start_byte >= file_size or end_byte >= file_size or start_byte > end_byte:
                return Response(
                    status_code=416,
                    headers={"Content-Range": f"bytes */{file_size}"}
                )
            requested_bytes = end_byte - start_byte + 1
            async def range_generator():
                remaining = requested_bytes
                async for chunk in client.iter_download(
                    media_obj,
                    offset=start_byte,
                    limit=requested_bytes,
                    request_size=512 * 1024,
                ):
                    if remaining <= 0:
                        break
                    if len(chunk) > remaining:
                        chunk = chunk[:remaining]
                    yield chunk
                    remaining -= len(chunk)
            return StreamingResponse(
                range_generator(),
                status_code=206,
                media_type=mime_type,
                headers={
                    "Content-Range": f"bytes {start_byte}-{end_byte}/{file_size}",
                    "Accept-Ranges": "bytes",
                    "Content-Length": str(requested_bytes),
                    "Cache-Control": "no-cache",
                }
            )
        except Exception as e:
            print(f"Range request error: {e}")
    async def full_generator():
        async for chunk in client.iter_download(media_obj, request_size=512 * 1024):
            yield chunk
    return StreamingResponse(
        full_generator(),
        status_code=200,
        media_type=mime_type,
        headers={"Accept-Ranges": "bytes", "Cache-Control": "no-cache"}
    )


@router.get("/download/{chat_id}/{message_id}")
async def download_file(request: Request, chat_id: int, message_id: int):
    session = _get_session_from_request_or_query(request)
    client = await get_telegram_client(session)
    entity = await resolve_entity(client, chat_id)
    if not entity:
        raise HTTPException(404, "Chat not found")
    msg = await client.get_messages(entity, ids=message_id)
    if not msg:
        delete_file(chat_id, message_id)
        raise HTTPException(410, "File has been deleted")
    info = resolve_media_info(msg)
    if not info:
        raise HTTPException(404, "No downloadable media")
    media_obj, _, mime_type, filename, _, _, _, _ = info
    async def generate():
        async for chunk in client.iter_download(media_obj, request_size=512 * 1024):
            yield chunk
    return StreamingResponse(
        generate(),
        media_type=mime_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )