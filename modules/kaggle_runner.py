# modules/kaggle_runner.py
# Push a private per-session Kaggle worker using the signed-in user's credentials.
import asyncio
import hashlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
from typing import Optional, Dict, Any

logger = logging.getLogger("kaggle_runner")

_WORKER_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "workerdl.py")
_PUBLIC_PACKAGE_DATASET = "herbanuwarthakusuma/media-offline-pkgs"
_push_locks: Dict[str, threading.Lock] = {}
_push_locks_guard = threading.Lock()


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
    uid_slug = hashlib.sha256(owner_uid.encode("utf-8")).hexdigest()[:10]
    slug = f"wartha-worker-{uid_slug}-{session_id[:16]}"
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

        # Never fall back to Render's old shared credentials. The token exists only
        # in this child process environment and is not written to the notebook files.
        cli_env = os.environ.copy()
        cli_env.pop("KAGGLE_KEY", None)
        cli_env.pop("KAGGLE_USERNAME", None)
        cli_env.pop("KAGGLE_API_TOKEN", None)
        cli_env["KAGGLE_USERNAME"] = username
        cli_env["KAGGLE_API_TOKEN"] = api_token
        cli_env["KAGGLE_CONFIG_DIR"] = work
        with _push_lock_for(owner_uid):
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
        logger.info("kaggle push ok sid=%s uid=%s slug=%s", session_id, uid_slug, slug)
        return {"ok": True, "slug": slug}
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
