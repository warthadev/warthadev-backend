# modules/kaggle_local_runner.py
# Push & reuse a single per-user GPU Kaggle kernel that runs the local LLM
# (llama-server + agent tool loop) from the user's private "Media AI Local" dataset.
#
# This mirrors modules/kaggle_runner.py (used by the Downloader), but:
#   - enable_gpu=True (the Downloader worker never needed a GPU)
#   - dataset_sources points at the user's own local-model dataset
#   - there is no job queue: one session_id == one long-lived chat connection,
#     so we only ever need to know "is this user's worker already booted/alive".
import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Optional, Dict, Any

logger = logging.getLogger("kaggle_local_runner")

_WORKER_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "workerlocal.py")
_STATE_FILE = os.environ.get("KAGGLE_LOCAL_WORKER_STATE_FILE", "/tmp/wartha-kaggle-local-workers.json")

# Model gguf + llama.cpp binaries disiapkan di dataset privat milik user sendiri.
# Override lewat env var kalau slug dataset-nya beda.
LOCAL_MODEL_DATASET = os.environ.get("KAGGLE_LOCAL_MODEL_DATASET", "")  # "" => wajib dikirim oleh caller
# Paket dokumen/media offline yang sama dengan worker downloader (wheels/ + deb/).
# Dipasang di workerlocal saat boot, terisolasi, supaya numpy/pillow bawaan Kaggle tidak tertimpa.
_PUBLIC_PACKAGE_DATASET = os.environ.get(
    "KAGGLE_MEDIA_OFFLINE_DATASET",
    "herbanuwarthakusuma/media-offline-pkgs",
).strip()

# Boot GPU + load model 9B jauh lebih lama daripada boot worker downloader (CPU-only).
HEARTBEAT_TTL_SECONDS = int(os.environ.get("KAGGLE_LOCAL_WORKER_HEARTBEAT_TTL", "40"))
BOOT_GRACE_SECONDS = int(os.environ.get("KAGGLE_LOCAL_WORKER_BOOT_GRACE", "420"))

_push_locks: Dict[str, threading.Lock] = {}
_push_locks_guard = threading.Lock()

_state_lock = threading.Lock()
# owner_uid -> {"session_id", "last_heartbeat", "last_push", "status", "model_id", "error"}
_worker_states: Dict[str, Dict[str, Any]] = {}


def _load_worker_states() -> None:
    if not os.path.exists(_STATE_FILE):
        return
    try:
        with open(_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            with _state_lock:
                _worker_states.update(data)
    except Exception as exc:
        logger.warning("Failed to load kaggle local worker states: %s", exc)


def _persist_worker_states() -> None:
    try:
        with _state_lock:
            snapshot = json.loads(json.dumps(_worker_states))
        tmp = _STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snapshot, f)
        os.replace(tmp, _STATE_FILE)
    except Exception as exc:
        logger.warning("Failed to persist kaggle local worker states: %s", exc)


def _get_entry_unlocked(owner_uid: str) -> Dict[str, Any]:
    entry = _worker_states.get(owner_uid)
    if not isinstance(entry, dict):
        entry = {
            "session_id": None,
            "last_heartbeat": 0.0,
            "last_push": 0.0,
            "status": "offline",
            "model_id": None,
            "error": None,
        }
        _worker_states[owner_uid] = entry
    return entry


def _is_worker_active_unlocked(entry: Dict[str, Any], now: float) -> bool:
    hb = float(entry.get("last_heartbeat") or 0.0)
    if hb > 0 and (now - hb) <= HEARTBEAT_TTL_SECONDS:
        return True
    pushed = float(entry.get("last_push") or 0.0)
    if pushed > 0 and (now - pushed) <= BOOT_GRACE_SECONDS:
        return True
    return False


def active_session_for(owner_uid: str) -> Optional[str]:
    """Return the still-alive session_id for this user, or None."""
    now = time.time()
    with _state_lock:
        entry = _worker_states.get(owner_uid)
        if entry and _is_worker_active_unlocked(entry, now):
            return str(entry.get("session_id") or "") or None
    return None


def local_kernel_slug(owner_uid: str) -> str:
    return f"wartha-local-{hashlib.sha256(owner_uid.encode('utf-8')).hexdigest()[:10]}"


def worker_snapshot(owner_uid: str) -> Dict[str, Any]:
    """State yang SUDAH dikoreksi terhadap heartbeat.

    Status mentah ("ready"/"booting") hanya benar selama worker masih
    heartbeat. Begitu Kaggle idle/sleep/berhenti, heartbeat putus, jadi:
      - ready   + heartbeat putus          -> offline  (worker berhenti)
      - booting + lewat BOOT_GRACE / mati  -> error    (gagal boot)
    """
    now = time.time()
    with _state_lock:
        entry = _get_entry_unlocked(owner_uid)
        alive = _is_worker_active_unlocked(entry, now)
        raw = entry.get("status") or "offline"
        error = entry.get("error")
        status = raw
        if not alive:
            if raw == "ready":
                status = "offline"
                error = error or "Worker Kaggle berhenti (sesi Kaggle idle/sleep atau dihentikan)."
            elif raw == "booting":
                status = "error"
                error = error or (
                    "Worker Kaggle tidak merespons setelah dijalankan. Kemungkinan kuota GPU Kaggle habis, "
                    "GPU sedang penuh, atau kernel gagal start."
                )
            elif raw not in ("error", "offline"):
                status = "offline"
        return {
            "session_id": entry.get("session_id"),
            "alive": alive,
            "status": status,
            "raw_status": raw,
            "model_id": entry.get("model_id") if status == "ready" else None,
            "error": error,
            "last_push": float(entry.get("last_push") or 0.0),
            "last_heartbeat": float(entry.get("last_heartbeat") or 0.0),
        }


def touch_worker(owner_uid: str, session_id: Optional[str] = None) -> None:
    """Called whenever the Kaggle worker heartbeats / polls."""
    if not owner_uid:
        return
    with _state_lock:
        entry = _get_entry_unlocked(owner_uid)
        entry["last_heartbeat"] = time.time()
        if session_id:
            entry["session_id"] = session_id
    _persist_worker_states()


def set_worker_status(owner_uid: str, *, status: str, model_id: Optional[str] = None, error: Optional[str] = None) -> None:
    if not owner_uid:
        return
    with _state_lock:
        entry = _get_entry_unlocked(owner_uid)
        entry["status"] = status
        if model_id is not None:
            entry["model_id"] = model_id
        entry["error"] = error
        if status in ("offline", "error"):
            entry["last_heartbeat"] = 0.0
            entry["last_push"] = 0.0
    _persist_worker_states()


def _push_lock_for(owner_uid: str) -> threading.Lock:
    with _push_locks_guard:
        lock = _push_locks.get(owner_uid)
        if lock is None:
            lock = _push_locks[owner_uid] = threading.Lock()
        return lock


def kaggle_enabled() -> bool:
    return bool(shutil.which("kaggle") and os.path.isfile(_WORKER_PATH))


def _push_sync(
    session_id: str,
    backend_url: str,
    owner_uid: str,
    worker_token: str,
    credentials: Dict[str, str],
    dataset_slug: str,
) -> Dict[str, Any]:
    username = str(credentials.get("username") or "").strip()
    api_token = str(credentials.get("api_token") or "").strip()
    if not username or not api_token:
        return {"ok": False, "error": "Kaggle credentials are not configured for this account"}
    if not dataset_slug or "/" not in dataset_slug:
        return {"ok": False, "error": "Local model dataset (owner/slug) is not configured"}

    slug = local_kernel_slug(owner_uid)
    uid_slug = slug.rsplit("-", 1)[-1]

    with _push_lock_for(owner_uid):
        now = time.time()
        with _state_lock:
            entry = _get_entry_unlocked(owner_uid)
            if _is_worker_active_unlocked(entry, now) and entry.get("session_id") == session_id:
                _persist_worker_states()
                logger.info("Reusing already-active local worker uid=%s slug=%s sid=%s", uid_slug, slug, session_id)
                return {"ok": True, "slug": slug, "reused_worker": True}

        work = tempfile.mkdtemp(prefix="kaggle_local_push_")
        try:
            with open(_WORKER_PATH, "r", encoding="utf-8") as source:
                worker_code = source.read()
            code = (
                worker_code
                + "\n\nif __name__ == '__main__':\n"
                + f"    run_kaggle_worker(session_id={json.dumps(session_id)}, "
                + f"backend_url={json.dumps(backend_url.rstrip('/'))}, "
                + f"worker_token={json.dumps(worker_token)})\n"
            )
            with open(os.path.join(work, "worker_loader.py"), "w", encoding="utf-8") as target:
                target.write(code)
            sources = [dataset_slug]
            if _PUBLIC_PACKAGE_DATASET and _PUBLIC_PACKAGE_DATASET not in sources:
                sources.append(_PUBLIC_PACKAGE_DATASET)
            meta = {
                "id": f"{username}/{slug}",
                "title": slug,
                "code_file": "worker_loader.py",
                "language": "python",
                "kernel_type": "script",
                "is_private": True,
                "enable_gpu": True,
                "enable_internet": True,
                "dataset_sources": sources,
            }
            with open(os.path.join(work, "kernel-metadata.json"), "w", encoding="utf-8") as target:
                json.dump(meta, target)

            cli_env = os.environ.copy()
            cli_env.pop("KAGGLE_KEY", None)
            cli_env.pop("KAGGLE_USERNAME", None)
            cli_env.pop("KAGGLE_API_TOKEN", None)
            cli_env["KAGGLE_USERNAME"] = username
            cli_env["KAGGLE_API_TOKEN"] = api_token
            cli_env["KAGGLE_CONFIG_DIR"] = work

            result = subprocess.run(
                ["kaggle", "kernels", "push", "-p", work],
                capture_output=True,
                text=True,
                timeout=120,
                env=cli_env,
            )
            output = ((result.stdout or "") + (result.stderr or "")).strip()
            safe_output = output.replace(api_token, "[redacted]")[-300:]
            if result.returncode != 0:
                logger.error("kaggle local push failed sid=%s uid=%s: %s", session_id, uid_slug, safe_output)
                return {"ok": False, "error": safe_output or f"kaggle push failed (exit {result.returncode})"}

            with _state_lock:
                entry = _get_entry_unlocked(owner_uid)
                entry["session_id"] = session_id
                entry["last_push"] = time.time()
                entry["last_heartbeat"] = 0.0
                entry["status"] = "booting"
                entry["model_id"] = None
                entry["error"] = None
            _persist_worker_states()

            logger.info("kaggle local push ok sid=%s uid=%s slug=%s", session_id, uid_slug, slug)
            return {"ok": True, "slug": slug, "reused_worker": False}
        except FileNotFoundError:
            return {"ok": False, "error": "kaggle CLI tidak terpasang di server"}
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "kaggle push timeout"}
        except Exception as exc:
            logger.error("kaggle local push exception sid=%s uid=%s type=%s", session_id, uid_slug, type(exc).__name__)
            return {"ok": False, "error": "Kaggle local worker push failed"}
        finally:
            shutil.rmtree(work, ignore_errors=True)


_kernel_status_cache: Dict[str, Dict[str, Any]] = {}
KERNEL_STATUS_CACHE_SECONDS = 15


def _kernel_status_sync(owner_uid: str, credentials: Dict[str, str]) -> Dict[str, Any]:
    username = str(credentials.get("username") or "").strip()
    api_token = str(credentials.get("api_token") or "").strip()
    if not username or not api_token:
        return {"state": "unknown", "detail": None}
    work = tempfile.mkdtemp(prefix="kaggle_local_status_")
    try:
        cli_env = os.environ.copy()
        for key in ("KAGGLE_KEY", "KAGGLE_USERNAME", "KAGGLE_API_TOKEN"):
            cli_env.pop(key, None)
        cli_env["KAGGLE_USERNAME"] = username
        cli_env["KAGGLE_API_TOKEN"] = api_token
        cli_env["KAGGLE_CONFIG_DIR"] = work
        result = subprocess.run(
            ["kaggle", "kernels", "status", f"{username}/{local_kernel_slug(owner_uid)}"],
            capture_output=True, text=True, timeout=30, env=cli_env,
        )
        output = ((result.stdout or "") + (result.stderr or "")).strip().replace(api_token, "[redacted]")
        match = re.search(r'status\s+"?([A-Za-z_.]+)"?', output)
        raw_state = (match.group(1).split(".")[-1].lower() if match else "")
        if result.returncode != 0 and not raw_state:
            return {"state": "unknown", "detail": output[-200:] or None}
        if raw_state in ("running", "queued", "complete", "error", "cancelacknowledged", "cancelrequested"):
            state = "cancelled" if raw_state.startswith("cancel") else raw_state
        else:
            state = "unknown"
        failure = re.search(r'failure\w*\s*[:=]?\s*"?([^"\n]+)', output, re.IGNORECASE)
        return {"state": state, "detail": (failure.group(1).strip() if failure else None)}
    except Exception as exc:  # status check is best-effort
        logger.warning("kaggle local status check failed type=%s", type(exc).__name__)
        return {"state": "unknown", "detail": None}
    finally:
        shutil.rmtree(work, ignore_errors=True)


async def kernel_status(owner_uid: str, credentials: Dict[str, str]) -> Dict[str, Any]:
    """Status asli kernel di Kaggle (running/queued/complete/error/cancelled), di-cache 15 dtk."""
    now = time.time()
    cached = _kernel_status_cache.get(owner_uid)
    if cached and now - cached["at"] < KERNEL_STATUS_CACHE_SECONDS:
        return cached["value"]
    value = await asyncio.to_thread(_kernel_status_sync, owner_uid, credentials)
    _kernel_status_cache[owner_uid] = {"at": now, "value": value}
    return value


async def start_worker(
    session_id: str,
    backend_url: str,
    owner_uid: str,
    worker_token: str,
    credentials: Dict[str, str],
    dataset_slug: str,
) -> Dict[str, Any]:
    if not kaggle_enabled():
        return {"ok": False, "error": "Kaggle CLI atau workerlocal.py tidak tersedia di backend"}
    return await asyncio.to_thread(_push_sync, session_id, backend_url, owner_uid, worker_token, credentials, dataset_slug)


def resolve_backend_url(request_base_url: Optional[str]) -> str:
    return (os.environ.get("PUBLIC_BACKEND_URL") or request_base_url or "").rstrip("/")


_load_worker_states()
