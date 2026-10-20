# universal_worker.py
import os
import re
import sys
import time
import shutil
import subprocess
import tempfile
import threading
import importlib
import importlib.util
from html import escape as _esc
from urllib.parse import urlparse, unquote

os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
os.environ["TQDM_DISABLE"] = "1"

WORKER_VERSION = "2026-10-21-gdown"

def run_colab_worker():
    print(f"[universal] worker version: {WORKER_VERSION}")
    UPGRADE_YTDLP = True
    _PIP_PKGS = {
        "requests": "requests", "yt_dlp": "yt-dlp", "gallery_dl": "gallery-dl",
        "huggingface_hub": "huggingface_hub", "mutagen": "mutagen", "PIL": "pillow",
    }
    _APT_PKGS = {"aria2c": "aria2", "ffmpeg": "ffmpeg", "megadl": "megatools"}

    def _run_quiet(cmd):
        return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def _ensure_deps():
        from IPython.display import display, HTML
        display(HTML("""
        <div style="font-family:monospace; padding: 15px; background: #1e1e1e; border-radius: 8px; border-left: 4px solid #2196F3; color: #e0e0e0; margin-bottom: 10px;">
            <b style="color: #2196F3;">[SETUP]</b> Memeriksa dan menginstal dependencies...
        </div>
        """))

        need_apt = [pkg for b, pkg in _APT_PKGS.items() if not shutil.which(b)]
        if need_apt:
            _run_quiet(["apt-get", "update", "-qq"])
            _run_quiet(["apt-get", "install", "-y", "-qq", *need_apt])

        need_pip = [pkg for mod, pkg in _PIP_PKGS.items() if importlib.util.find_spec(mod) is None]
        if need_pip:
            _run_quiet([sys.executable, "-m", "pip", "install", "-q", *need_pip])

        if UPGRADE_YTDLP:
            _run_quiet([sys.executable, "-m", "pip", "install", "-q", "-U", "yt-dlp", "gallery-dl"])

        importlib.invalidate_caches()

        missing = [b for b in _APT_PKGS if not shutil.which(b)]
        missing += [mod for mod in _PIP_PKGS if importlib.util.find_spec(mod) is None]
        
        if missing:
            err_msg = f"MASIH KURANG: {', '.join(missing)} (cek koneksi runtime)"
            display(HTML(f"""
            <div style="font-family:monospace; padding: 15px; background: #2a1a1a; border-radius: 8px; border-left: 4px solid #F44336; color: #F44336; margin-bottom: 10px;">
                <b>[ERROR]</b> {err_msg}
            </div>
            """))
            raise RuntimeError(err_msg)
        else:
            display(HTML("""
            <div style="font-family:monospace; padding: 15px; background: #1a2e1a; border-radius: 8px; border-left: 4px solid #4CAF50; color: #4CAF50; margin-bottom: 10px;">
                <b>[SETUP]</b> Semua dependency siap.
            </div>
            """))

    _ensure_deps()

    from IPython.display import display, HTML, JSON

    COLAB_SAVE = "/content/Universal"
    TEMP_PATH = "/content/Temp"
    COOKIES_FILE = "/content/cookies.txt"

    SESSION_ID = None
    BACKEND_URL = None

    POST_RETRY_COUNT = 3
    POST_RETRY_DELAY = 2
    WAIT_FAIL_LIMIT = 200
    WAIT_MAX_SECONDS = 3 * 3600
    PROBE_TIMEOUT = 90

    SLOW_MIN_BPS = 200 * 1024
    SLOW_WINDOW = 6
    SLOW_RETRIES = 4
    USE_ARIA2_YT = True
    ARIA2_CONN = 16

    DIRECT_EXTS = (".mp4", ".mkv", ".webm", ".avi", ".ts", ".mp3", ".m4a", ".zip", ".rar", ".7z", ".pdf", ".apk", ".exe", ".iso")
    UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    BEST_FORMAT = "bestvideo*+bestaudio/bestvideo*/best"

    STRATS = [
        {"name": "Normal", "client": None},
        {"name": "Android", "client": ["android", "web_safari"]},
        {"name": "iOS", "client": ["ios", "mweb"]},
        {"name": "TV", "client": ["tv_embedded", "web_embedded"]},
        {"name": "Web", "client": ["web"]},
    ]

    _state = {
        "phase": "idle", "files": [], "current_file": "", "current_index": 0,
        "total_files": 0, "completed_files": 0, "failed_files": 0, "percent": 0,
        "current_human": "0 B", "total_human": "0 B", "speed_human": "0 B/s",
        "eta_seconds": None, "message": "", "error": "",
    }
    _stop = threading.Event()
    _runner = None
    _image_urls = {}

    def _reset_state():
        _state.update({
            "phase": "idle", "files": [], "current_file": "", "current_index": 0,
            "total_files": 0, "completed_files": 0, "failed_files": 0, "percent": 0,
            "current_human": "0 B", "total_human": "0 B", "speed_human": "0 B/s",
            "eta_seconds": None, "message": "", "error": "",
        })

    def _ensure_dirs():
        for p in (COLAB_SAVE, TEMP_PATH): os.makedirs(p, exist_ok=True)

    def _format_size(num):
        try: n = float(num)
        except Exception: return "0 B"
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if abs(n) < 1024.0: return f"{int(n)} B" if unit == "B" else f"{n:.1f} {unit}"
            n /= 1024.0
        return f"{n:.1f} PB"

    def _clean_err(e): return re.sub(r"\x1b\[[0-9;]*m", "", str(e)).strip()
    def _sh(cmd, timeout=None): return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

    class _SlowStream(BaseException): pass

    def _human_to_bytes(txt):
        m = re.match(r"([\d.]+)\s*([KMGTP]?)i?B", str(txt or "").strip(), re.IGNORECASE)
        if not m: return 0
        mult = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4, "P": 1024 ** 5}
        return float(m.group(1)) * mult[m.group(2).upper()]

    def _cookie_opts(): return {"cookiefile": COOKIES_FILE} if os.path.exists(COOKIES_FILE) else {}

    _PHASE_STYLE = {
        "idle": ("#888", "WAITING FOR INPUT"), "fetching": ("#2196F3", "FETCHING CONFIG"),
        "waiting": ("#FF9800", "WAITING FOR USER"), "downloading": ("#4CAF50", "DOWNLOADING"),
        "uploading": ("#FF9800", "UPLOADING TO HUGGING FACE"), "done": ("#00BCD4", "ALL DONE"),
        "error": ("#F44336", "ERROR"),
    }
    _BADGES = {
        "done": ("#4CAF50", "DONE"), "uploading": ("#FF9800", "UPLOAD"),
        "downloading": ("#2196F3", "DOWNLOAD"), "downloaded": ("#00BCD4", "READY"),
        "error": ("#F44336", "FAILED"), "pending": ("#555", "PENDING"),
    }

    def _render_ui():
        s = dict(_state)
        files = list(s["files"])
        phase = s["phase"]
        color, label = _PHASE_STYLE.get(phase, ("#888", phase.upper()))
        total, completed, failed = s["total_files"], s["completed_files"], s["failed_files"]
        overall = int(((completed + failed) / total) * 100) if total > 0 else 0

        eta = s["eta_seconds"]
        eta_str = "--"
        if isinstance(eta, (int, float)) and eta >= 0:
            secs = int(eta)
            eta_str = f"{secs}s" if secs < 60 else (f"{secs / 60:.1f}m" if secs < 3600 else f"{secs / 3600:.1f}h")

        rows = ""
        for idx, f in enumerate(files, start=1):
            badge_bg, badge_text = _BADGES.get(f.get("_status", "pending"), _BADGES["pending"])
            row_color = "#888" if badge_text == "PENDING" else badge_bg
            size_str = _format_size(f.get("size", 0)) if f.get("size") else ""
            rows += f"""
            <div style="display:grid;grid-template-columns:34px 1fr 90px 80px;padding:8px 10px;
                        border-bottom:1px solid #2a2a2a;color:{row_color};font-family:monospace;
                        font-size:12px;align-items:center;gap:8px;">
                <span style="color:#888;font-size:11px;">#{idx}</span>
                <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">{_esc(str(f.get("name", "")))}</span>
                <span style="text-align:right;color:#aaa;font-size:11px;">{size_str}</span>
                <span style="text-align:center;background:{badge_bg};color:#fff;border-radius:4px;
                             font-size:10px;padding:2px 6px;font-weight:bold;">{badge_text}</span>
            </div>"""
        if not rows:
            rows = '<div style="padding:16px;text-align:center;color:#666;font-family:monospace;font-size:12px;">No files loaded yet</div>'

        err_html = ""
        if s["error"]:
            err_html = f"""<div style="margin-top:10px;padding:10px 14px;background:#2a1a1a;border-left:4px solid #F44336;
                        border-radius:6px;color:#F44336;font-family:monospace;font-size:12px;word-break:break-all;">{_esc(str(s["error"]))}</div>"""
        msg_html = ""
        if s["message"]:
            msg_html = f"""<div style="margin-top:10px;padding:10px 14px;background:#1a1a2e;border-left:4px solid {color};
                        border-radius:6px;color:#e0e0e0;font-family:monospace;font-size:12px;word-break:break-all;">{_esc(str(s["message"]))}</div>"""

        prog_html = ""
        if phase in ("downloading", "uploading"):
            prog_html = f"""
            <div style="margin-top:12px;padding:12px 14px;background:#141420;border-radius:8px;border:1px solid #2a2a4a;">
                <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
                    <span style="color:#e0e0e0;font-family:monospace;font-size:12px;overflow:hidden;
                                 text-overflow:ellipsis;white-space:nowrap;max-width:60%;">{_esc(str(s["current_file"]))}</span>
                    <span style="color:{color};font-family:monospace;font-size:11px;font-weight:bold;">
                        [{s["current_index"]}/{total}] {label}</span>
                </div>
                <div style="background:#2a2a2a;border-radius:6px;height:18px;overflow:hidden;">
                    <div style="background:linear-gradient(90deg,{color},{color}cc);height:100%;width:{s["percent"]}%;
                                transition:width .3s linear;border-radius:6px;display:flex;align-items:center;
                                justify-content:center;color:#fff;font-size:10px;font-weight:bold;">{s["percent"]}%</div>
                </div>
                <div style="display:flex;justify-content:space-between;margin-top:6px;color:#888;font-family:monospace;font-size:11px;">
                    <span>{s["current_human"]} / {s["total_human"]}</span>
                    <span>{s["speed_human"]} · ETA {eta_str}</span>
                </div>
            </div>"""

        return f"""
        <div style="font-family:monospace;margin:10px 0;background:linear-gradient(135deg,#0f0f1e,#1a1a2e,#16213e);
                    border-radius:12px;border:1px solid #2a2a4a;padding:16px;color:#e0e0e0;">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;">
                <div style="color:#4CAF50;font-size:16px;font-weight:bold;letter-spacing:1px;">UNIVERSAL COLAB</div>
                <div style="color:{color};font-size:11px;font-weight:bold;background:rgba(255,255,255,0.05);
                            padding:4px 10px;border-radius:20px;border:1px solid {color};">{label}</div>
            </div>
            <div style="display:flex;gap:16px;margin-bottom:12px;font-size:11px;color:#888;">
                <span>Total: <b style="color:#e0e0e0;">{total}</b></span>
                <span>Done: <b style="color:#4CAF50;">{completed}</b></span>
                <span>Failed: <b style="color:#F44336;">{failed}</b></span>
                <span>Overall: <b style="color:#00BCD4;">{overall}%</b></span>
            </div>
            <div style="background:#141420;border-radius:8px;border:1px solid #2a2a4a;max-height:320px;overflow-y:auto;">{rows}</div>
            {prog_html}{msg_html}{err_html}
        </div>"""

    def _set_state(**kw): _state.update(kw)

    def _poll():
        try: return JSON({"html": _render_ui()})
        except Exception as e: return JSON({"html": f"<pre style='color:#F44336'>UI error: {_esc(_clean_err(e))}</pre>"})

    def _post_update(payload, retries=POST_RETRY_COUNT):
        if not SESSION_ID or not BACKEND_URL: return False
        url = f"{BACKEND_URL.rstrip('/')}/universal/colab-update"
        import requests
        for attempt in range(retries):
            try:
                r = requests.post(url, params={"session_id": SESSION_ID}, json=payload, timeout=10)
                if r.ok: return True
            except Exception: pass
            if attempt < retries - 1: time.sleep(POST_RETRY_DELAY * (attempt + 1))
        return False

    def _fetch_config(session_id, backend_url):
        import requests
        url = f"{backend_url.rstrip('/')}/universal/colab-config"
        for attempt in range(POST_RETRY_COUNT):
            try:
                r = requests.get(url, params={"session_id": session_id}, timeout=30)
                if r.ok: return r.json()
            except Exception: pass
            if attempt < POST_RETRY_COUNT - 1: time.sleep(POST_RETRY_DELAY * (attempt + 1))
        return None

    def _check_status(session_id, backend_url):
        import requests
        try:
            r = requests.get(f"{backend_url.rstrip('/')}/universal/colab-check",
                             params={"session_id": session_id}, timeout=15)
            if r.ok: return r.json().get("status")
        except Exception: pass
        return None

    def _safe_name(name, fallback="download"):
        base = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", unquote(str(name or "")))
        base = re.sub(r"\s+", " ", base).strip().strip(".").replace("-", "_")
        return (base or fallback)[:120]

    def _strip_ext(name): return re.sub(r"\.(mp4|mp3)$", "", str(name or ""), flags=re.IGNORECASE)
    def _base_from_item(name): return _safe_name(re.sub(r"\s*\(\w{2,5}\)\s*$", "", name or ""))
    def _display_name(item): return f"{item.get('name', '')} · {item.get('label')}" if item.get("label") else item.get("name", "")
    def _video_base(name, label): return f"{_safe_name(name)[:105].replace('-', '_')} ({label})"

    def _move_final(src, dst):
        os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
        if os.path.exists(dst): os.remove(dst)
        shutil.move(src, dst)

    def _check_file(path, min_size=1):
        if not os.path.exists(path): return False, "File tidak ada"
        size = os.path.getsize(path)
        if size < min_size: return False, f"File terlalu kecil ({_format_size(size)})"
        try:
            with open(path, "rb") as f:
                head = f.read(512).lstrip().lower()
            if head.startswith((b"<!doc", b"<html")): 
                return False, "File terdeteksi sebagai HTML (kemungkinan halaman peringatan Google Drive)"
        except Exception as e: return False, f"Error baca file: {e}"
        return True, "OK"

    def _host(url): return (urlparse(url).hostname or "").lower()
    def _is_youtube(url): return "youtube" in _host(url) or _host(url).endswith("youtu.be")
    def _is_instagram(url): return "instagram.com" in _host(url)

    def _looks_like_file(url):
        try:
            import requests
            r = requests.head(url, headers={"User-Agent": UA}, timeout=15, allow_redirects=True)
            ctype = (r.headers.get("content-type") or "").lower()
            return r.status_code < 400 and "text/html" not in ctype and bool(ctype)
        except Exception: return False

    def _with_timeout(fn, timeout, *args):
        box = {}
        def run():
            try: box["ok"] = fn(*args)
            except Exception as e: box["err"] = _clean_err(e)
        t = threading.Thread(target=run, daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive(): return None, f"timeout > {timeout}s"
        return box.get("ok"), box.get("err")

    def _probe_once(url, st):
        from yt_dlp import YoutubeDL
        opts = {"quiet": True, "no_warnings": True, "socket_timeout": 60, "retries": 5,
                "noplaylist": True, "http_headers": {"User-Agent": UA}, **_cookie_opts()}
        if st["client"]: opts["extractor_args"] = {"youtube": {"player_client": st["client"]}}
        with YoutubeDL(opts) as ydl: info = ydl.extract_info(url, download=False)
        if info and (info.get("formats") or info.get("url")): return info
        raise RuntimeError("tidak ada format")

    def _probe(url):
        strats = STRATS if _is_youtube(url) else STRATS[:1]
        errs = []
        for st in strats:
            _set_state(message=f"Analyzing source URL... (strategi: {st['name']})")
            info, err = _with_timeout(_probe_once, PROBE_TIMEOUT, url, st)
            if info: return info, st, errs
            errs.append(f"{st['name']}: {(err or 'unknown')[:160]}")
            _set_state(message=" | ".join(errs)[-400:])
        return None, None, errs

    def _est_size(f, duration):
        sz = f.get("filesize") or f.get("filesize_approx")
        if sz: return int(sz)
        tbr = f.get("tbr")
        if tbr and duration: return int(float(tbr) * 1000 / 8 * float(duration))
        return 0

    def _build_formats(info):
        title = info.get("title") or "download"
        duration = info.get("duration") or 0
        flist = info.get("formats") or []
        audio_only = [f for f in flist if (f.get("vcodec") or "none") == "none" and (f.get("acodec") or "none") != "none"]
        has_audio = any((f.get("acodec") or "none") != "none" for f in flist)
        best_audio_size = 0
        if audio_only:
            best_a = max(audio_only, key=lambda x: x.get("abr") or x.get("tbr") or 0)
            best_audio_size = _est_size(best_a, duration)

        vname = f"{title}.mp4"
        videos, seen = [], set()
        for f in sorted(flist, key=lambda x: ((x.get("height") or 0), (x.get("tbr") or 0)), reverse=True):
            h = f.get("height")
            fid = f.get("format_id")
            if not fid or not h or h < 144 or h in seen or (f.get("vcodec") or "none") == "none": continue
            seen.add(h)
            has_a = (f.get("acodec") or "none") != "none"
            size = _est_size(f, duration)
            if size and not has_a: size += best_audio_size
            videos.append({
                "name": vname, "label": f"{h}p", "size": size,
                "format_id": f"{fid}/best" if has_a else f"{fid}+bestaudio/best",
            })

        out = []
        if videos:
            out.append({"name": vname, "label": "Best", "size": videos[0]["size"], "format_id": BEST_FORMAT})
            out += videos
        if has_audio:
            mp3_size = int(float(duration) * int(MP3_BITRATE.rstrip("k")) * 1000 / 8) if duration else 0
            out.append({"name": f"{title}.mp3", "label": MP3_BITRATE, "size": mp3_size, "format_id": "bestaudio/best"})
        if not out: out.append({"name": vname, "label": "Best", "size": 0, "format_id": BEST_FORMAT})
        for i, e in enumerate(out, start=1): e["index"] = i
        return out

    def _probe_images(url):
        cmd = [sys.executable, "-m", "gallery_dl", "-g", url]
        if os.path.exists(COOKIES_FILE): cmd += ["--cookies", COOKIES_FILE]
        try: r = _sh(cmd, timeout=120)
        except Exception as e: return [], _clean_err(e)[:200]
        urls = [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip().startswith("http")]
        if not urls:
            err = (r.stderr or "").strip().splitlines()
            return [], (err[-1] if err else "tidak ada gambar")[:200]
        m = re.search(r"/(?:p|reel|tv)/([^/?#]+)", urlparse(url).path)
        code = m.group(1) if m else "image"
        formats = []
        _image_urls.clear()
        for i, u in enumerate(urls, start=1):
            ext = os.path.splitext(urlparse(u).path)[1].lower()
            ext = ext if ext in (".jpg", ".jpeg", ".png", ".webp", ".heic") else ".jpg"
            fid = f"image:{i}"
            _image_urls[fid] = u
            formats.append({"index": i, "name": f"{code}_{i}{ext}", "label": "Image", "size": 0, "format_id": fid})
        return formats, None

    def _download_image(img_url, base, out_dir):
        import requests
        if not img_url: return None, "URL gambar tidak ditemukan (ulangi Extract)"
        try:
            r = requests.get(img_url, headers={"User-Agent": UA}, timeout=60, stream=True)
            if r.status_code != 200: return None, f"HTTP {r.status_code}"
            ext = os.path.splitext(urlparse(img_url).path)[1].lower()
            if ext not in (".jpg", ".jpeg", ".png", ".webp", ".heic"):
                ctype = (r.headers.get("content-type") or "").lower()
                ext = ".png" if "png" in ctype else (".webp" if "webp" in ctype else ".jpg")
            dest = os.path.join(out_dir, base + ext)
            with open(dest, "wb") as f:
                for chunk in r.iter_content(1024 * 256): f.write(chunk)
            ok, msg = _check_file(dest, 1024)
            return (dest, None) if ok else (None, msg)
        except Exception as e: return None, _clean_err(e)[:200]

    COVER_SIZE = 500
    MP3_BITRATE = "320k"
    _notes = []

    def _is_audio_fmt(fmt_id): return str(fmt_id or "").startswith("bestaudio")
    def _meta_artist(info): return info.get("artist") or info.get("creator") or info.get("uploader") or info.get("channel") or "Unknown"

    def _audio_base(info, fallback):
        if not info or not info.get("title"): return fallback
        title = _safe_name(str(info["title"]).replace("_", " "))
        artist = _safe_name(_meta_artist(info), "")
        if artist and artist != "Unknown" and not title.lower().startswith(artist.lower()):
            return _safe_name(f"{artist} - {title}")
        return title

    def _cover_candidates(info):
        urls = []
        main = info.get("thumbnail")
        if main and str(main).startswith("http"): urls.append(main)
        thumbs = [t for t in (info.get("thumbnails") or []) if str(t.get("url") or "").startswith("http")]
        thumbs.sort(key=lambda t: (str(t["url"]).split("?")[0].lower().endswith((".jpg", ".jpeg")),
                                   (t.get("width") or 0) * (t.get("height") or 0)), reverse=True)
        for t in thumbs:
            if t["url"] not in urls: urls.append(t["url"])
        return urls[:6]

    def _cover_bytes(info, size):
        import io, requests
        from PIL import Image
        resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
        for url in _cover_candidates(info):
            try:
                r = requests.get(url, headers={"User-Agent": UA}, timeout=20)
                if r.status_code != 200 or len(r.content) < 1024: continue
                img = Image.open(io.BytesIO(r.content)).convert("RGB")
                w, h = img.size
                side = min(w, h)
                left, top = (w - side) // 2, (h - side) // 2
                img = img.crop((left, top, left + side, top + side)).resize((size, size), resample)
                out = io.BytesIO()
                img.save(out, format="JPEG", quality=90, optimize=True)
                return out.getvalue()
            except Exception: continue
        return None

    def _to_mp3(src, dst):
        try:
            r = _sh(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", src, "-vn", "-map_metadata", "-1",
                     "-c:a", "libmp3lame", "-b:a", MP3_BITRATE, "-ar", "44100", "-ac", "2",
                     "-id3v2_version", "3", "-write_id3v1", "1", dst], timeout=1800)
        except Exception as e: return False, str(e)[:300]
        return r.returncode == 0, (r.stderr[:300] if r.returncode else None)

    def _tag_mp3(path, info, cover):
        from mutagen.id3 import ID3, APIC, TIT2, TPE1, TPE2, TALB, TDRC, COMM, TCON, TRCK, error as ID3Error
        try: tags = ID3(path)
        except ID3Error: tags = ID3()
        for k in ("TIT2", "TPE1", "TPE2", "TALB", "TDRC", "COMM", "TCON", "TRCK", "APIC"): tags.delall(k)
        artist = str(_meta_artist(info))
        tags.add(TIT2(encoding=3, text=str(info.get("track") or info.get("title") or "Unknown")))
        tags.add(TPE1(encoding=3, text=artist))
        tags.add(TPE2(encoding=3, text=artist))
        album = info.get("album") or info.get("uploader") or info.get("channel") or info.get("webpage_url_domain")
        if album: tags.add(TALB(encoding=3, text=str(album)))
        year = str(info.get("release_year") or (info.get("upload_date") or "")[:4] or "")
        if year: tags.add(TDRC(encoding=3, text=year))
        if info.get("track_number"): tags.add(TRCK(encoding=3, text=str(info["track_number"])))
        if info.get("genre"): tags.add(TCON(encoding=3, text=str(info["genre"])))
        if info.get("webpage_url"): tags.add(COMM(encoding=3, lang="eng", desc="Source", text=str(info["webpage_url"])))
        if cover: tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover))
        tags.save(path, v2_version=3)

    def _parse_aria2_line(line):
        out = {}
        m = re.search(r"\((\d+)%\)", line)
        if m: out["percent"] = int(m.group(1))
        m2 = re.search(r"([\d.]+\s*[KMGTP]?i?B)/([\d.]+\s*[KMGTP]?i?B)", line)
        if m2: out["current_human"], out["total_human"] = m2.group(1).strip(), m2.group(2).strip()
        m3 = re.search(r"DL:([\d.]+\s*[KMGTP]?i?B)", line)
        if m3: out["speed_human"] = m3.group(1).strip() + "/s"
        m4 = re.search(r"ETA:(\S+)", line)
        if m4:
            total = 0
            for v, u in re.findall(r"(\d+)([hms])", m4.group(1)): total += int(v) * {"h": 3600, "m": 60, "s": 1}[u]
            out["eta_seconds"] = total
        return out

    def _download_aria2(url, filename, fname, out_dir, headers=None, watchdog=False):
        cmd = ["aria2c", "-x", str(ARIA2_CONN), "-s", str(ARIA2_CONN), "-k", "1M",
               f"--max-connection-per-server={ARIA2_CONN}",
               "-c", "--file-allocation=none", "--allow-overwrite=true", "--auto-file-renaming=false",
               "--max-tries=5", "--retry-wait=3", "--timeout=60", "--console-log-level=warn",
               "--summary-interval=1", f"--out={filename}", f"--dir={out_dir}"]
        hdrs = dict(headers or {})
        ua = hdrs.pop("User-Agent", None) or UA
        cmd.append(f"--user-agent={ua}")
        for k, v in hdrs.items():
            if k.lower() in ("accept-encoding", "host"): continue
            cmd.append(f"--header={k}: {v}")
        cmd.append(url)

        last_err = None
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
            last_pct = -1
            t_begin, slow_since = time.time(), None
            for line in p.stdout:
                if _stop.is_set():
                    p.terminate()
                    return None, "Dihentikan"
                line = line.strip()
                if "error" in line.lower(): last_err = line
                parsed = _parse_aria2_line(line)
                if watchdog and "speed_human" in parsed and time.time() - t_begin > 4:
                    if _human_to_bytes(parsed["speed_human"].replace("/s", "")) < SLOW_MIN_BPS:
                        slow_since = slow_since or time.time()
                        if time.time() - slow_since >= SLOW_WINDOW:
                            p.terminate()
                            return None, "SLOW"
                    else: slow_since = None
                if "percent" not in parsed or parsed["percent"] == last_pct: continue
                last_pct = parsed["percent"]
                _set_state(phase="downloading", message="", **parsed)
                _post_update({"status": "downloading", "phase": "download", "file_name": fname,
                              "save_status": None, **parsed}, retries=1)
            p.wait()
            if p.returncode != 0: return None, last_err or f"aria2c exit {p.returncode}"
            dest = os.path.join(out_dir, filename)
            ok, msg = _check_file(dest, 1)
            return (dest, None) if ok else (None, msg)
        except Exception as e: return None, str(e)[:200]

    def _try_aria2_streams(url, fmt, st, tmp, fname):
        if not USE_ARIA2_YT or not _is_youtube(url): return False
        from yt_dlp import YoutubeDL

        def wipe():
            for f in os.listdir(tmp):
                if f.startswith(("part", "out.")):
                    try: os.remove(os.path.join(tmp, f))
                    except Exception: pass

        def make_opts(fmt_override):
            o = {"quiet": True, "no_warnings": True, "noplaylist": True, "socket_timeout": 60, "retries": 5,
                 "format": fmt_override, "http_headers": {"User-Agent": UA}, **_cookie_opts()}
            if st["client"]: o["extractor_args"] = {"youtube": {"player_client": st["client"]}}
            return o

        base_headers = {
            "User-Agent": UA, "Referer": "https://www.youtube.com/", "Origin": "https://www.youtube.com",
            "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9", "Accept-Encoding": "identity",
        }

        try:
            with YoutubeDL(make_opts(fmt)) as ydl: initial = ydl.extract_info(url, download=False)
            parts_meta = initial.get("requested_formats") or [initial]

            files = []
            for n, p in enumerate(parts_meta):
                if _stop.is_set():
                    wipe()
                    return False

                if len(parts_meta) > 1:
                    if (p.get("vcodec") or "none") != "none": fresh_fmt = f"bestvideo[height<={p.get('height') or 9999}]/bestvideo"
                    else: fresh_fmt = "bestaudio/best"
                else: fresh_fmt = fmt

                got = False
                for attempt in range(SLOW_RETRIES + 1):
                    if _stop.is_set():
                        wipe()
                        return False
                    try:
                        _set_state(message=f"aria2c: refresh URL stream {n + 1}/{len(parts_meta)}"
                                   if attempt == 0 else
                                   f"aria2c: retry stream {n + 1}/{len(parts_meta)} ({attempt + 1})")
                        with YoutubeDL(make_opts(fresh_fmt)) as ydl: fresh = ydl.extract_info(url, download=False)
                        rparts = fresh.get("requested_formats") or [fresh]
                        if len(rparts) > 1 and len(parts_meta) > 1:
                            target = rparts[0] if (rparts[0].get("vcodec") or "none") != "none" else rparts[1]
                        else: target = rparts[0]
                        stream_url = target.get("url")
                        stream_headers = target.get("http_headers") or {}
                        if not stream_url or str(target.get("protocol") or "") not in ("https", "http"):
                            raise RuntimeError("no url")
                    except Exception:
                        if attempt >= SLOW_RETRIES:
                            wipe()
                            return False
                        continue

                    ext = target.get("ext") or "bin"
                    name = f"part{n}.{ext}"
                    path = os.path.join(tmp, name)
                    marker = path + ".ok"
                    if os.path.exists(marker) and os.path.exists(path):
                        files.append(path)
                        got = True
                        break

                    hdrs = dict(base_headers)
                    for k, v in stream_headers.items():
                        if k.lower() in ("accept-encoding", "host", "content-length"): continue
                        hdrs[k] = v

                    dest, err = _download_aria2(stream_url, name, fname, tmp, headers=hdrs,
                                                watchdog=(attempt < SLOW_RETRIES))
                    if dest:
                        open(marker, "w").close()
                        files.append(dest)
                        got = True
                        break
                    if err == "SLOW": continue
                    wipe()
                    return False

                if not got:
                    wipe()
                    return False

            if len(files) == 1:
                os.replace(files[0], os.path.join(tmp, "out." + (os.path.splitext(files[0])[1].lstrip(".") or "mp4")))
            else:
                _set_state(message="Merging (ffmpeg)...", percent=100)
                r = _sh(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", files[0], "-i", files[1],
                         "-map", "0:v:0", "-map", "1:a:0", "-c", "copy", "-movflags", "+faststart",
                         os.path.join(tmp, "out.mp4")], timeout=3600)
                if r.returncode != 0:
                    wipe()
                    return False
            for f in os.listdir(tmp):
                if f.startswith("part"): os.remove(os.path.join(tmp, f))
            return True
        except Exception:
            wipe()
            return False

    def _make_hook(fname, ctx, watch=False):
        def hook(d):
            if _stop.is_set(): raise KeyboardInterrupt("stopped")
            st = d.get("status")
            if st == "downloading":
                now = time.time()
                if now - ctx["t"] < 1.0: return
                ctx["t"] = now
                done = d.get("downloaded_bytes") or 0
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                if watch:
                    key = d.get("filename")
                    if ctx.get("key") != key: ctx.update(key=key, w_t=now, w_b=done)
                    elif now - ctx["w_t"] >= SLOW_WINDOW:
                        rate = (done - ctx["w_b"]) / max(now - ctx["w_t"], 1)
                        if rate < SLOW_MIN_BPS and not (total and total - done < 2 * 1024 * 1024):
                            raise _SlowStream()
                        ctx.update(w_t=now, w_b=done)
                pct = round(min(done / total * 100, 100), 1) if total else 0
                spd = d.get("speed") or 0
                upd = {"percent": pct, "current_human": _format_size(done), "total_human": _format_size(total),
                       "speed_human": _format_size(spd) + "/s", "eta_seconds": d.get("eta")}
                _set_state(phase="downloading", message="", **upd)
                _post_update({"status": "downloading", "phase": "download", "file_name": fname,
                              "save_status": None, **upd}, retries=1)
            elif st == "finished": _set_state(message="Merging / finalizing...")
        return hook

    def _is_slow_exc(e):
        if isinstance(e, _SlowStream): return True
        cur = e
        for _ in range(5):
            if cur is None: break
            if isinstance(cur, _SlowStream): return True
            nxt = getattr(cur, "__cause__", None) or getattr(cur, "__context__", None)
            if nxt is cur: break
            cur = nxt
        txt = _clean_err(e).lower()
        return "_slowstream" in txt or "slow stream" in txt

    def _download_ytdlp(url, fmt, base, first_strat, fname, out_dir, audio=False, info=None):
        from yt_dlp import YoutubeDL
        strats = ([first_strat] if first_strat else []) + (STRATS if _is_youtube(url) else STRATS[:1])
        uniq, seen = [], set()
        for s in strats:
            if str(s["client"]) not in seen:
                seen.add(str(s["client"]))
                uniq.append(s)

        last_err = "unknown"
        for st in uniq:
            if _stop.is_set(): return None, "Dihentikan"
            tmp = tempfile.mkdtemp(prefix="uvd_", dir=TEMP_PATH)
            try:
                _set_state(message=f"Strategi download: {st['name']}", percent=0)
                used_aria = _try_aria2_streams(url, fmt, st, tmp, fname)
                if _stop.is_set(): return None, "Dihentikan"
                if not used_aria:
                    opts = {
                        "outtmpl": os.path.join(tmp, "out.%(ext)s"), "format": fmt, "merge_output_format": "mp4",
                        "quiet": True, "no_warnings": True, "noplaylist": True, "noprogress": True,
                        "retries": 30, "fragment_retries": 30, "extractor_retries": 10, "file_access_retries": 10,
                        "socket_timeout": 60, "continuedl": True, "concurrent_fragment_downloads": 16,
                        "http_chunk_size": 1 * 1024 * 1024, "buffersize": 1024 * 1024,
                        "http_headers": {"User-Agent": UA},
                        "ffmpeg_location": shutil.which("ffmpeg"), "paths": {"temp": tmp}, **_cookie_opts(),
                    }
                    if st["client"]: opts["extractor_args"] = {"youtube": {"player_client": st["client"]}}
                    failed_dl = False
                    for attempt in range(SLOW_RETRIES + 1):
                        watch = _is_youtube(url) and attempt < SLOW_RETRIES
                        opts["progress_hooks"] = [_make_hook(fname, {"t": 0}, watch)]
                        try:
                            with YoutubeDL(opts) as ydl: ydl.download([url])
                            break
                        except _SlowStream:
                            _set_state(message=f"Stream lambat, request baru (percobaan {attempt + 2})...")
                            continue
                        except KeyboardInterrupt: return None, "Dihentikan"
                        except Exception as e:
                            if _is_slow_exc(e):
                                _set_state(message=f"Stream lambat, request baru (percobaan {attempt + 2})...")
                                continue
                            if _stop.is_set(): return None, "Dihentikan"
                            last_err = _clean_err(e)[:200]
                            failed_dl = True
                            break
                    if failed_dl: continue

                files = [os.path.join(tmp, f) for f in os.listdir(tmp)
                         if os.path.isfile(os.path.join(tmp, f)) and not f.endswith((".part", ".ytdl", ".temp", ".aria2"))]
                if not files:
                    last_err = "File tidak dihasilkan"
                    continue
                src = max(files, key=os.path.getsize)
                ok, msg = _check_file(src, 10 * 1024)
                if not ok:
                    last_err = msg
                    continue
                if audio:
                    _set_state(message=f"Converting ke MP3 ({MP3_BITRATE})...", percent=100)
                    _post_update({"status": "downloading", "phase": "download", "file_name": fname, "percent": 100,
                                  "current_human": _format_size(os.path.getsize(src)),
                                  "total_human": _format_size(os.path.getsize(src)),
                                  "speed_human": "converting...", "eta_seconds": 0, "save_status": None}, retries=1)
                    mp3 = os.path.join(tmp, "final.mp3")
                    ok, cerr = _to_mp3(src, mp3)
                    if not ok: return None, f"FFmpeg: {cerr}"
                    if info:
                        _set_state(message="Embed metadata + cover...")
                        cover = _cover_bytes(info, COVER_SIZE)
                        try: _tag_mp3(mp3, info, cover)
                        except Exception as e: return None, f"Gagal embed metadata: {_clean_err(e)[:150]}"
                        if not cover: _notes.append(f"cover tidak tersedia untuk '{base}'")
                    src, ext = mp3, ".mp3"
                else: ext = os.path.splitext(src)[1] or ".mp4"
                dest = os.path.join(out_dir, base + ext)
                _move_final(src, dest)
                return dest, None
            finally: shutil.rmtree(tmp, ignore_errors=True)
        return None, last_err

    def _upload_one_file(local_path, hf_token, repo_id, repo_type, folder, idx, total_files):
        fname = os.path.basename(local_path)
        try: size = os.path.getsize(local_path)
        except Exception: size = 0

        _set_state(phase="uploading", current_file=fname, current_index=idx, percent=0,
                   current_human="0 B", total_human=_format_size(size), speed_human="0 B/s",
                   eta_seconds=None, message="Uploading to Hugging Face...", error="")
        _post_update({"status": "uploading", "phase": "upload", "save_status": "uploading", "file_name": fname,
                      "percent": 0, "current_human": "0 B", "total_human": _format_size(size), "speed_human": "0 B/s"})

        stop = {"stop": False}
        def animate():
            t0, last = time.time(), 0
            while not stop["stop"]:
                time.sleep(0.5)
                el = time.time() - t0
                if stop["stop"] or el < 1.5 or time.time() - last < 2: continue
                last = time.time()
                frac = min(1 - pow(0.5, el / 30.0), 0.95)
                cur = int(size * frac)
                spd = cur / el if el > 0 else 0
                eta = int((size - cur) / spd) if spd > 0 and cur < size else None
                upd = {"percent": int(frac * 100), "current_human": _format_size(cur), "total_human": _format_size(size),
                       "speed_human": _format_size(spd) + "/s", "eta_seconds": eta}
                _set_state(phase="uploading", message="", **upd)
                _post_update({"status": "uploading", "phase": "upload", "save_status": "uploading",
                              "file_name": fname, **upd}, retries=1)

        threading.Thread(target=animate, daemon=True).start()
        try:
            from huggingface_hub import HfApi
            api = HfApi(token=hf_token)
            path_in_repo = f"{folder}/{fname}" if folder else fname
            api.upload_file(path_or_fileobj=local_path, path_in_repo=path_in_repo, repo_id=repo_id,
                            repo_type=repo_type, commit_message=f"Add {fname}")
            stop["stop"] = True
            prefix = {"dataset": "datasets/", "space": "spaces/", "model": "models/"}.get(repo_type, "")
            save_url = f"https://huggingface.co/{prefix}{repo_id}/blob/main/{path_in_repo}"
            _set_state(phase="uploading", percent=100, current_human=_format_size(size),
                       total_human=_format_size(size), speed_human="0 B/s", eta_seconds=0, message="Uploaded")
            _post_update({"status": "saved", "save_status": "done", "save_url": save_url, "file_name": fname,
                          "phase": "upload", "percent": 100, "current_human": _format_size(size),
                          "total_human": _format_size(size), "speed_human": "0 B/s"})
            return True, save_url
        except Exception as e:
            stop["stop"] = True
            err = _clean_err(e)[:300]
            _post_update({"status": "error", "save_status": "error", "save_error": err,
                          "file_name": fname, "phase": "upload"})
            return False, err

    def _fail(msg, phase="resolve"):
        _set_state(phase="error", error=msg, message="")
        _post_update({"status": "error", "phase": phase, "error": msg[:300], "finished": True})

    def _wait_for_selection(first_round):
        t_start, fails = time.time(), 0
        while True:
            if _stop.wait(3): return "stopped"
            status = _check_status(SESSION_ID, BACKEND_URL)
            if status == "download": return "download"
            if status == "newurl": return "newurl"
            if status == "waiting":
                fails = 0
                if time.time() - t_start > WAIT_MAX_SECONDS: return "timeout"
                continue
            if status == "cancel" or (first_round and status == "error"): return "cancel"
            if status is None:
                fails += 1
                if fails >= WAIT_FAIL_LIMIT: return "lost"
            else: fails = 0
            if time.time() - t_start > WAIT_MAX_SECONDS: return "timeout"

    def _run_round(url, cfg, first_strat, info, formats_all):
        selected = cfg["formats"]
        hf_token = cfg.get("token")
        repo_id = cfg.get("repo_id")
        repo_type = cfg.get("repo_type") or "dataset"
        folder = cfg.get("folder") or ""

        total = len(selected)
        _notes.clear()
        _state["files"] = [{"name": _display_name(i), "size": i.get("size", 0), "_status": "pending"} for i in selected]
        completed, failed, errors = 0, 0, []
        _set_state(phase="downloading", total_files=total, completed_files=0, failed_files=0, percent=0,
                   message=f"Mulai download {total} file.", error="")

        stopped = False
        for idx, item in enumerate(selected, start=1):
            if _stop.is_set():
                stopped = True
                break
            name = item.get("name") or "download"
            fmt_id = item.get("format_id") or BEST_FORMAT
            is_direct = fmt_id == "direct"
            is_image = str(fmt_id).startswith("image:")
            is_audio = (not is_direct) and (not is_image) and _is_audio_fmt(fmt_id)
            label = item.get("label")

            if is_direct: base = _safe_name(name)
            elif is_image: base = _safe_name(os.path.splitext(name)[0])
            elif is_audio: base = _audio_base(info, _base_from_item(_strip_ext(name)))
            elif label: base = _video_base(_strip_ext(name), label)
            else: base = _base_from_item(_strip_ext(name))
            out_dir = tempfile.mkdtemp(prefix="uvd_out_", dir=TEMP_PATH)

            _state["files"][idx - 1]["_status"] = "downloading"
            _set_state(phase="downloading", current_file=base, current_index=idx, percent=0,
                       current_human="0 B", total_human="0 B", speed_human="0 B/s", eta_seconds=None, message="")
            _post_update({"status": "downloading", "phase": "download", "file_name": base, "percent": 0,
                          "current_human": "0 B", "speed_human": "0 B/s", "save_status": None,
                          "finished": False, "error": None, "save_error": None})

            if is_image:
                _set_state(message="Downloading image...")
                dest, err = _download_image(_image_urls.get(fmt_id), base, out_dir)
            elif is_direct:
                _set_state(message="Downloading file...")
                direct_url = item.get("url") or url
                
                if direct_url and "mega.nz" in direct_url:
                    _set_state(message="Downloading via megadl...")
                    dest = os.path.join(out_dir, base)
                    if not shutil.which("megadl"): _run_quiet(["apt-get", "install", "-y", "-qq", "megatools"])
                    r = subprocess.run(["megadl", "--path", out_dir, direct_url], capture_output=True, text=True)
                    if r.returncode != 0: dest, err = None, f"megadl error: {r.stderr[:200]}"
                    else:
                        files = [f for f in os.listdir(out_dir) if not f.endswith(".tmp")]
                        if files: os.rename(os.path.join(out_dir, files[0]), dest)
                        else: dest, err = None, "File MEGA tidak ditemukan setelah download"
                elif direct_url and ("drive.google.com" in direct_url or "docs.google.com" in direct_url):
                    _set_state(message="Downloading via gdown...")
                    dest_dir = os.path.join(out_dir, base)
                    os.makedirs(dest_dir, exist_ok=True)

                    try:
                        import gdown  # noqa: F401
                    except Exception:
                        _run_quiet([sys.executable, "-m", "pip", "install", "-q", "-U", "gdown"])

                    if "/folders/" in direct_url:
                        cmd = [sys.executable, "-m", "gdown", "--folder", direct_url, "-O", dest_dir, "--remaining-ok"]
                    else:
                        cmd = [sys.executable, "-m", "gdown", "--fuzzy", direct_url, "-O", dest_dir + os.sep]

                    r = subprocess.run(cmd, capture_output=True, text=True)

                    if r.returncode != 0:
                        dest, err = None, f"gdown error: {(r.stderr or r.stdout)[-300:]}"
                    else:
                        downloaded = []
                        for root, dirs, files in os.walk(dest_dir):
                            for f in files:
                                downloaded.append(os.path.join(root, f))
                        
                        if not downloaded:
                            dest, err = None, "Folder Google Drive kosong atau gagal diunduh"
                        else:
                            if len(downloaded) == 1:
                                dest = os.path.join(out_dir, base)
                                os.rename(downloaded[0], dest)
                            else:
                                dest = os.path.join(out_dir, base + ".zip")
                                shutil.make_archive(os.path.join(out_dir, base), 'zip', dest_dir)
                                shutil.rmtree(dest_dir, ignore_errors=True)
                            
                            ok, msg = _check_file(dest)
                            if not ok:
                                dest, err = None, msg
                                
                elif direct_url: 
                    dest, err = _download_aria2(direct_url, base, base, out_dir)
                else: 
                    dest, err = _download_aria2(url, base, base, out_dir)
            else:
                dest, err = _download_ytdlp(url, fmt_id, base, first_strat, base, out_dir, audio=is_audio, info=info)

            if _stop.is_set():
                shutil.rmtree(out_dir, ignore_errors=True)
                stopped = True
                break
            if not dest:
                failed += 1
                errors.append(f"[{idx}] {err}")
                _state["files"][idx - 1]["_status"] = "error"
                _set_state(failed_files=failed, error=f"[{idx}/{total}] Gagal: {err}")
                shutil.rmtree(out_dir, ignore_errors=True)
                continue

            _state["files"][idx - 1]["_status"] = "uploading"
            ok, res = _upload_one_file(dest, hf_token, repo_id, repo_type, folder, idx, total)
            shutil.rmtree(out_dir, ignore_errors=True)
            if ok:
                completed += 1
                _state["files"][idx - 1]["_status"] = "done"
            else:
                failed += 1
                errors.append(f"[{idx}] upload: {res}")
                _state["files"][idx - 1]["_status"] = "error"
            _set_state(completed_files=completed, failed_files=failed)

        if stopped: return True
        all_ok = failed == 0
        err_text = " | ".join(errors)[:300]
        _set_state(phase="done" if all_ok else "error", percent=100,
                   message=f"Selesai: {completed} OK, {failed} Gagal." + ((" Catatan: " + "; ".join(_notes)) if _notes else ""),
                   error="" if all_ok else err_text)
        _post_update({"status": "done" if all_ok else "error", "phase": "done", "finished": True, "percent": 100,
                      "save_status": "done" if all_ok else "error",
                      "error": None if all_ok else err_text, "save_error": None if all_ok else err_text,
                      "formats": formats_all})
        return False

    def _resolve_url(url):
        _set_state(phase="waiting", message="Analyzing source URL...")
        path_l = urlparse(url).path.lower()
        first_strat = None
        formats = []
        info = None

        _h = _host(url)
        if _h == "mega.nz" or _h.endswith(".mega.nz") or _h == "drive.google.com" or _h == "docs.google.com":
            # MEGA / Google Drive: jangan lewat yt-dlp (error 'bool'), langsung handler direct
            fname = "gdrive_download" if "google.com" in _h else "mega_download"
            formats = [{"index": 1, "name": fname, "size": 0, "format_id": "direct", "url": url}]
        elif path_l.endswith(DIRECT_EXTS):
            fname = _safe_name(os.path.basename(urlparse(url).path)) or "download"
            formats = [{"index": 1, "name": fname, "size": 0, "format_id": "direct"}]
        else:
            info, first_strat, errs = _probe(url)
            if info: formats = _build_formats(info)
            elif _is_instagram(url):
                _set_state(message="Bukan video? Coba ambil sebagai gambar (gallery-dl)...")
                formats, ierr = _probe_images(url)
                if not formats:
                    hint = "" if os.path.exists(COOKIES_FILE) else " Kalau postingan butuh login, taruh cookies Netscape di /content/cookies.txt."
                    _fail(f"Instagram: bukan video & gambar gagal diambil ({ierr}).{hint}"[:400])
                    return None
            elif not _is_youtube(url) and _looks_like_file(url):
                fname = _safe_name(os.path.basename(urlparse(url).path)) or "download"
                formats = [{"index": 1, "name": fname, "size": 0, "format_id": "direct"}]
            else:
                detail = " | ".join(errs) if errs else "unknown"
                _fail(f"Failed to extract. {detail}"[:400])
                return None

        return info, first_strat, formats

    def _meta_from_info(info):
        if not info: return None
        thumb = info.get("thumbnail")
        if not (isinstance(thumb, str) and thumb.startswith("https://")):
            cands = [u for u in _cover_candidates(info) if str(u).startswith("https://")]
            thumb = cands[0] if cands else None
        return {"title": info.get("title") or "", "thumbnail": thumb,
                "uploader": info.get("uploader") or info.get("channel"), "duration": info.get("duration")}

    def _publish_formats(formats, info=None):
        _state["files"] = [{"name": _display_name(f), "size": f.get("size", 0), "_status": "pending", "_index": f["index"]} for f in formats]
        _set_state(phase="waiting", total_files=len(formats), completed_files=0, failed_files=0, error="",
                   message="Pilih format di web, Colab akan otomatis lanjut.")
        _post_update({"status": "waiting", "phase": "resolve", "finished": False, "formats": formats,
                      "meta": _meta_from_info(info)})

    def _start_session(session_id, backend_url):
        nonlocal SESSION_ID, BACKEND_URL
        session_id = (session_id or "").strip()
        backend_url = (backend_url or "").strip()
        _notes.clear()
        _ensure_dirs()

        if not session_id or not backend_url:
            _set_state(phase="error", error="Session ID and Backend URL are required")
            return

        SESSION_ID, BACKEND_URL = session_id, backend_url

        _set_state(phase="fetching", message=f"Fetching config from {backend_url}")
        cfg = _fetch_config(session_id, backend_url)
        if not cfg or not cfg.get("ok"):
            _set_state(phase="error", message="", error="Failed to fetch config. Check Session ID / Backend URL.")
            return

        url = (cfg.get("url") or "").strip()
        if not url:
            _fail("Config tidak berisi URL.")
            return

        r = _resolve_url(url)
        if not r: return
        info, first_strat, formats = r

        first_round = True
        if cfg.get("preselected") and cfg.get("formats"):
            _post_update({"preselected": False}, retries=1)
            if _run_round(url, cfg, first_strat, info, formats):
                _on_stopped()
                return
            first_round = False
            _set_state(message=_state["message"] + " Menunggu pilihan berikutnya di web...")
        else: _publish_formats(formats, info)

        while True:
            res = _wait_for_selection(first_round)
            if res == "newurl":
                cfg = _fetch_config(SESSION_ID, BACKEND_URL)
                new_url = ((cfg or {}).get("url") or "").strip()
                if not new_url:
                    _set_state(phase="error", message="", error="Gagal mengambil URL baru dari web.")
                    return
                url = new_url
                _image_urls.clear()
                _reset_state()
                r = _resolve_url(url)
                first_round = False
                if not r: continue
                info, first_strat, formats = r
                if cfg.get("preselected") and cfg.get("formats"):
                    _post_update({"preselected": False}, retries=1)
                    if _run_round(url, cfg, first_strat, info, formats):
                        _on_stopped()
                        return
                    _set_state(message=_state["message"] + " Menunggu pilihan berikutnya di web...")
                    continue
                _publish_formats(formats, info)
                continue
            if res == "stopped":
                _on_stopped()
                return
            if res != "download":
                msgs = {"cancel": "Dibatalkan oleh user.",
                        "lost": "Backend tidak terjangkau / sesi kedaluwarsa.",
                        "timeout": "Timeout menunggu pilihan format."}
                _set_state(phase="error", message="", error=msgs.get(res, res))
                return

            cfg = _fetch_config(SESSION_ID, BACKEND_URL)
            if not cfg or not cfg.get("formats"):
                _set_state(phase="error", message="", error="Gagal mengambil daftar pilihan dari web.")
                return

            if _run_round(url, cfg, first_strat, info, formats):
                _on_stopped()
                return
            first_round = False
            _set_state(message=_state["message"] + " Menunggu pilihan berikutnya di web...")

    def _on_stopped():
        _set_state(phase="error", message="", error="Dihentikan. Tekan START DOWNLOAD lagi (Session ID sama).")
        _post_update({"status": "error", "phase": "resolve", "error": "Dihentikan dari Colab", "finished": True})

    def _on_start_click(session_id, backend_url):
        nonlocal _runner
        if _runner is not None and _runner.is_alive():
            _set_state(message="Masih berjalan. Tekan STOP dulu kalau mau mulai ulang.")
            return

        _stop.clear()
        _reset_state()

        def run():
            try: _start_session(session_id, backend_url)
            except KeyboardInterrupt: _on_stopped()
            except Exception as e:
                _set_state(phase="error", message="", error=f"Unexpected: {_clean_err(e)[:300]}")
                _post_update({"status": "error", "phase": "resolve", "error": _clean_err(e)[:300], "finished": True})

        _runner = threading.Thread(target=run, daemon=True)
        _runner.start()

    def _on_stop_click():
        if _runner is None or not _runner.is_alive():
            _set_state(message="Tidak ada proses yang berjalan.")
            return
        _stop.set()
        _set_state(message="Menghentikan... (upload yang sedang berjalan selesai dulu)")

    try:
        from google.colab import output as _colab_output
        _colab_output.register_callback("universal_start", _on_start_click)
        _colab_output.register_callback("universal_stop", _on_stop_click)
        _colab_output.register_callback("universal_poll", _poll)
    except Exception: pass

    def _bootstrap():
        _ensure_dirs()
        display(HTML("""
        <div style="font-family:monospace;margin:10px 0;padding:18px;background:linear-gradient(135deg,#0f0f1e,#1a1a2e,#16213e);
                    border-radius:12px;border:1px solid #2a2a4a;color:#e0e0e0;">
            <div style="color:#4CAF50;font-size:18px;font-weight:bold;letter-spacing:2px;text-align:center;">UNIVERSAL COLAB</div>
            <div style="color:#888;font-size:12px;margin-top:4px;text-align:center;">
                Download media universal di Colab · Upload otomatis ke Hugging Face</div>
            <div style="margin-top:18px;">
                <label style="font-size:11px;color:#888;letter-spacing:1px;">SESSION ID</label>
                <input id="uvd-session-id" type="text" placeholder="paste dari kernel log"
                       style="width:100%;margin-top:6px;padding:10px 12px;background:#0a0a14;border:1px solid #2a2a4a;
                              border-radius:6px;color:#e0e0e0;font-family:monospace;font-size:13px;outline:none;box-sizing:border-box;" />
            </div>
            <div style="margin-top:12px;">
                <label style="font-size:11px;color:#888;letter-spacing:1px;">BACKEND URL</label>
                <input id="uvd-backend-url" type="text" value="https://warthadev-backend.onrender.com"
                       style="width:100%;margin-top:6px;padding:10px 12px;background:#0a0a14;border:1px solid #2a2a4a;
                              border-radius:6px;color:#e0e0e0;font-family:monospace;font-size:13px;outline:none;box-sizing:border-box;" />
            </div>
            <button id="uvd-start-btn"
                    style="margin-top:16px;width:100%;padding:12px 20px;background:linear-gradient(90deg,#4CAF50,#00BCD4);
                           border:none;border-radius:8px;color:white;font-family:monospace;font-size:14px;font-weight:bold;
                           cursor:pointer;letter-spacing:1px;">START DOWNLOAD</button>
            <button id="uvd-stop-btn"
                    style="margin-top:10px;width:100%;padding:12px 20px;background:linear-gradient(90deg,#F44336,#FF9800);
                           border:none;border-radius:8px;color:white;font-family:monospace;font-size:14px;font-weight:bold;
                           cursor:pointer;letter-spacing:1px;">STOP</button>
            <div id="uvd-ui" style="margin-top:6px;"></div>
        </div>
        <script>
        (function() {
            var btn = document.getElementById('uvd-start-btn');
            if (!btn) return;
            btn.addEventListener('click', function() {
                var sid = (document.getElementById('uvd-session-id').value || '').trim();
                var backend = (document.getElementById('uvd-backend-url').value || '').trim();
                if (!sid) { alert('Isi Session ID dulu'); return; }
                google.colab.kernel.invokeFunction('universal_start', [sid, backend], {});
            });
            var stop = document.getElementById('uvd-stop-btn');
            if (stop) stop.addEventListener('click', function() {
                google.colab.kernel.invokeFunction('universal_stop', [], {});
            });
            if (window.__uvdPoll) { clearInterval(window.__uvdPoll); }
            var busy = false;
            async function tick() {
                if (busy) return;
                var box = document.getElementById('uvd-ui');
                if (!box) { clearInterval(window.__uvdPoll); return; }
                busy = true;
                try {
                    var r = await google.colab.kernel.invokeFunction('universal_poll', [], {});
                    var html = r.data['application/json'].html;
                    if (box.__last !== html) { box.innerHTML = html; box.__last = html; }
                } catch (e) {}
                busy = false;
            }
            window.__uvdPoll = setInterval(tick, 1000);
            tick();
        })();
        </script>"""))

    _bootstrap()