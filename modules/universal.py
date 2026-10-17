# backend/modules/universal.py
import os
import time
import uuid
import json
import threading
import logging
from typing import Dict, Any
from fastapi import APIRouter, HTTPException, Request, Query

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/universal", tags=["universal"])

SESSION_TTL_SECONDS = int(os.environ.get("UNIVERSAL_SESSION_TTL", str(6 * 60 * 60)))
SESSIONS_FILE = os.environ.get("UNIVERSAL_SESSIONS_FILE", "/tmp/wartha-universal-sessions.json")

_sessions: Dict[str, Dict[str, Any]] = {}
_sessions_lock = threading.Lock()


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


@router.post("/colab-start")
async def universal_colab_start(request: Request):
    body = await request.json()
    url = str(body.get("url") or "").strip()
    token = str(body.get("token") or "").strip()
    repo_id = str(body.get("repo_id") or "").strip()
    repo_type = str(body.get("repo_type") or "dataset").strip().lower()
    folder = _safe_folder(str(body.get("folder") or ""))
    action = str(body.get("action") or "resolve").strip().lower() # 'resolve' | 'download'

    if not url:
        raise HTTPException(400, "url is required")
    if not token:
        raise HTTPException(400, "token is required")
    if not repo_id:
        raise HTTPException(400, "repo_id is required")
    if "/" not in repo_id:
        raise HTTPException(400, "repo_id must be format 'owner/name'")
    if repo_type not in {"dataset", "model", "space"}:
        raise HTTPException(400, "repo_type not valid")

    _cleanup_stale_sessions()
    now = time.time()
    sid = uuid.uuid4().hex[:16]

    with _sessions_lock:
        _sessions[sid] = {
            "session_id": sid,
            "url": url,
            "action": action,
            "formats": body.get("formats") or [],
            "colab_token": token,
            "colab_repo_id": repo_id,
            "colab_repo_type": repo_type,
            "colab_folder": folder,
            "status": "colab",
            "phase": "resolve" if action == "resolve" else "download",
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
            "created_at": now,
            "started_at": now,
            "expires_at": now + SESSION_TTL_SECONDS,
        }

    _persist_sessions()

    return {
        "ok": True,
        "session_id": sid,
    }


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
            "action": sess.get("action"),
            "formats": sess.get("formats"),
            "token": sess.get("colab_token"),
            "repo_id": sess.get("colab_repo_id"),
            "repo_type": sess.get("colab_repo_type") or "dataset",
            "folder": sess.get("colab_folder") or "",
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
            "error", "save_status", "save_error", "save_url", "formats"
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
        }


@router.delete("/session/{sid}")
async def universal_session_delete(sid: str):
    with _sessions_lock:
        sess = _sessions.pop(sid, None)
    if not sess:
        raise HTTPException(404, "session not found")
    _persist_sessions()
    return {"ok": True, "session_id": sid}

@router.get("/health")
async def universal_health():
    with _sessions_lock:
        count = len(_sessions)
    return {
        "status": "healthy",
        "sessions": count,
        "ttl_seconds": SESSION_TTL_SECONDS,
    }

_load_sessions()
