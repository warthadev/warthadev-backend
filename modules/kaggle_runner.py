# modules/kaggle_runner.py
# Menyalakan worker di Kaggle lewat Kaggle API (kernels push), tanpa buka web Kaggle.
# Env yang dibutuhkan di Render:
#   KAGGLE_API_TOKEN   token API Kaggle (format KGAT_...)   [atau KAGGLE_USERNAME + KAGGLE_KEY]
#   KAGGLE_USERNAME    username Kaggle
#   KAGGLE_DATASET     (opsional) dataset paket, default "<username>/media-offline-pkgs"
#   KAGGLE_KERNEL_SLUG (opsional) nama notebook/script, default "wartha-worker"
#   PUBLIC_BACKEND_URL (opsional) URL publik backend, default dari request
import os
import json
import shutil
import tempfile
import subprocess
import threading
import logging
from typing import Optional, Dict, Any

logger = logging.getLogger("kaggle_runner")

_WORKER_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "workerdl.py")
_push_lock = threading.Lock()


def _cfg() -> Dict[str, str]:
    user = os.environ.get("KAGGLE_USERNAME", "").strip()
    return {
        "user": user,
        "slug": os.environ.get("KAGGLE_KERNEL_SLUG", "wartha-worker").strip() or "wartha-worker",
        "dataset": os.environ.get("KAGGLE_DATASET", f"{user}/media-offline-pkgs").strip(),
    }


def kaggle_enabled() -> bool:
    has_auth = bool(os.environ.get("KAGGLE_API_TOKEN") or os.environ.get("KAGGLE_KEY"))
    return has_auth and bool(os.environ.get("KAGGLE_USERNAME")) and os.path.isfile(_WORKER_PATH)


def _push_sync(session_id: str, backend_url: str) -> Dict[str, Any]:
    cfg = _cfg()
    work = tempfile.mkdtemp(prefix="kaggle_push_")
    try:
        with open(_WORKER_PATH, "r", encoding="utf-8") as f:
            worker_code = f.read()
        code = (
            worker_code
            + "\n\nif __name__ == '__main__':\n"
            + f"    run_kaggle_worker(session_id={json.dumps(session_id)}, "
            + f"backend_url={json.dumps(backend_url.rstrip('/'))})\n"
        )
        with open(os.path.join(work, "worker_loader.py"), "w", encoding="utf-8") as f:
            f.write(code)
        meta = {
            "id": f"{cfg['user']}/{cfg['slug']}",
            "title": cfg["slug"],
            "code_file": "worker_loader.py",
            "language": "python",
            "kernel_type": "script",
            "is_private": True,
            "enable_gpu": False,
            "enable_internet": True,
            "dataset_sources": [cfg["dataset"]],
        }
        with open(os.path.join(work, "kernel-metadata.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f)
        with _push_lock:
            r = subprocess.run(["kaggle", "kernels", "push", "-p", work],
                               capture_output=True, text=True, timeout=120, env=os.environ.copy())
        out = ((r.stdout or "") + (r.stderr or "")).strip()
        if r.returncode != 0:
            logger.error(f"kaggle push failed sid={session_id}: {out[-300:]}")
            return {"ok": False, "error": out[-300:] or f"exit {r.returncode}"}
        logger.info(f"kaggle push ok sid={session_id}")
        return {"ok": True}
    except FileNotFoundError:
        return {"ok": False, "error": "kaggle CLI tidak terpasang di server"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "kaggle push timeout"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}
    finally:
        shutil.rmtree(work, ignore_errors=True)


async def start_worker(session_id: str, backend_url: str) -> Dict[str, Any]:
    import asyncio
    if not kaggle_enabled():
        return {"ok": False, "error": "kaggle not configured"}
    return await asyncio.to_thread(_push_sync, session_id, backend_url)


def resolve_backend_url(request_base_url: Optional[str]) -> str:
    return (os.environ.get("PUBLIC_BACKEND_URL") or request_base_url or "").rstrip("/")
