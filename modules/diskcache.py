# modules/diskcache.py
import os
import time
import shutil
import logging
from typing import Dict, List

logger = logging.getLogger(__name__)

DOWNLOAD_ROOT = os.environ.get("TORRENT_DOWNLOAD_DIR", "/tmp/wartha-torrents")
MIN_FREE_DISK_MB = int(os.environ.get("MIN_FREE_DISK_MB", "100"))

_SAFE_CLEAN_PATHS = [
    "/tmp/wartha-torrents",
    "/tmp/tmeta_",
    "/tmp/ytdl_",
    "/tmp/videomux_",
    "/tmp/ffmpeg_",
    "/root/.cache/aria2",
    "/root/.cache/pip",
    "/root/.cache/huggingface",
    "/root/.cache/yt-dlp",
    "/tmp/pip-",
    "/tmp/tmp",
]


def human_size(num: int) -> str:
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


def ensure_download_root() -> None:
    os.makedirs(DOWNLOAD_ROOT, exist_ok=True)


def dir_size(path: str) -> int:
    if not path or not os.path.exists(path):
        return 0
    if os.path.isfile(path):
        try:
            return os.path.getsize(path)
        except Exception:
            return 0
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except Exception:
                pass
    return total


def disk_stats() -> Dict[str, int]:
    try:
        usage = shutil.disk_usage(DOWNLOAD_ROOT)
        return {
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
        }
    except Exception:
        try:
            usage = shutil.disk_usage("/")
            return {
                "total_bytes": usage.total,
                "used_bytes": usage.used,
                "free_bytes": usage.free,
            }
        except Exception:
            return {"total_bytes": 0, "used_bytes": 0, "free_bytes": 0}


def log_disk_stats(prefix: str = "") -> None:
    stats = disk_stats()
    total = stats["total_bytes"]
    used = stats["used_bytes"]
    free = stats["free_bytes"]
    if total > 0:
        logger.info(
            f"{prefix}disk: {human_size(used)} used / {human_size(total)} total "
            f"({human_size(free)} free)"
        )
    else:
        logger.warning(f"{prefix}disk: unable to read disk stats")


def is_safe_path(path: str) -> bool:
    if not path:
        return False
    abs_path = os.path.abspath(path)
    for safe in _SAFE_CLEAN_PATHS:
        safe_abs = os.path.abspath(safe)
        if abs_path == safe_abs:
            return True
        if abs_path.startswith(safe_abs + os.sep):
            return True
    return False


def remove_path(path: str) -> int:
    if not path or not os.path.exists(path):
        return 0
    if not is_safe_path(path):
        logger.warning(f"diskcache: refused to remove unsafe path {path}")
        return 0
    size = dir_size(path)
    try:
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        else:
            os.remove(path)
        if size > 0:
            logger.info(f"diskcache: removed {path} ({human_size(size)})")
        else:
            logger.info(f"diskcache: removed {path}")
        return size
    except Exception as e:
        logger.warning(f"diskcache: failed to remove {path}: {e}")
        return 0


def cleanup_disk(
    force: bool = False,
    active_dirs: List[str] | None = None,
) -> int:
    ensure_download_root()

    now = time.time()
    freed_bytes = 0

    active_set = set()
    if active_dirs:
        for d in active_dirs:
            if d:
                active_set.add(os.path.abspath(d))

    try:
        entries = os.listdir(DOWNLOAD_ROOT)
    except Exception:
        entries = []

    for name in entries:
        path = os.path.join(DOWNLOAD_ROOT, name)
        if not os.path.isdir(path):
            continue

        abs_path = os.path.abspath(path)

        if name.startswith("tmeta_"):
            try:
                if force or os.path.getmtime(path) < now - 60:
                    freed_bytes += remove_path(path)
            except Exception:
                pass
            continue

        if name.startswith("session_"):
            if abs_path in active_set and not force:
                continue
            try:
                if force or os.path.getmtime(path) < now - 300:
                    freed_bytes += remove_path(path)
            except Exception:
                pass

    for root, _dirs, files in os.walk(DOWNLOAD_ROOT):
        for f in files:
            p = os.path.join(root, f)
            try:
                if f.endswith(".aria2"):
                    if force or os.path.getmtime(p) < now - 300:
                        freed_bytes += remove_path(p)
            except Exception:
                pass

    stale_temp_prefixes = [
        "/tmp/tmeta_",
        "/tmp/ytdl_",
        "/tmp/videomux_",
        "/tmp/ffmpeg_",
    ]
    try:
        tmp_entries = os.listdir("/tmp")
    except Exception:
        tmp_entries = []

    for name in tmp_entries:
        full = os.path.join("/tmp", name)
        for prefix in stale_temp_prefixes:
            if full.startswith(prefix):
                try:
                    if force or os.path.getmtime(full) < now - 300:
                        freed_bytes += remove_path(full)
                except Exception:
                    pass
                break

    for name in tmp_entries:
        full = os.path.join("/tmp", name)
        try:
            if name.startswith("pip-"):
                if force or os.path.getmtime(full) < now - 600:
                    freed_bytes += remove_path(full)
            elif name.startswith("tmp"):
                if force or os.path.getmtime(full) < now - 1800:
                    freed_bytes += remove_path(full)
        except Exception:
            pass

    aria_cache = "/root/.cache/aria2"
    if force and os.path.isdir(aria_cache):
        try:
            for name in os.listdir(aria_cache):
                if name.endswith(".aria2") or name == "dht.dat":
                    p = os.path.join(aria_cache, name)
                    freed_bytes += remove_path(p)
        except Exception:
            pass

    if force:
        for cache_dir in [
            "/root/.cache/pip",
            "/root/.cache/huggingface",
            "/root/.cache/yt-dlp",
        ]:
            if os.path.isdir(cache_dir):
                freed_bytes += remove_path(cache_dir)

    if freed_bytes > 0:
        logger.info(f"diskcache: freed {human_size(freed_bytes)}")

    return freed_bytes


def aggressive_cleanup_if_low(
    reserve_bytes: int,
    active_dirs: List[str] | None = None,
) -> int:
    stats = disk_stats()
    if stats["free_bytes"] >= reserve_bytes:
        return 0

    logger.info("diskcache: disk low, running aggressive cleanup")
    freed = cleanup_disk(force=True, active_dirs=active_dirs)

    try:
        for root, _dirs, files in os.walk("/tmp"):
            for f in files:
                if f.endswith(".log") or f.endswith(".tmp"):
                    p = os.path.join(root, f)
                    try:
                        if os.path.getmtime(p) < time.time() - 600:
                            freed += remove_path(p)
                    except Exception:
                        pass
    except Exception:
        pass

    return freed
