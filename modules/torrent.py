# modules/torrent.py
import os
import re
import time
import uuid
import shutil
import socket
import asyncio
import logging
import tempfile
import subprocess
import threading
from typing import Dict, Any, Optional, List
from fastapi import APIRouter, HTTPException, Request, Query
from fastapi.responses import FileResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/torrent", tags=["torrent"])

DOWNLOAD_ROOT = os.environ.get("TORRENT_DOWNLOAD_DIR", "/tmp/wartha-torrents")
METADATA_TIMEOUT = int(os.environ.get("TORRENT_METADATA_TIMEOUT", "90"))
SESSION_TTL_SECONDS = int(os.environ.get("TORRENT_SESSION_TTL", str(6 * 60 * 60)))

_sessions: Dict[str, Dict[str, Any]] = {}
_sessions_lock = threading.Lock()

MAGNET_RE = re.compile(r"^magnet:\?xt=urn:btih:[A-Za-z0-9]+", re.IGNORECASE)

MAX_UPLOAD_LIMIT = os.environ.get("TORRENT_MAX_UPLOAD_LIMIT", "20K")
MAX_PEERS = os.environ.get("TORRENT_MAX_PEERS", "30")

HF_REPO_TYPES = {"dataset"}
HF_REPO_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")

_SIZE_UNITS = {
    "B": 1,
    "KIB": 1024, "MIB": 1024 ** 2, "GIB": 1024 ** 3, "TIB": 1024 ** 4,
    "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4,
}


def _human_to_bytes(text: str) -> Optional[int]:
    m = re.match(r"^\s*([\d.]+)\s*([KMGT]?i?B)\s*$", text or "", re.IGNORECASE)
    if not m:
        return None
    try:
        return int(float(m.group(1)) * _SIZE_UNITS.get(m.group(2).upper(), 1))
    except Exception:
        return None


def _ensure_download_root() -> None:
    os.makedirs(DOWNLOAD_ROOT, exist_ok=True)


def _human_size(num: int) -> str:
    try:
        n = float(num)
    except Exception:
        return "0 B"
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(n) < 1024.0:
            if unit == "B":
                return f"{int(n)} B"
            return f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} PB"


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
    tmp = tempfile.mkdtemp(prefix="tmeta_", dir=DOWNLOAD_ROOT)
    torrent_path: Optional[str] = None
    try:
        input_file = _write_magnet_input_file(magnet, tmp)

        cmd1 = [
            "aria2c",
            "--bt-metadata-only=true",
            "--bt-save-metadata=true",
            "--seed-time=0",
            "--enable-dht=true",
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
            if now - sess["created_at"] > SESSION_TTL_SECONDS:
                stale.append(sid)
        for sid in stale:
            sess = _sessions.pop(sid, None)
            if not sess:
                continue
            proc: Optional[subprocess.Popen] = sess.get("proc")
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                except Exception:
                    pass
            try:
                shutil.rmtree(sess.get("dir", ""), ignore_errors=True)
            except Exception:
                pass


def _session_dir(sid: str) -> str:
    return os.path.join(DOWNLOAD_ROOT, f"session_{sid}")


def _start_download_process(magnet: str, file_index: int, sid: str) -> subprocess.Popen:
    work_dir = _session_dir(sid)
    os.makedirs(work_dir, exist_ok=True)

    input_file = _write_magnet_input_file(magnet, work_dir)

    cmd = [
        "aria2c",
        "--enable-dht=true",
        "--seed-time=0",
        "--file-allocation=none",
        "--summary-interval=1",
        "--console-log-level=notice",
        "--show-console-readout=true",
        "--download-result=full",
        "--select-file=" + str(file_index),
        "--max-upload-limit=" + MAX_UPLOAD_LIMIT,
        "--bt-max-peers=" + MAX_PEERS,
        "--input-file=" + input_file,
        "--dir", work_dir,
    ]

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=True,
    )
    return proc


def _parse_progress_line(line: str) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    m = re.search(r"\((\d+)%\)", line)
    if m:
        result["percent"] = int(m.group(1))
    m2 = re.search(r"([\d.]+\s*[KMGTP]?i?B)/([\d.]+\s*[KMGTP]?i?B)", line)
    if m2:
        result["current_human"] = m2.group(1).strip()
        result["total_human"] = m2.group(2).strip()
    m3 = re.search(r"DL:([\d.]+\s*[KMGTP]?i?B)", line)
    if m3:
        result["speed_human"] = m3.group(1).strip()
    m4 = re.search(r"ETA:(\S+)", line)
    if m4:
        result["eta_raw"] = m4.group(1).strip()
    return result


def _eta_raw_to_seconds(raw: str) -> Optional[int]:
    if not raw or raw == "--":
        return None
    if raw == "0s":
        return 0
    total = 0
    pattern = re.compile(r"(\d+)([hms])")
    matches = pattern.findall(raw)
    if not matches:
        return None
    for value, unit in matches:
        v = int(value)
        if unit == "h":
            total += v * 3600
        elif unit == "m":
            total += v * 60
        else:
            total += v
    return total


def _watch_process(sid: str) -> None:
    with _sessions_lock:
        sess = _sessions.get(sid)
    if not sess:
        return

    proc: subprocess.Popen = sess["proc"]
    last_line = ""
    try:
        if proc.stdout:
            for raw_line in proc.stdout:
                line = raw_line.rstrip()
                if not line:
                    continue
                last_line = line
                parsed = _parse_progress_line(line)
                if parsed:
                    with _sessions_lock:
                        s = _sessions.get(sid)
                        if not s:
                            break
                        if "percent" in parsed:
                            s["percent"] = parsed["percent"]
                        if "current_human" in parsed:
                            s["current_human"] = parsed["current_human"]
                        if "total_human" in parsed:
                            s["total_human"] = parsed["total_human"]
                        if "speed_human" in parsed:
                            s["speed_human"] = parsed["speed_human"]
                        if "eta_raw" in parsed:
                            s["eta_seconds"] = _eta_raw_to_seconds(parsed["eta_raw"])
    except Exception as e:
        logger.error(f"torrent watcher error: {e}")
    finally:
        try:
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass

        code = proc.returncode
        with _sessions_lock:
            s = _sessions.get(sid)
            if not s:
                return
            if s.get("aborted"):
                shutil.rmtree(s.get("dir", ""), ignore_errors=True)
                return
            if code == 0:
                s["status"] = "done"
                s["finished"] = True
                s["percent"] = 100
            else:
                s["status"] = "error"
                s["error"] = f"aria2 exited with code {code}. Last: {last_line[:200]}"
                shutil.rmtree(s.get("dir", ""), ignore_errors=True)


async def _get_payload(
    request: Request,
    magnet_query: Optional[str],
    file_index_query: Optional[int],
    file_name_query: Optional[str],
    file_size_query: Optional[int],
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}

    if magnet_query and magnet_query.strip():
        payload["magnet"] = magnet_query.strip()
    if file_index_query is not None:
        payload["file_index"] = file_index_query
    if file_name_query is not None:
        payload["file_name"] = file_name_query
    if file_size_query is not None:
        payload["file_size"] = file_size_query

    if "magnet" in payload and "file_index" in payload:
        return payload

    try:
        body = await request.json()
        if isinstance(body, dict):
            for key in ("magnet", "file_index", "file_name", "file_size"):
                if key in body and key not in payload:
                    payload[key] = body[key]
    except Exception:
        pass

    return payload


@router.post("/start")
async def torrent_start(
    request: Request,
    magnet: Optional[str] = Query(default=None),
):
    payload = await _get_payload(
        request,
        magnet_query=magnet,
        file_index_query=None,
        file_name_query=None,
        file_size_query=None,
    )

    magnet_value = str(payload.get("magnet", "")).strip()
    if not magnet_value:
        raise HTTPException(400, "magnet is required")
    if not MAGNET_RE.match(magnet_value):
        raise HTTPException(400, "magnet link not valid")

    _cleanup_stale_sessions()

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


@router.post("/download")
async def torrent_download(
    request: Request,
    magnet: Optional[str] = Query(default=None),
    file_index: Optional[int] = Query(default=None),
    file_name: Optional[str] = Query(default=None),
    file_size: Optional[int] = Query(default=None),
):
    payload = await _get_payload(
        request,
        magnet_query=magnet,
        file_index_query=file_index,
        file_name_query=file_name,
        file_size_query=file_size,
    )

    magnet_value = str(payload.get("magnet", "")).strip()
    file_index_value = payload.get("file_index")
    file_name_value = str(payload.get("file_name") or "")

    if not magnet_value:
        raise HTTPException(400, "magnet is required")
    if file_index_value is None:
        raise HTTPException(400, "file_index is required")

    try:
        file_index_value = int(file_index_value)
    except Exception:
        raise HTTPException(400, "file_index must be integer")

    _cleanup_stale_sessions()

    sid = uuid.uuid4().hex[:16]

    with _sessions_lock:
        _sessions[sid] = {
            "session_id": sid,
            "magnet": magnet_value,
            "file_index": file_index_value,
            "file_name": file_name_value,
            "dir": _session_dir(sid),
            "status": "starting",
            "percent": 0,
            "current_human": "0 B",
            "total_human": "0 B",
            "speed_human": "0 B/s",
            "eta_seconds": None,
            "finished": False,
            "error": None,
            "created_at": time.time(),
            "started_at": time.time(),
        }

    try:
        proc = _start_download_process(magnet_value, file_index_value, sid)
    except Exception as e:
        with _sessions_lock:
            sess = _sessions.pop(sid, None)
            if sess:
                shutil.rmtree(sess.get("dir", ""), ignore_errors=True)
        raise HTTPException(500, f"Failed to start aria2c: {e}")

    with _sessions_lock:
        _sessions[sid]["proc"] = proc
        _sessions[sid]["status"] = "downloading"

    t = threading.Thread(target=_watch_process, args=(sid,), daemon=True)
    t.start()

    return {
        "ok": True,
        "session_id": sid,
        "file_name": file_name_value,
        "file_size": 0,
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

    return {
        "ok": True,
        "session_id": sid,
        "status": sess.get("status", "downloading"),
        "percent": sess.get("percent", 0),
        "current_human": sess.get("current_human", "0 B"),
        "total_human": sess.get("total_human", "0 B"),
        "speed_human": sess.get("speed_human", "0 B/s"),
        "elapsed": elapsed,
        "eta_seconds": sess.get("eta_seconds"),
        "file_name": sess.get("file_name", ""),
        "finished": bool(sess.get("finished")),
        "error": sess.get("error"),
        "save_status": sess.get("save_status"),
        "save_error": sess.get("save_error"),
        "save_url": sess.get("save_url"),
    }


def _find_completed_file(sess: Dict[str, Any]) -> Optional[str]:
    work_dir = sess.get("dir", "")
    if not work_dir or not os.path.isdir(work_dir):
        return None
    preferred = sess.get("file_name", "")
    candidates: List[str] = []
    for root, _dirs, files in os.walk(work_dir):
        for f in files:
            if f.endswith(".aria2") or f.endswith(".torrent") or f == "input.txt":
                continue
            candidates.append(os.path.join(root, f))
    if not candidates:
        return None
    if preferred:
        for c in candidates:
            if os.path.basename(c) == preferred:
                return c
    candidates.sort(key=lambda p: os.path.getsize(p), reverse=True)
    return candidates[0]


@router.get("/file/{sid}")
async def torrent_file(sid: str):
    with _sessions_lock:
        sess = _sessions.get(sid)
    if not sess:
        raise HTTPException(404, "session not found")

    if sess.get("status") == "error":
        raise HTTPException(500, sess.get("error") or "download failed")

    path = _find_completed_file(sess)
    if not path or not os.path.isfile(path):
        raise HTTPException(404, "file not ready")

    filename = os.path.basename(path)
    media_type = "application/octet-stream"
    lower = filename.lower()
    if lower.endswith(".mp4"):
        media_type = "video/mp4"
    elif lower.endswith(".mkv"):
        media_type = "video/x-matroska"
    elif lower.endswith(".webm"):
        media_type = "video/webm"
    elif lower.endswith(".mp3"):
        media_type = "audio/mpeg"
    elif lower.endswith(".m4a"):
        media_type = "audio/mp4"
    elif lower.endswith(".zip"):
        media_type = "application/zip"

    return FileResponse(
        path,
        media_type=media_type,
        filename=filename,
    )


def _safe_folder(folder: str) -> str:
    parts = [p for p in (folder or "").strip().replace("\\", "/").split("/") if p]
    if any(p in (".", "..") for p in parts):
        raise HTTPException(400, "invalid folder")
    return "/".join(parts)


def _hf_blob_url(repo_type: str, repo_id: str, path_in_repo: str) -> str:
    prefix = {"dataset": "datasets/", "space": "spaces/"}.get(repo_type, "")
    return f"https://huggingface.co/{prefix}{repo_id}/blob/main/{path_in_repo}"


def _upload_to_hf_worker(
    sid: str,
    local_path: str,
    token: str,
    repo_id: str,
    repo_type: str,
    path_in_repo: str,
) -> None:
    error: Optional[str] = None
    try:
        from huggingface_hub import HfApi

        api = HfApi(token=token)
        api.upload_file(
            path_or_fileobj=local_path,
            path_in_repo=path_in_repo,
            repo_id=repo_id,
            repo_type=repo_type,
            commit_message=f"Add {os.path.basename(path_in_repo)}",
        )
    except Exception as e:
        error = str(e).replace(token, "***")[:300] if token else str(e)[:300]
        logger.error(f"torrent hf upload failed: {error}")

    with _sessions_lock:
        s = _sessions.get(sid)
        if not s:
            return
        if error:
            s["save_status"] = "error"
            s["save_error"] = error
            return
        work_dir = s.get("dir", "")

    shutil.rmtree(work_dir, ignore_errors=True)

    with _sessions_lock:
        s = _sessions.get(sid)
        if not s:
            return
        s["save_status"] = "done"
        s["save_url"] = _hf_blob_url(repo_type, repo_id, path_in_repo)


async def _get_save_hf_payload(request: Request) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}
    try:
        body = await request.json()
        if isinstance(body, dict):
            payload.update(body)
    except Exception:
        pass

    for key in ("session_id", "token", "repo_id", "repo_type", "folder"):
        qv = request.query_params.get(key)
        if qv is not None and key not in payload:
            payload[key] = qv

    return payload


@router.post("/save-hf")
async def torrent_save_hf(request: Request):
    p = await _get_save_hf_payload(request)
    sid = str(p.get("session_id") or "").strip()
    token = str(p.get("token") or "").strip()
    repo_id = str(p.get("repo_id") or "").strip()
    repo_type = str(p.get("repo_type") or "dataset").strip().lower()
    folder = _safe_folder(str(p.get("folder") or ""))

    if not sid:
        raise HTTPException(400, "session_id is required")
    if not token:
        raise HTTPException(400, "token is required")
    if not HF_REPO_ID_RE.match(repo_id):
        raise HTTPException(400, "repo_id not valid")
    if repo_type not in HF_REPO_TYPES:
        raise HTTPException(400, "repo_type not valid")

    with _sessions_lock:
        sess = _sessions.get(sid)
        if not sess:
            raise HTTPException(404, "session not found")
        if sess.get("status") != "done" or not sess.get("finished"):
            raise HTTPException(409, "download not finished")
        if sess.get("save_status") == "uploading":
            raise HTTPException(409, "upload already in progress")
        sess_copy = dict(sess)

    local_path = _find_completed_file(sess_copy)
    if not local_path or not os.path.isfile(local_path):
        raise HTTPException(404, "file not ready")

    filename = os.path.basename(local_path)
    path_in_repo = f"{folder}/{filename}" if folder else filename

    with _sessions_lock:
        s = _sessions.get(sid)
        if not s:
            raise HTTPException(404, "session not found")
        s["save_status"] = "uploading"
        s["save_error"] = None
        s["save_url"] = None

    t = threading.Thread(
        target=_upload_to_hf_worker,
        args=(sid, local_path, token, repo_id, repo_type, path_in_repo),
        daemon=True,
    )
    t.start()

    return {
        "ok": True,
        "session_id": sid,
        "file_name": filename,
        "path_in_repo": path_in_repo,
    }


@router.delete("/session/{sid}")
async def torrent_session_delete(sid: str):
    with _sessions_lock:
        sess = _sessions.pop(sid, None)
    if not sess:
        raise HTTPException(404, "session not found")

    proc: Optional[subprocess.Popen] = sess.get("proc")
    if proc and proc.poll() is None:
        try:
            proc.terminate()
        except Exception:
            pass

    shutil.rmtree(sess.get("dir", ""), ignore_errors=True)
    return {"ok": True, "session_id": sid}
