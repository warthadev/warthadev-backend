# modules/kaggle_runner.py
# Manage a single reusable per-user Kaggle worker with session queueing and heartbeat tracking.
import asyncio
import hashlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Optional, Dict, Any

logger = logging.getLogger("kaggle_runner")

_WORKER_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "workerdl.py")
_PUBLIC_PACKAGE_DATASET = "herbanuwarthakusuma/media-offline-pkgs"
_STATE_FILE = os.environ.get("KAGGLE_WORKER_STATE_FILE", "/tmp/wartha-kaggle-workers.json")

# Heartbeat dianggap hidup jika ada ping dalam 35 detik terakhir;
# boot grace period 90 detik setelah `kaggle kernels push` sebelum ping pertama masuk.
HEARTBEAT_TTL_SECONDS = int(os.environ.get("KAGGLE_WORKER_HEARTBEAT_TTL", "35"))
BOOT_GRACE_SECONDS = int(os.environ.get("KAGGLE_WORKER_BOOT_GRACE", "90"))

_push_locks: Dict[str, threading.Lock] = {}
_push_locks_guard = threading.Lock()

_state_lock = threading.Lock()
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
        logger.warning("Failed to load kaggle worker states: %s", exc)


def _persist_worker_states() -> None:
    try:
        with _state_lock:
            snapshot = json.loads(json.dumps(_worker_states))
        tmp = _STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snapshot, f)
        os.replace(tmp, _STATE_FILE)
    except Exception as exc:
        logger.warning("Failed to persist kaggle worker states: %s", exc)


def _get_entry_unlocked(owner_uid: str) -> Dict[str, Any]:
    entry = _worker_states.get(owner_uid)
    if not isinstance(entry, dict):
        entry = {
            "last_heartbeat": 0.0,
            "last_push": 0.0,
            "active_session_id": None,
            "queue": [],
        }
        _worker_states[owner_uid] = entry
    if not isinstance(entry.get("queue"), list):
        entry["queue"] = []
    return entry


def _is_worker_active_unlocked(entry: Dict[str, Any], now: float) -> bool:
    hb = float(entry.get("last_heartbeat") or 0.0)
    if hb > 0 and (now - hb) <= HEARTBEAT_TTL_SECONDS:
        return True
    pushed = float(entry.get("last_push") or 0.0)
    if pushed > 0 and (now - pushed) <= BOOT_GRACE_SECONDS:
        return True
    return False


def touch_worker(owner_uid: str, session_id: Optional[str] = None) -> None:
    """Update heartbeat timestamp when the running Kaggle worker polls or reports progress."""
    if not owner_uid:
        return
    now = time.time()
    with _state_lock:
        entry = _get_entry_unlocked(owner_uid)
        entry["last_heartbeat"] = now
        if session_id:
            entry["active_session_id"] = session_id
            # Hapus dari queue jika sesi ini sudah mulai diambil oleh worker
            entry["queue"] = [
                item for item in entry["queue"]
                if str(item.get("session_id") or "") != session_id
            ]
    _persist_worker_states()


def pop_next_session(
    owner_uid: str,
    current_session_id: Optional[str] = None,
    closing: bool = False,
) -> Optional[Dict[str, str]]:
    """
    Dipanggil worker saat idle / selesai task untuk mengambil sesi berikutnya.
    Jika `closing=True` dan antrean kosong, worker ditandai offline secara atomik.
    """
    if not owner_uid:
        return None
    now = time.time()
    next_item: Optional[Dict[str, str]] = None
    with _state_lock:
        entry = _get_entry_unlocked(owner_uid)
        queue = entry["queue"]
        while queue:
            candidate = queue.pop(0)
            sid = str(candidate.get("session_id") or "").strip()
            tok = str(candidate.get("worker_token") or "").strip()
            if not sid or not tok:
                continue
            if current_session_id and sid == current_session_id:
                continue
            next_item = {"session_id": sid, "worker_token": tok}
            entry["active_session_id"] = sid
            entry["last_heartbeat"] = now
            break

        if next_item is None:
            if closing:
                entry["last_heartbeat"] = 0.0
                entry["last_push"] = 0.0
                entry["active_session_id"] = None
            else:
                entry["last_heartbeat"] = now
    _persist_worker_states()
    return next_item


def _push_lock_for(owner_uid: str) -> threading.Lock:
    """One lock per account: pushes of different users run in parallel, same user stays serial."""
    with _push_locks_guard:
        lock = _push_locks.get(owner_uid)
        if lock is None:
            lock = _push_locks[owner_uid] = threading.Lock()
        return lock


def kaggle_enabled() -> bool:
    """Report whether the executable worker prerequisites exist, not global credentials."""
    return bool(shutil.which("kaggle") and os.path.isfile(_WORKER_PATH))


def _push_sync(
    session_id: str,
    backend_url: str,
    owner_uid: str,
    worker_token: str,
    credentials: Dict[str, str],
) -> Dict[str, Any]:
    username = str(credentials.get("username") or "").strip()
    api_token = str(credentials.get("api_token") or "").strip()
    if not username or not api_token:
        return {"ok": False, "error": "Kaggle credentials are not configured for this account"}

    # Satu slug tetap per user agar tidak pernah membuat notebook baru berulang kali
    uid_slug = hashlib.sha256(owner_uid.encode("utf-8")).hexdigest()[:10]
    slug = f"wartha-worker-{uid_slug}"

    with _push_lock_for(owner_uid):
        now = time.time()
        with _state_lock:
            entry = _get_entry_unlocked(owner_uid)
            if _is_worker_active_unlocked(entry, now):
                active_sid = str(entry.get("active_session_id") or "")
                if active_sid != session_id:
                    # Masukkan ke antrean worker yang sedang aktif/booting tanpa push ulang
                    entry["queue"] = [
                        item for item in entry["queue"]
                        if str(item.get("session_id") or "") != session_id
                    ]
                    entry["queue"].append({
                        "session_id": session_id,
                        "worker_token": worker_token,
                        "queued_at": now,
                    })
                _persist_worker_states()
                logger.info(
                    "Reusing active Kaggle worker uid=%s slug=%s queued_sid=%s (active_sid=%s)",
                    uid_slug, slug, session_id, active_sid,
                )
                return {"ok": True, "slug": slug, "reused_worker": True}

        work = tempfile.mkdtemp(prefix="kaggle_push_")
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
            meta = {
                "id": f"{username}/{slug}",
                "title": slug,
                "code_file": "worker_loader.py",
                "language": "python",
                "kernel_type": "script",
                "is_private": True,
                "enable_gpu": False,
                "enable_internet": True,
                "dataset_sources": [_PUBLIC_PACKAGE_DATASET],
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
                logger.error("kaggle push failed sid=%s uid=%s: %s", session_id, uid_slug, safe_output)
                return {"ok": False, "error": safe_output or f"kaggle push failed (exit {result.returncode})"}

            with _state_lock:
                entry = _get_entry_unlocked(owner_uid)
                entry["last_push"] = time.time()
                entry["active_session_id"] = session_id
                entry["queue"] = [
                    item for item in entry["queue"]
                    if str(item.get("session_id") or "") != session_id
                ]
            _persist_worker_states()

            logger.info("kaggle push ok sid=%s uid=%s slug=%s", session_id, uid_slug, slug)
            return {"ok": True, "slug": slug, "reused_worker": False}
        except FileNotFoundError:
            return {"ok": False, "error": "kaggle CLI tidak terpasang di server"}
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "kaggle push timeout"}
        except Exception as exc:
            logger.error("kaggle push exception sid=%s uid=%s type=%s", session_id, uid_slug, type(exc).__name__)
            return {"ok": False, "error": "Kaggle worker push failed"}
        finally:
            shutil.rmtree(work, ignore_errors=True)


async def start_worker(
    session_id: str,
    backend_url: str,
    owner_uid: str,
    worker_token: str,
    credentials: Dict[str, str],
) -> Dict[str, Any]:
    if not kaggle_enabled():
        return {"ok": False, "error": "Kaggle CLI atau worker file tidak tersedia di backend"}
    return await asyncio.to_thread(_push_sync, session_id, backend_url, owner_uid, worker_token, credentials)


def resolve_backend_url(request_base_url: Optional[str]) -> str:
    return (os.environ.get("PUBLIC_BACKEND_URL") or request_base_url or "").rstrip("/")


_load_worker_states()
