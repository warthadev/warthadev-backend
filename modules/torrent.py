# modules/torrent.py
import os
import re
import time
import uuid
import json
import shutil
import asyncio
import logging
import tempfile
import subprocess
import threading
from typing import Dict, Any, Optional, List
from fastapi import APIRouter, HTTPException, Request, Query

from . import diskcache as dc

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/torrent", tags=["torrent"])

DOWNLOAD_ROOT = dc.DOWNLOAD_ROOT
METADATA_TIMEOUT = int(os.environ.get("TORRENT_METADATA_TIMEOUT", "90"))
SESSION_TTL_SECONDS = int(os.environ.get("TORRENT_SESSION_TTL", str(6 * 60 * 60)))
SESSIONS_FILE = os.path.join(DOWNLOAD_ROOT, "sessions.json")

_sessions: Dict[str, Dict[str, Any]] = {}
_sessions_lock = threading.Lock()

MAGNET_RE = re.compile(r"^magnet:\?xt=urn:btih:[A-Za-z0-9]+", re.IGNORECASE)

MAX_PEERS = os.environ.get("TORRENT_MAX_PEERS", "200")
DHT_LISTEN_PORT = os.environ.get("TORRENT_DHT_PORT", "6881")

HF_REPO_TYPES = {"dataset"}
HF_REPO_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")


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


def _persist_sessions() -> None:
    try:
        _ensure_download_root()
        with _sessions_lock:
            snapshot = {}
            for sid, sess in _sessions.items():
                snapshot[sid] = {
                    "session_id": sid,
                    "magnet": sess.get("magnet"),
                    "file_name": sess.get("file_name"),
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
                    "colab_files": sess.get("colab_files", []),
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
        logger.warning(f"persist_sessions failed: {e}")


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
        logger.info(f"Loaded {len(_sessions)} session(s) from disk")
    except Exception as e:
        logger.warning(f"load_sessions failed: {e}")


def _write_magnet_input_file(magnet: str, dir_path: str) -> str:
    path = os.path.join(dir_path, "input.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(magnet.strip() + "\n")
    return path


def _find_torrent_file(dir_path: str) -> Optional[str]:
    try:
        for name in os.listdir(dir_path):
            if name.lower().endswith(".torrent"):
                return os.path.join(dir_path, name)
    except Exception:
        return None
    return None


def _bdecode(data: bytes, i: int = 0):
    c = data[i:i + 1]
    if c == b"i":
        j = data.index(b"e", i)
        return int(data[i + 1:j]), j + 1
    if c == b"l":
        i += 1
        out: List[Any] = []
        while data[i:i + 1] != b"e":
            v, i = _bdecode(data, i)
            out.append(v)
        return out, i + 1
    if c == b"d":
        i += 1
        out_d: Dict[str, Any] = {}
        while data[i:i + 1] != b"e":
            k, i = _bdecode(data, i)
            v, i = _bdecode(data, i)
            out_d[k.decode("utf-8", "replace")] = v
        return out_d, i + 1
    j = data.index(b":", i)
    n = int(data[i:j])
    return data[j + 1:j + 1 + n], j + 1 + n


def _parse_torrent_file(torrent_path: str):
    with open(torrent_path, "rb") as fh:
        meta, _ = _bdecode(fh.read())

    info = meta.get("info", {}) if isinstance(meta, dict) else {}

    def dec(b: Any) -> str:
        return b.decode("utf-8", "replace") if isinstance(b, bytes) else str(b)

    root = dec(info.get("name.utf-8") or info.get("name", b"unknown"))
    files: List[Dict[str, Any]] = []

    if "files" in info:
        for item in info["files"]:
            raw_parts = item.get("path.utf-8") or item.get("path", [])
            parts = [dec(p) for p in raw_parts]
            files.append({
                "index": len(files) + 1,
                "path": "/".join([root] + parts),
                "name": parts[-1] if parts else root,
                "size": int(item.get("length", 0)),
            })
    else:
        files.append({
            "index": 1,
            "path": root,
            "name": root,
            "size": int(info.get("length", 0)),
        })

    return root, files


def _run_aria2_metadata(magnet: str, timeout: int) -> Dict[str, Any]:
    _ensure_download_root()
    _cleanup_disk()
    tmp = tempfile.mkdtemp(prefix="tmeta_", dir=DOWNLOAD_ROOT)
    try:
        input_file = _write_magnet_input_file(magnet, tmp)

        cmd1 = [
            "aria2c",
            "--bt-metadata-only=true",
            "--bt-save-metadata=true",
            "--seed-time=0",
            "--enable-dht=true",
            "--dht-listen-port=" + DHT_LISTEN_PORT,
            "--enable-dht6=false",
            "--bt-enable-lpd=true",
            "--bt-max-peers=" + MAX_PEERS,
            "--console-log-level=notice",
            "--summary-interval=0",
            "--file-allocation=none",
            "--input-file=" + input_file,
            "-d", tmp,
        ]

        try:
            r1 = subprocess.run(cmd1, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            shutil.rmtree(tmp, ignore_errors=True)
            return {"ok": False, "detail": f"Metadata timeout ({timeout}s). No peer responded."}

        out1 = (r1.stdout or "") + "\n" + (r1.stderr or "")
        logger.info(f"aria2 metadata phase1 tail: {out1[-1500:]}")

        torrent_path = _find_torrent_file(tmp)
        if not torrent_path:
            shutil.rmtree(tmp, ignore_errors=True)
            return {
                "ok": False,
                "detail": "Metadata fetched but .torrent file not found.",
                "raw_tail": out1[-1200:],
            }

        try:
            torrent_name, files = _parse_torrent_file(torrent_path)
        except Exception as e:
            logger.error(f"failed to parse .torrent: {e}")
            shutil.rmtree(tmp, ignore_errors=True)
            return {"ok": False, "detail": f"Failed to parse .torrent: {e}"}

        if not files:
            shutil.rmtree(tmp, ignore_errors=True)
            return {"ok": False, "detail": "No files found in torrent metadata."}

        info_hash = None
        m = re.search(r"([0-9a-fA-F]{40})\.torrent$", torrent_path)
        if m:
            info_hash = m.group(1).lower()
        if not info_hash:
            m2 = re.search(r"urn:btih:([A-Za-z0-9]+)", magnet)
            if m2:
                info_hash = m2.group(1).lower()

        total_size = sum(f["size"] for f in files)

        return {
            "ok": True,
            "info_hash": info_hash,
            "torrent_name": torrent_name,
            "files": files,
            "total_size": total_size,
            "total_size_human": _human_size(total_size),
        }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


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


def _session_dir(sid: str) -> str:
    return os.path.join(DOWNLOAD_ROOT, f"session_{sid}")


def _safe_folder(folder: str) -> str:
    parts = [p for p in (folder or "").strip().replace("\\", "/").split("/") if p]
    if any(p in (".", "..") for p in parts):
        raise HTTPException(400, "invalid folder")
    return "/".join(parts)


@router.post("/start")
async def torrent_start(
    request: Request,
    magnet: Optional[str] = Query(default=None),
):
    payload: Dict[str, Any] = {}
    if magnet and magnet.strip():
        payload["magnet"] = magnet.strip()

    if "magnet" not in payload:
        try:
            body = await request.json()
            if isinstance(body, dict) and body.get("magnet"):
                payload["magnet"] = str(body["magnet"]).strip()
        except Exception:
            pass

    magnet_value = str(payload.get("magnet", "")).strip()
    if not magnet_value:
        raise HTTPException(400, "magnet is required")
    if not MAGNET_RE.match(magnet_value):
        raise HTTPException(400, "magnet link not valid")

    _cleanup_stale_sessions()
    _log_disk_stats("[start] before cleanup ")

    result = await asyncio.to_thread(_run_aria2_metadata, magnet_value, METADATA_TIMEOUT)
    if not result.get("ok"):
        return {
            "ok": False,
            "detail": result.get("detail") or "Failed to fetch metadata",
            "raw_tail": result.get("raw_tail"),
        }

    return {
        "ok": True,
        "info_hash": result.get("info_hash"),
        "torrent_name": result.get("torrent_name") or result.get("info_hash"),
        "files": result.get("files", []),
        "total_size": result.get("total_size", 0),
        "total_size_human": result.get("total_size_human", "0 B"),
    }


@router.get("/progress/{sid}")
async def torrent_progress(sid: str):
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


@router.post("/colab-start")
async def torrent_colab_start(request: Request):
    body = await request.json()
    magnet = str(body.get("magnet") or "").strip()
    token = str(body.get("token") or "").strip()
    repo_id = str(body.get("repo_id") or "").strip()
    repo_type = str(body.get("repo_type") or "dataset").strip().lower()
    folder = _safe_folder(str(body.get("folder") or ""))
    files = body.get("files") or []
    existing_sid = str(body.get("session_id") or "").strip()

    if not magnet or not MAGNET_RE.match(magnet):
        raise HTTPException(400, "magnet is required")
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

    if existing_sid:
        with _sessions_lock:
            sess = _sessions.get(existing_sid)
            if sess:
                merged = list(sess.get("colab_files") or [])
                seen = {(int(f.get("index", 0)), str(f.get("name", ""))) for f in merged}
                for f in files:
                    key = (int(f.get("index", 0)), str(f.get("name", "")))
                    if key not in seen:
                        merged.append({
                            "index": int(f.get("index", 0)),
                            "name": str(f.get("name", "")),
                            "size": int(f.get("size", 0)),
                        })
                        seen.add(key)
                sess["colab_files"] = merged
                sess["magnet"] = magnet
                sess["colab_token"] = token
                sess["colab_repo_id"] = repo_id
                sess["colab_repo_type"] = repo_type
                sess["colab_folder"] = folder
                sess["expires_at"] = now + SESSION_TTL_SECONDS
                sess["status"] = "colab"
                sess["phase"] = "download"
                sess["percent"] = 0
                sess["current_human"] = "0 B"
                sess["total_human"] = "0 B"
                sess["speed_human"] = "0 B/s"
                sess["eta_seconds"] = None
                sess["current_file_index"] = 0
                sess["total_files"] = 0
                sess["completed_files"] = 0
                sess["failed_files"] = 0
                sess["finished"] = False
                sess["error"] = None
                sess["save_status"] = None
                sess["save_error"] = None
                sess["save_url"] = None
                sess["total_size_bytes"] = 0
                sess["total_speed_bps"] = 0
                sess["total_elapsed_seconds"] = 0
                sid = existing_sid
                total_files = len(merged)
        _persist_sessions()
        return {
            "ok": True,
            "session_id": sid,
            "files_count": total_files,
            "appended": True,
            "colab_url": body.get("colab_url") or "https://colab.research.google.com/",
        }

    sid = uuid.uuid4().hex[:16]

    with _sessions_lock:
        _sessions[sid] = {
            "session_id": sid,
            "magnet": magnet,
            "file_name": "",
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
            "colab_files": [
                {
                    "index": int(f.get("index", 0)),
                    "name": str(f.get("name", "")),
                    "size": int(f.get("size", 0)),
                }
                for f in files
            ],
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
        "appended": False,
        "colab_url": body.get("colab_url") or "https://colab.research.google.com/",
    }


@router.get("/colab-config")
async def torrent_colab_config(session_id: str = Query(...)):
    with _sessions_lock:
        sess = _sessions.get(session_id)
        if not sess:
            raise HTTPException(410, "session not found or expired")
        files = list(sess.get("colab_files") or [])
        magnet = sess.get("magnet")
        token = sess.get("colab_token")
        repo_id = sess.get("colab_repo_id")
        repo_type = sess.get("colab_repo_type") or "dataset"
        folder = sess.get("colab_folder") or ""

    if not files:
        raise HTTPException(404, "no files in session")

    return {
        "ok": True,
        "session_id": session_id,
        "magnet": magnet,
        "files": files,
        "token": token,
        "repo_id": repo_id,
        "repo_type": repo_type,
        "folder": folder,
    }


@router.post("/colab-update")
async def torrent_colab_update(request: Request, session_id: str = Query(...)):
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

        if body.get("save_status") == "done":
            done_name = str(body.get("file_name") or "").strip()
            if done_name:
                remaining = [
                    f for f in (sess.get("colab_files") or [])
                    if str(f.get("name", "")) != done_name
                ]
                sess["colab_files"] = remaining

    _persist_sessions()

    return {"ok": True}


@router.get("/colab-session")
async def torrent_colab_session(session_id: str = Query(...)):
    now = time.time()
    with _sessions_lock:
        sess = _sessions.get(session_id)
        if not sess:
            raise HTTPException(410, "session not found or expired")
        expires_at = sess.get("expires_at") or (sess.get("created_at", now) + SESSION_TTL_SECONDS)
        seconds_left = max(0, int(expires_at - now))
        colab_files = list(sess.get("colab_files") or [])
        file_indexes = sorted({
            int(f.get("index", 0))
            for f in colab_files
            if int(f.get("index", 0)) > 0
        })
        return {
            "ok": True,
            "session_id": session_id,
            "expires_at": expires_at,
            "seconds_left": seconds_left,
            "ttl_seconds": SESSION_TTL_SECONDS,
            "status": sess.get("status"),
            "file_indexes": file_indexes,
            "files_count": len(colab_files),
        }


@router.delete("/session/{sid}")
async def torrent_session_delete(sid: str):
    with _sessions_lock:
        sess = _sessions.pop(sid, None)
    if not sess:
        raise HTTPException(404, "session not found")

    shutil.rmtree(sess.get("dir", ""), ignore_errors=True)
    _persist_sessions()
    return {"ok": True, "session_id": sid}


@router.post("/cleanup")
async def torrent_cleanup(force: bool = Query(default=True)):
    freed = _cleanup_disk(force=force)
    if force:
        freed += _aggressive_cleanup_if_low(0)
    stats = _disk_stats()
    return {
        "ok": True,
        "freed_bytes": freed,
        "freed_human": _human_size(freed),
        "disk": {
            "total": _human_size(stats["total_bytes"]),
            "used": _human_size(stats["used_bytes"]),
            "free": _human_size(stats["free_bytes"]),
            "free_bytes": stats["free_bytes"],
        },
    }


@router.get("/disk")
async def torrent_disk():
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