# modules/universal.py
import os
import time
import re
import uuid
import json
import asyncio
import socket
import ipaddress
import threading
import logging
from typing import Dict, Any
from urllib.parse import urlparse, unquote
from cachetools import TTLCache
from fastapi import APIRouter, HTTPException, Request, Query

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/universal", tags=["universal"])

_PROC_STARTED = time.time()

SESSION_TTL_SECONDS = int(os.environ.get("UNIVERSAL_SESSION_TTL", str(6 * 60 * 60)))
SESSIONS_FILE = os.environ.get("UNIVERSAL_SESSIONS_FILE", "/tmp/wartha-universal-sessions.json")

SID_RE = re.compile(r"^[0-9a-f]{16}$")

_sessions: Dict[str, Dict[str, Any]] = {}
_sessions_lock = threading.Lock()

RESOLVE_TIMEOUT = int(os.environ.get("UNIVERSAL_RESOLVE_TIMEOUT", "40"))
RESOLVE_CACHE_TTL = int(os.environ.get("UNIVERSAL_RESOLVE_CACHE_TTL", "600"))
MP3_LABEL = "320k"
BEST_FORMAT = "bestvideo*+bestaudio/bestvideo*/best"
DIRECT_EXTS = (".mp4", ".mkv", ".webm", ".avi", ".ts", ".mp3", ".m4a", ".zip", ".rar", ".7z", ".pdf")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

SKIP_SERVER_HOSTS = tuple(
    h.strip().lower()
    for h in os.environ.get("UNIVERSAL_SERVER_SKIP_HOSTS", "youtube.com,youtu.be,youtube-nocookie.com").split(",")
    if h.strip()
)

_resolve_cache: TTLCache = TTLCache(maxsize=64, ttl=RESOLVE_CACHE_TTL)
_resolve_cache_lock = threading.Lock()
_resolve_lock = threading.Lock()


def _clean_err(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", str(text or "")).strip()


def _is_public_url(url: str) -> bool:
    try:
        p = urlparse(url)
        if p.scheme not in ("http", "https") or not p.hostname:
            return False
        for item in socket.getaddrinfo(p.hostname, None):
            ip = ipaddress.ip_address(item[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                return False
        return True
    except Exception:
        return False


def _skip_on_server(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in SKIP_SERVER_HOSTS)


def _est_size(f: Dict[str, Any], duration: Any) -> int:
    sz = f.get("filesize") or f.get("filesize_approx")
    if sz:
        return int(sz)
    tbr = f.get("tbr")
    if tbr and duration:
        return int(float(tbr) * 1000 / 8 * float(duration))
    return 0


def _pick_thumbnail(info: Dict[str, Any]):
    main = info.get("thumbnail")
    if isinstance(main, str) and main.startswith("https://"):
        return main
    thumbs = [t for t in (info.get("thumbnails") or []) if str(t.get("url") or "").startswith("https://")]
    if not thumbs:
        return None
    thumbs.sort(key=lambda t: (t.get("width") or 0) * (t.get("height") or 0), reverse=True)
    return thumbs[0]["url"]


def _build_formats(info: Dict[str, Any]):
    title = info.get("title") or "download"
    duration = info.get("duration") or 0
    flist = info.get("formats") or []

    def has_v(f): return (f.get("vcodec") or "none") != "none"
    def has_a(f): return (f.get("acodec") or "none") != "none"

    audio_only = [f for f in flist if not has_v(f) and has_a(f)]
    best_audio_size = 0
    if audio_only:
        best_a = max(audio_only, key=lambda x: x.get("abr") or x.get("tbr") or 0)
        best_audio_size = _est_size(best_a, duration)

    by_height: Dict[int, Dict[str, Any]] = {}
    for f in flist:
        h = f.get("height")
        if not h or h < 144 or not has_v(f):
            continue
        cur = by_height.get(h)
        if cur is None or (f.get("tbr") or 0) > (cur.get("tbr") or 0):
            by_height[h] = f

    vname = f"{title}.mp4"
    videos = []
    for h in sorted(by_height, reverse=True):
        f = by_height[h]
        size = _est_size(f, duration)
        if size and not has_a(f):
            size += best_audio_size
        videos.append({
            "name": vname, "label": f"{h}p", "size": size,
            "format_id": f"bestvideo*[height<={h}]+bestaudio/best[height<={h}]",
        })

    out = []
    if videos:
        out.append({"name": vname, "label": "Best", "size": videos[0]["size"], "format_id": BEST_FORMAT})
        out += videos
    if any(has_a(f) for f in flist):
        mp3_size = int(float(duration) * 320 * 1000 / 8) if duration else 0
        out.append({"name": f"{title}.mp3", "label": MP3_LABEL, "size": mp3_size, "format_id": "bestaudio/best"})
    if not out and info.get("url"):
        out.append({"name": vname, "label": "Best", "size": 0, "format_id": BEST_FORMAT})
    for i, e in enumerate(out, start=1):
        e["index"] = i
    return out


def _resolve_sync(url: str) -> Dict[str, Any]:
    path = urlparse(url).path
    if path.lower().endswith(DIRECT_EXTS):
        fname = unquote(os.path.basename(path)) or "download"
        return {
            "ok": True, "source": "server", "title": fname, "thumbnail": None,
            "uploader": None, "duration": None,
            "formats": [{"index": 1, "name": fname, "size": 0, "format_id": "direct"}],
        }

    if _skip_on_server(url):
        return {"ok": False, "reason": "resolved via Colab"}
    if not _is_public_url(url):
        return {"ok": False, "reason": "url not allowed"}
    if not _resolve_lock.acquire(blocking=False):
        return {"ok": False, "reason": "busy"}
    try:
        from yt_dlp import YoutubeDL
        opts = {
            "quiet": True, "no_warnings": True, "noplaylist": True, "skip_download": True,
            "socket_timeout": 20, "retries": 2, "extractor_retries": 1,
            "http_headers": {"User-Agent": UA},
        }
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if not info or info.get("_type") == "playlist":
            return {"ok": False, "reason": "not a single media"}
        formats = _build_formats(info)
        if not formats:
            return {"ok": False, "reason": "no formats"}
        return {
            "ok": True, "source": "server",
            "title": info.get("title") or "download",
            "thumbnail": _pick_thumbnail(info),
            "uploader": info.get("uploader") or info.get("channel"),
            "duration": info.get("duration"),
            "formats": formats,
        }
    except Exception as e:
        return {"ok": False, "reason": _clean_err(e)[:200]}
    finally:
        _resolve_lock.release()


def _persist_sessions() -> None:
    try:
        with _sessions_lock:
            snapshot = {sid: sess for sid, sess in _sessions.items()}
        tmp = SESSIONS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snapshot, f)
        os.replace(tmp, SESSIONS_FILE)
    except Exception as e:
        logger.warning(f"universal persist_sessions failed: {e}")


def _load_sessions() -> None:
    if not os.path.exists(SESSIONS_FILE):
        return
    try:
        with open(SESSIONS_FILE, "r", encoding="utf-8") as f:
            snapshot = json.load(f)
        now = time.time()
        with _sessions_lock:
            for sid, data in snapshot.items():
                exp = data.get("expires_at") or 0
                if exp and exp < now:
                    continue
                _sessions[sid] = data
    except Exception as e:
        logger.warning(f"universal load_sessions failed: {e}")


def _cleanup_stale_sessions() -> None:
    now = time.time()
    with _sessions_lock:
        stale = []
        for sid, sess in _sessions.items():
            exp = sess.get("expires_at") or (sess.get("created_at", now) + SESSION_TTL_SECONDS)
            if now - exp > 0:
                stale.append(sid)
        for sid in stale:
            _sessions.pop(sid, None)
    if stale:
        _persist_sessions()


def _safe_folder(folder: str) -> str:
    parts = [p for p in (folder or "").strip().replace("\\", "/").split("/") if p]
    if any(p in (".", "..") for p in parts):
        raise HTTPException(400, "invalid folder")
    return "/".join(parts)


@router.post("/resolve")
async def universal_resolve(request: Request):
    body = await request.json()
    url = str(body.get("url") or "").strip()
    if not url:
        raise HTTPException(400, "url is required")

    with _resolve_cache_lock:
        cached = _resolve_cache.get(url)
    if cached:
        return cached

    try:
        result = await asyncio.wait_for(asyncio.to_thread(_resolve_sync, url), timeout=RESOLVE_TIMEOUT)
    except asyncio.TimeoutError:
        return {"ok": False, "fallback": True, "reason": "timeout"}

    if result.get("ok"):
        with _resolve_cache_lock:
            _resolve_cache[url] = result
        return result
    result["fallback"] = True
    return result


@router.post("/colab-start")
async def universal_colab_start(request: Request):
    body = await request.json()
    url = str(body.get("url") or "").strip()
    token = str(body.get("token") or "").strip()
    repo_id = str(body.get("repo_id") or "").strip()
    repo_type = str(body.get("repo_type") or "dataset").strip().lower()
    folder = _safe_folder(str(body.get("folder") or ""))
    formats = body.get("formats") or []
    if not isinstance(formats, list):
        formats = []
    formats = [f for f in formats if isinstance(f, dict)][:50]
    preselected = bool(body.get("preselected")) and len(formats) > 0

    if not url:
        raise HTTPException(400, "url is required")
    if not token:
        raise HTTPException(400, "token is required")
    if not repo_id or "/" not in repo_id:
        raise HTTPException(400, "repo_id format 'owner/name' required")
    if repo_type not in {"dataset", "model", "space"}:
        raise HTTPException(400, "repo_type not valid")

    existing_sid = str(body.get("session_id") or "").strip().lower()

    _cleanup_stale_sessions()
    now = time.time()
    reused = False

    if existing_sid and SID_RE.match(existing_sid):
        sid = existing_sid
    else:
        sid = uuid.uuid4().hex[:16]

    fresh = {
        "url": url,
        "formats": formats if preselected else [],
        "preselected": preselected,
        "meta": None,
        "colab_token": token,
        "colab_repo_id": repo_id,
        "colab_repo_type": repo_type,
        "colab_folder": folder,
        "status": "waiting",
        "phase": "resolve",
        "percent": 0,
        "current_human": "0 B",
        "total_human": "0 B",
        "speed_human": "0 B/s",
        "eta_seconds": None,
        "file_name": "",
        "finished": False,
        "error": None,
        "save_status": None,
        "save_error": None,
        "save_url": None,
        "expires_at": now + SESSION_TTL_SECONDS,
    }

    with _sessions_lock:
        sess = _sessions.get(sid)
        if sess:
            reused = True
            fresh["status"] = "newurl"
            sess.update(fresh)
        else:
            _sessions[sid] = {"session_id": sid, "created_at": now, "started_at": now, **fresh}

    _persist_sessions()
    logger.info(f"universal colab-start sid={sid} reused={reused} pid={os.getpid()} sessions={len(_sessions)}")
    return {"ok": True, "session_id": sid, "reused": reused}


@router.get("/colab-config")
async def universal_colab_config(session_id: str = Query(...)):
    with _sessions_lock:
        sess = _sessions.get(session_id)
        if not sess:
            raise HTTPException(410, "session not found or expired")
        return {
            "ok": True,
            "session_id": session_id,
            "url": sess.get("url"),
            "formats": sess.get("formats"),
            "preselected": bool(sess.get("preselected")),
            "token": sess.get("colab_token"),
            "repo_id": sess.get("colab_repo_id"),
            "repo_type": sess.get("colab_repo_type") or "dataset",
            "folder": sess.get("colab_folder") or "",
        }


@router.get("/colab-check")
async def universal_colab_check(session_id: str = Query(...)):
    with _sessions_lock:
        sess = _sessions.get(session_id)
        if not sess:
            raise HTTPException(410, "session not found or expired")
        sess["expires_at"] = time.time() + SESSION_TTL_SECONDS
        return {
            "ok": True,
            "status": sess.get("status"),
        }


@router.post("/colab-update")
async def universal_colab_update(request: Request, session_id: str = Query(...)):
    body = await request.json()
    with _sessions_lock:
        sess = _sessions.get(session_id)
        if not sess:
            raise HTTPException(410, "session not found or expired")
        for key in (
            "status", "phase", "percent", "current_human", "total_human",
            "speed_human", "eta_seconds", "file_name", "finished",
            "error", "save_status", "save_error", "save_url", "formats",
            "preselected", "meta"
        ):
            if key in body:
                sess[key] = body[key]
        if body.get("status") == "download" and "preselected" not in body:
            sess["preselected"] = False
        sess["expires_at"] = time.time() + SESSION_TTL_SECONDS
    _persist_sessions()
    return {"ok": True}


@router.get("/colab-session")
async def universal_colab_session(session_id: str = Query(...)):
    now = time.time()
    with _sessions_lock:
        sess = _sessions.get(session_id)
        if not sess:
            logger.warning(f"universal colab-session MISS sid={session_id} pid={os.getpid()} known={len(_sessions)}")
            raise HTTPException(410, "session not found or expired")
        expires_at = sess.get("expires_at") or (sess.get("created_at", now) + SESSION_TTL_SECONDS)
        return {
            "ok": True,
            "session_id": session_id,
            "expires_at": expires_at,
            "seconds_left": max(0, int(expires_at - now)),
            "ttl_seconds": SESSION_TTL_SECONDS,
            "status": sess.get("status"),
            "phase": sess.get("phase"),
            "percent": sess.get("percent", 0),
            "current_human": sess.get("current_human", "0 B"),
            "total_human": sess.get("total_human", "0 B"),
            "speed_human": sess.get("speed_human", "0 B/s"),
            "eta_seconds": sess.get("eta_seconds"),
            "file_name": sess.get("file_name"),
            "finished": bool(sess.get("finished")),
            "error": sess.get("error"),
            "save_status": sess.get("save_status"),
            "save_error": sess.get("save_error"),
            "save_url": sess.get("save_url"),
            "formats": sess.get("formats"),
            "meta": sess.get("meta"),
        }


@router.get("/health")
async def universal_health():
    with _sessions_lock:
        count = len(_sessions)
    return {
        "status": "healthy",
        "sessions": count,
        "ttl_seconds": SESSION_TTL_SECONDS,
        "pid": os.getpid(),
        "uptime_seconds": int(time.time() - _PROC_STARTED),
    }

_load_sessions()