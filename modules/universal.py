# modules/universal.py
import os
import re
import time
import uuid
import json
import shutil
import asyncio
import logging
import threading
from typing import Dict, Any, Optional, List
from fastapi import APIRouter, HTTPException, Request, Query

from . import diskcache as dc

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/universal", tags=["universal"])

DOWNLOAD_ROOT = dc.DOWNLOAD_ROOT
SESSION_TTL_SECONDS = int(os.environ.get("UNIVERSAL_SESSION_TTL", str(6 * 60 * 60)))
SESSIONS_FILE = os.path.join(DOWNLOAD_ROOT, "universal_sessions.json")

_sessions: Dict[str, Dict[str, Any]] = {}
_sessions_lock = threading.Lock()

HF_REPO_TYPES = {"dataset"}
HF_REPO_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")

URL_RE = re.compile(r"^https?://", re.IGNORECASE)

SITES_NEED_EXTRACTOR = [
    "youtube.com", "youtu.be", "youtube-nocookie.com",
    "facebook.com", "fb.watch", "instagram.com",
    "twitter.com", "x.com", "tiktok.com",
    "vimeo.com", "dailymotion.com", "twitch.tv",
    "bilibili.com", "nicovideo.jp", "soundcloud.com",
    "reddit.com", "redd.it",
]

STREAM_EXTS = (".m3u8", ".mpd")
DIRECT_EXTS = (
    ".mp4", ".mkv", ".webm", ".avi", ".flv", ".mov", ".m4v", ".3gp",
    ".mp3", ".m4a", ".wav", ".ogg", ".flac", ".aac",
    ".jpg", ".jpeg", ".png", ".gif", ".webp",
    ".zip", ".rar", ".7z", ".pdf",
)


def _human_size(num: int) -> str:
    return dc.human_size(num)


def _disk_stats() -> Dict[str, int]:
    return dc.disk_stats()


def _log_disk_stats(prefix: str = "") -> None:
    dc.log_disk_stats(prefix)


def _active_session_dirs() -> List[str]:
    with _sessions_lock:
        dirs = []
        for sess in _sessions.values():
            d = sess.get("dir", "")
            if d:
                dirs.append(d)
        return dirs


def _cleanup_disk(force: bool = False) -> int:
    return dc.cleanup_disk(force=force, active_dirs=_active_session_dirs())


def _aggressive_cleanup_if_low(reserve_bytes: int) -> int:
    return dc.aggressive_cleanup_if_low(reserve_bytes, active_dirs=_active_session_dirs())


def _ensure_download_root() -> None:
    dc.ensure_download_root()


def _safe_folder(folder: str) -> str:
    parts = [p for p in (folder or "").strip().replace("\\", "/").split("/") if p]
    if any(p in (".", "..") for p in parts):
        raise HTTPException(400, "invalid folder")
    return "/".join(parts)


def _detect_url_type(url: str) -> str:
    url_lower = url.lower()
    path = url_lower.split("?")[0].split("#")[0]
    if any(path.endswith(ext) for ext in STREAM_EXTS):
        return "hls" if path.endswith(".m3u8") else "dash"
    if any(site in url_lower for site in SITES_NEED_EXTRACTOR):
        return "extractor"
    if any(path.endswith(ext) for ext in DIRECT_EXTS):
        return "direct"
    return "direct"


def _session_dir(sid: str) -> str:
    return os.path.join(DOWNLOAD_ROOT, f"universal_{sid}")


def _persist_sessions() -> None:
    try:
        _ensure_download_root()
        with _sessions_lock:
            snapshot = {}
            for sid, sess in _sessions.items():
                snapshot[sid] = {
                    "session_id": sid,
                    "url": sess.get("url"),
                    "url_type": sess.get("url_type"),
                    "audio_only": sess.get("audio_only", False),
                    "format_id": sess.get("format_id"),
                    "quality": sess.get("quality"),
                    "custom_filename": sess.get("custom_filename"),
                    "dir": sess.get("dir"),
                    "status": sess.get("status"),
                    "phase": sess.get("phase"),
                    "percent": sess.get("percent", 0),
                    "current_human": sess.get("current_human", "0 B"),
                    "total_human": sess.get("total_human", "0 B"),
                    "speed_human": sess.get("speed_human", "0 B/s"),
                    "eta_seconds": sess.get("eta_seconds"),
                    "current_file_index": sess.get("current_file_index", 0),
                    "total_files": sess.get("total_files", 0),
                    "completed_files": sess.get("completed_files", 0),
                    "failed_files": sess.get("failed_files", 0),
                    "finished": sess.get("finished", False),
                    "error": sess.get("error"),
                    "created_at": sess.get("created_at"),
                    "started_at": sess.get("started_at"),
                    "expires_at": sess.get("expires_at"),
                    "colab_token": sess.get("colab_token"),
                    "colab_repo_id": sess.get("colab_repo_id"),
                    "colab_repo_type": sess.get("colab_repo_type"),
                    "colab_folder": sess.get("colab_folder"),
                    "save_status": sess.get("save_status"),
                    "save_error": sess.get("save_error"),
                    "save_url": sess.get("save_url"),
                    "total_size_bytes": sess.get("total_size_bytes", 0),
                    "total_speed_bps": sess.get("total_speed_bps", 0),
                    "total_elapsed_seconds": sess.get("total_elapsed_seconds", 0),
                }
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
        logger.info(f"Loaded {len(_sessions)} universal session(s) from disk")
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
            sess = _sessions.pop(sid, None)
            if not sess:
                continue
            try:
                shutil.rmtree(sess.get("dir", ""), ignore_errors=True)
            except Exception:
                pass
    if stale:
        _persist_sessions()


@router.post("/start")
async def universal_start(request: Request):
    body = await request.json()
    url = str(body.get("url") or "").strip()
    audio_only = bool(body.get("audio_only", False))
    format_id = str(body.get("format_id") or "").strip() or None
    quality = str(body.get("quality") or "").strip() or None
    custom_filename = str(body.get("custom_filename") or "").strip() or None
    token = str(body.get("token") or "").strip()
    repo_id = str(body.get("repo_id") or "").strip()
    repo_type = str(body.get("repo_type") or "dataset").strip().lower()
    folder = _safe_folder(str(body.get("folder") or ""))
    files = body.get("files") or []

    if not url or not URL_RE.match(url):
        raise HTTPException(400, "url is required")
    if not token:
        raise HTTPException(400, "token is required")
    if not HF_REPO_ID_RE.match(repo_id):
        raise HTTPException(400, "repo_id not valid")
    if repo_type not in HF_REPO_TYPES:
        raise HTTPException(400, "repo_type not valid")
    if not files:
        raise HTTPException(400, "files is required")

    _cleanup_stale_sessions()

    now = time.time()
    sid = uuid.uuid4().hex[:16]
    url_type = _detect_url_type(url)

    with _sessions_lock:
        _sessions[sid] = {
            "session_id": sid,
            "url": url,
            "url_type": url_type,
            "audio_only": audio_only,
            "format_id": format_id,
            "quality": quality,
            "custom_filename": custom_filename,
            "dir": _session_dir(sid),
            "status": "colab",
            "phase": "download",
            "percent": 0,
            "current_human": "0 B",
            "total_human": "0 B",
            "speed_human": "0 B/s",
            "eta_seconds": None,
            "current_file_index": 0,
            "total_files": 0,
            "completed_files": 0,
            "failed_files": 0,
            "finished": False,
            "error": None,
            "created_at": now,
            "started_at": now,
            "expires_at": now + SESSION_TTL_SECONDS,
            "colab_token": token,
            "colab_repo_id": repo_id,
            "colab_repo_type": repo_type,
            "colab_folder": folder,
            "total_size_bytes": 0,
            "total_speed_bps": 0,
            "total_elapsed_seconds": 0,
        }

    _persist_sessions()

    return {
        "ok": True,
        "session_id": sid,
        "files_count": len(files),
        "url_type": url_type,
        "appended": False,
        "colab_url": body.get("colab_url") or "https://colab.research.google.com/",
    }


@router.get("/colab-config")
async def universal_colab_config(session_id: str = Query(...)):
    with _sessions_lock:
        sess = _sessions.get(session_id)
        if not sess:
            raise HTTPException(410, "session not found or expired")
        url = sess.get("url")
        url_type = sess.get("url_type") or "direct"
        audio_only = bool(sess.get("audio_only", False))
        format_id = sess.get("format_id")
        quality = sess.get("quality")
        custom_filename = sess.get("custom_filename")
        token = sess.get("colab_token")
        repo_id = sess.get("colab_repo_id")
        repo_type = sess.get("colab_repo_type") or "dataset"
        folder = sess.get("colab_folder") or ""

    if not url:
        raise HTTPException(404, "no url in session")

    return {
        "ok": True,
        "session_id": session_id,
        "url": url,
        "url_type": url_type,
        "audio_only": audio_only,
        "format_id": format_id,
        "quality": quality,
        "custom_filename": custom_filename,
        "token": token,
        "repo_id": repo_id,
        "repo_type": repo_type,
        "folder": folder,
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
            "speed_human", "eta_seconds", "file_name", "current_file_index",
            "total_files", "completed_files", "failed_files", "finished",
            "error", "save_status", "save_error", "save_url",
            "total_size_bytes", "total_speed_bps", "total_elapsed_seconds",
        ):
            if key in body:
                sess[key] = body[key]

        sess["expires_at"] = time.time() + SESSION_TTL_SECONDS

    _persist_sessions()

    return {"ok": True}


@router.get("/colab-session")
async def universal_colab_session(session_id: str = Query(...)):
    now = time.time()
    with _sessions_lock:
        sess = _sessions.get(session_id)
        if not sess:
            raise HTTPException(410, "session not found or expired")
        expires_at = sess.get("expires_at") or (sess.get("created_at", now) + SESSION_TTL_SECONDS)
        seconds_left = max(0, int(expires_at - now))
        return {
            "ok": True,
            "session_id": session_id,
            "expires_at": expires_at,
            "seconds_left": seconds_left,
            "ttl_seconds": SESSION_TTL_SECONDS,
            "status": sess.get("status"),
            "files_count": 1,
        }


@router.get("/progress/{sid}")
async def universal_progress(sid: str):
    with _sessions_lock:
        sess = _sessions.get(sid)

    if not sess:
        return {
            "ok": False,
            "session_id": sid,
            "status": "error",
            "error": "session not found",
        }

    now = time.time()
    elapsed = int(now - sess.get("started_at", now))
    expires_at = sess.get("expires_at")
    if expires_at is None:
        expires_at = sess.get("created_at", now) + SESSION_TTL_SECONDS
    seconds_left = max(0, int(expires_at - now))

    return {
        "ok": True,
        "session_id": sid,
        "status": sess.get("status", "downloading"),
        "phase": sess.get("phase"),
        "percent": sess.get("percent", 0),
        "current_human": sess.get("current_human", "0 B"),
        "total_human": sess.get("total_human", "0 B"),
        "speed_human": sess.get("speed_human", "0 B/s"),
        "elapsed": elapsed,
        "eta_seconds": sess.get("eta_seconds"),
        "file_name": sess.get("file_name", ""),
        "current_file_index": sess.get("current_file_index", 0),
        "total_files": sess.get("total_files", 0),
        "completed_files": sess.get("completed_files", 0),
        "failed_files": sess.get("failed_files", 0),
        "finished": bool(sess.get("finished")),
        "error": sess.get("error"),
        "save_status": sess.get("save_status"),
        "save_error": sess.get("save_error"),
        "save_url": sess.get("save_url"),
        "expires_at": expires_at,
        "seconds_left": seconds_left,
        "ttl_seconds": SESSION_TTL_SECONDS,
        "total_size_bytes": sess.get("total_size_bytes", 0),
        "total_speed_bps": sess.get("total_speed_bps", 0),
        "total_elapsed_seconds": sess.get("total_elapsed_seconds", 0),
    }


@router.delete("/session/{sid}")
async def universal_session_delete(sid: str):
    with _sessions_lock:
        sess = _sessions.pop(sid, None)
    if not sess:
        raise HTTPException(404, "session not found")

    shutil.rmtree(sess.get("dir", ""), ignore_errors=True)
    _persist_sessions()
    return {"ok": True, "session_id": sid}


@router.get("/disk")
async def universal_disk():
    stats = _disk_stats()
    return {
        "ok": True,
        "total_bytes": stats["total_bytes"],
        "used_bytes": stats["used_bytes"],
        "free_bytes": stats["free_bytes"],
        "total_human": _human_size(stats["total_bytes"]),
        "used_human": _human_size(stats["used_bytes"]),
        "free_human": _human_size(stats["free_bytes"]),
    }


_load_sessions()
