# Kaggle worker for "AI Local": boots llama-server (GPU) from the user's
# private Media AI Local dataset, then runs the same agent tool loop as
# codekaggle.py -- but headless, driven by jobs polled from the backend
# instead of an interactive input() prompt.
import os
import re
import sys
import glob
import html
import json
import time
import base64
import mimetypes
import shutil
import subprocess
import threading
import importlib
import importlib.util
import urllib.parse
import urllib.request

os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"

WORKER_VERSION = "2026-11-25-ai-local-any-file-multi"
IS_KAGGLE = os.path.isdir("/kaggle") or "KAGGLE_KERNEL_RUN_TYPE" in os.environ
BASE_DIR = "/kaggle/working"
DEFAULT_PORT = 8080
DEFAULT_MAX_CTX = 131072

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

PROJECT_IGNORE_DIRS = {
    "node_modules", ".git", "__pycache__", ".venv", "venv", "dist", "build", ".next", ".cache",
    "llama_bin", "model_gguf",
}
SYS_SHALLOW_DIRS = {"proc", "sys", "dev", "usr", "lib", "lib64", "var", "etc", "boot", "run", "opt", "srv"}


# ---------------------------------------------------------------------------
# File delivery ke chat: kecil = base64 inline, besar = upload HF (browser unduh langsung)
# ---------------------------------------------------------------------------
import uuid
import zipfile
from pathlib import Path as _Path

_DELIVER_WORK_ROOT = _Path(os.getenv("WARTHA_WORK_ROOT", "/kaggle/working")).resolve()
_DELIVER_INLINE_MAX = int(os.getenv("WARTHA_INLINE_MAX", str(1_500_000)))
_DELIVER_UPLOAD_MAX = int(os.getenv("WARTHA_UPLOAD_MAX", str(500 * 1024 * 1024)))
_DELIVER_INLINE_FALLBACK_MAX = int(os.getenv("WARTHA_INLINE_FALLBACK_MAX", str(8_000_000)))
_DELIVER_HF_REPO = os.getenv("WARTHA_HF_REPO", "")
# Semua jenis file boleh dikirim (tanpa whitelist ekstensi). Yang diblokir hanya rahasia/kredensial.
_DELIVER_BLOCKED = {"kaggle.json", ".env", ".netrc", "id_rsa", "id_ed25519", "id_ecdsa", "credentials", "secrets.json"}
_DELIVER_BLOCKED_SUFFIX = (".pem", ".key", ".p12", ".pfx", ".keystore")
_DELIVER_MARKER = re.compile(r"\[\[file:([^\]\n]+)\]\]")
_DELIVER_MAX_FILES = int(os.getenv("WARTHA_DELIVER_MAX_FILES", "25"))  # lebih dari ini -> digabung jadi 1 zip
_DELIVER_INLINE_TOTAL = int(os.getenv("WARTHA_INLINE_TOTAL", str(20_000_000)))  # total base64 mentah per jawaban


def _deliver_secret(name):
    value = os.getenv(name, "")
    if value:
        return value
    try:
        from kaggle_secrets import UserSecretsClient  # type: ignore
        return UserSecretsClient().get_secret(name)
    except Exception:
        return ""


def _deliver_safe_path(raw):
    ref = (raw or "").strip().strip("`'\"")
    if not ref.startswith("/"):
        # Nama/relatif: cari di folder output dulu, lalu di root kerja.
        for base in (_Path(_OUTPUT_DIR), _DELIVER_WORK_ROOT):
            if (base / ref).exists():
                ref = str(base / ref)
                break
    path = _Path(ref).expanduser().resolve()
    if _DELIVER_WORK_ROOT not in path.parents and path != _DELIVER_WORK_ROOT:
        raise ValueError("file di luar /kaggle/working tidak boleh dikirim")
    if any(part.startswith(".") for part in path.relative_to(_DELIVER_WORK_ROOT).parts):
        raise ValueError("file tersembunyi tidak boleh dikirim")
    if path.name.lower() in _DELIVER_BLOCKED or path.name.lower().endswith(_DELIVER_BLOCKED_SUFFIX):
        raise ValueError("file sensitif tidak boleh dikirim")
    if not path.exists():
        raise ValueError("file tidak ditemukan")
    return path


def _deliver_zip_dir(folder):
    target = _DELIVER_WORK_ROOT / "_deliver" / f"{folder.name}.zip"
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for item in folder.rglob("*"):
            if item.is_file() and not any(p.startswith(".") for p in item.relative_to(folder).parts):
                archive.write(item, item.relative_to(folder.parent))
    return target


def _deliver_zip_files(paths, name):
    """Banyak file sekaligus -> satu zip (dipakai saat jumlah file melebihi batas)."""
    target = _DELIVER_WORK_ROOT / "_deliver" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        used = set()
        for item in paths:
            arc = item.name
            n = 1
            while arc in used:
                n += 1
                arc = f"{item.stem}_{n}{item.suffix}"
            used.add(arc)
            archive.write(item, arc)
    return target


def _deliver_upload_hf(path, session_id):
    token = _deliver_secret("HF_TOKEN")
    if not (_DELIVER_HF_REPO and token):
        raise RuntimeError("WARTHA_HF_REPO / HF_TOKEN belum diatur")
    from huggingface_hub import HfApi  # type: ignore
    remote = f"{session_id or 'session'}/{uuid.uuid4().hex[:12]}/{path.name}"
    HfApi(token=token).upload_file(
        path_or_fileobj=str(path), path_in_repo=remote, repo_id=_DELIVER_HF_REPO, repo_type="dataset",
    )
    return f"https://huggingface.co/datasets/{_DELIVER_HF_REPO}/resolve/main/{remote}?download=true"


def deliver_file(raw_path, session_id="", budget=None):
    """Satu file (atau folder -> zip) -> entri lampiran. `budget` = {"inline": sisa byte inline} dibagi
    ke semua file dalam satu jawaban supaya banyak file tidak membuat balasan raksasa."""
    budget = budget if budget is not None else {"inline": _DELIVER_INLINE_TOTAL}
    path = raw_path if isinstance(raw_path, _Path) else _deliver_safe_path(raw_path)
    if path.is_dir():
        path = _deliver_zip_dir(path)
    size = path.stat().st_size
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    entry = {"name": path.name, "mime_type": mime, "size": size, "path": str(path)}

    def _inline():
        entry["data_base64"] = base64.b64encode(path.read_bytes()).decode("ascii")
        budget["inline"] -= size
        return entry

    if size <= _DELIVER_INLINE_MAX and size <= budget["inline"]:
        return _inline()
    if size > _DELIVER_UPLOAD_MAX:
        raise ValueError(f"file terlalu besar ({size // (1024 * 1024)} MB, batas {_DELIVER_UPLOAD_MAX // (1024 * 1024)} MB)")
    try:
        entry["url"] = _deliver_upload_hf(path, session_id)
        return entry
    except Exception as err:
        if size <= _DELIVER_INLINE_FALLBACK_MAX and size <= budget["inline"]:
            return _inline()
        reason = "Hugging Face belum diatur" if "belum diatur" in str(err) else f"upload ke Hugging Face gagal ({err})"
        raise ValueError(f"{reason}, dan file terlalu besar untuk dikirim langsung di chat")


def _expand_deliver_ref(raw):
    """Satu marker bisa berupa file, folder, atau pola glob (*.png) -> daftar path."""
    ref = (raw or "").strip().strip("`'\"")
    if any(ch in ref for ch in "*?"):
        import glob as _glob
        base = ref if ref.startswith("/") else str(_Path(_OUTPUT_DIR) / ref)
        hits = sorted(_glob.glob(base, recursive=True))
        if not hits:
            raise ValueError("tidak ada file yang cocok dengan pola itu")
        files = []
        for hit in hits:
            hp = _Path(hit)
            if hp.is_dir():
                files.extend(sorted(x for x in hp.rglob("*") if x.is_file()))
            else:
                files.append(hp)
        return files
    return [_deliver_safe_path(ref)]


_DELIVER_PATH_MENTION = re.compile(
    r"(?<![\w/.])(" + re.escape(str(_DELIVER_WORK_ROOT)) + r"/[^\s`'\"<>\[\]()|*]+)"
)


def _mentioned_output_files(text):
    """Model sering hanya MENULIS path file (mis. saat user minta 'kirim file-nya') tanpa marker [[file:...]],
    padahal file itu sudah ada dari giliran sebelumnya. Path yang menunjuk file nyata di folder output
    tetap dikirim sebagai lampiran (semua jenis file)."""
    found, already = [], set(m.group(1).strip() for m in _DELIVER_MARKER.finditer(text or ""))
    out_root = _Path(_OUTPUT_DIR).resolve()
    for match in _DELIVER_PATH_MENTION.finditer(text or ""):
        raw = match.group(1).rstrip(".,;:!?")
        if raw in already or raw in found:
            continue
        try:
            path = _Path(raw).resolve()
            if out_root in path.parents and path.is_file():
                found.append(raw)
        except Exception:
            continue
        if len(found) >= _DELIVER_MAX_FILES:
            break
    return found


def extract_and_deliver(text, session_id=""):
    """Ambil semua marker [[file:...]] (juga path yang hanya disebut), kirim SEMUA sekaligus. Satu file maupun
    banyak file, jenis apa pun. Jika jumlahnya melebihi batas, digabung jadi satu zip."""
    text = text or ""
    extra = _mentioned_output_files(text)
    if extra:
        text = text + "\n" + "\n".join(f"[[file:{p}]]" for p in extra)
    notes, targets, seen_refs, seen_files = [], [], set(), set()
    for match in _DELIVER_MARKER.finditer(text):
        raw = match.group(1).strip()
        if raw in seen_refs:
            continue
        seen_refs.add(raw)
        try:
            for item in _expand_deliver_ref(raw):
                key = str(_Path(item).resolve())
                if key not in seen_files:
                    seen_files.add(key)
                    targets.append(_Path(item))
        except Exception as err:
            notes.append(f"Gagal mengirim `{os.path.basename(raw) or raw}`: {err}")
    cleaned = _DELIVER_MARKER.sub("", text).strip()

    attachments = []
    budget = {"inline": _DELIVER_INLINE_TOTAL}
    if len(targets) > _DELIVER_MAX_FILES:
        try:
            bundle = _deliver_zip_files(targets, f"files_{uuid.uuid4().hex[:6]}.zip")
            targets = [bundle]
            notes.append(f"Ada {len(seen_files)} file, jadi dikemas dalam satu zip.")
        except Exception as err:
            notes.append(f"Gagal mengemas banyak file jadi zip: {err}")
            targets = targets[:_DELIVER_MAX_FILES]
    for item in targets:
        try:
            attachments.append(deliver_file(item, session_id, budget))
        except Exception as err:
            notes.append(f"Gagal mengirim `{item.name}`: {err}")
    if notes:
        cleaned = f"{cleaned}\n\n" + "\n".join(notes)
    return cleaned, attachments


_DELIVER_SYSTEM_HINT = (
    "Jika kamu membuat file untuk pengguna, SELALU tulis file itu ke disk dengan tool write_file "
    "di /kaggle/working/out/ lalu tulis [[file:/kaggle/working/out/NAMA_FILE]] di jawabanmu — "
    "ini satu-satunya cara mengirim file ke pengguna. Semua jenis file boleh (gambar, video, audio, dokumen, "
    "arsip, kode, data, apa pun). Kirim BANYAK file sekaligus dengan menulis satu marker per file, atau pola "
    "seperti [[file:/kaggle/working/out/*.png]]. Folder juga boleh (akan otomatis di-zip). "
    "Jika pengguna meminta file yang SUDAH ada dari sebelumnya ('kirim file-nya', 'download'), jangan hanya "
    "menyebut path: cukup tulis marker [[file:PATH_LENGKAP]] di jawabanmu dan file otomatis terkirim. "
    "Jangan menempelkan isi file besar ke chat sebagai gantinya.\n"
    "ATURAN KERAS SOAL PENGIRIMAN FILE:\n"
    "- Pengguna TIDAK bisa membuka Kaggle sendiri dan TIDAK punya akses ke notebook/kernel ini. "
    "Jangan PERNAH menyuruh pengguna 'download dari Kaggle', 'cek output Kaggle', 'buka notebook', "
    "atau instruksi sejenis — itu mustahil dilakukan pengguna dan merupakan kegagalan tugas.\n"
    "- Jangan berhenti di tengah pembuatan kode dengan alasan 'lanjutkan sendiri' atau 'sisanya bisa "
    "kamu generate'. Jika tugas berisi banyak file, kerjakan SATU PER SATU dengan write_file sampai "
    "semua selesai sebelum menjawab, baru sertakan marker [[file:...]] untuk tiap file (atau satu "
    "marker folder untuk semuanya sekaligus).\n"
    "- Kode yang kamu tulis lewat write_file harus lengkap dan bisa langsung dipakai, bukan potongan "
    "atau placeholder seperti '# TODO: lengkapi sisanya'.\n"
    "- Jika proyek benar-benar terlalu besar untuk diselesaikan dalam satu giliran, kirim dulu file-file "
    "yang sudah selesai lewat marker [[file:...]], lalu katakan dengan jelas file/bagian apa yang masih "
    "akan menyusul di balasan berikutnya — jangan menyuruh pengguna menyelesaikannya sendiri.\n"
    "Jika user meminta struktur folder/file tree/arsip/daftar lengkap, SELALU berikan struktur "
    "LENGKAP tanpa memotong, tanpa ellipsis '...', dan tanpa meringkas sisa item. "
    "Jangan malas: jangan berhenti di tengah. Prefer kelengkapan daripada singkat. "
    "Jika output terlalu panjang dan terpaksa dipotong, sebutkan titik berhenti lalu lanjutkan "
    "dari titik itu di jawaban berikutnya."
)


# ---------------------------------------------------------------------------
# Tool schema exposed to the local model (same shape as codekaggle.py so the
# gguf model's tool-calling behaviour doesn't need to be retrained/adjusted).
# ---------------------------------------------------------------------------
TOOLS = [
    {"type": "function", "function": {
        "name": "list_dir",
        "description": "Melihat daftar folder, file, dan ukuran/baris di direktori proyek (default workspace: /kaggle/working).",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Path direktori target, misal /kaggle/working atau /"}
        }, "required": ["path"]},
    }},
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Membaca isi file dengan nomor baris (hingga 7.500 baris sekaligus).",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "start_line": {"type": "integer"},
            "end_line": {"type": "integer"},
        }, "required": ["path"]},
    }},
    {"type": "function", "function": {
        "name": "patch_file",
        "description": "Mengganti baris tertentu pada file besar tanpa menulis ulang seluruh file (otomatis backup .bak).",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "start_line": {"type": "integer"},
            "end_line": {"type": "integer"},
            "new_code": {"type": "string"},
        }, "required": ["path", "start_line", "end_line", "new_code"]},
    }},
    {"type": "function", "function": {
        "name": "write_file",
        "description": "Membuat file baru atau menimpa isi file kecil/menengah dengan konten lengkap.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        }, "required": ["path", "content"]},
    }},
    {"type": "function", "function": {
        "name": "run_bash",
        "description": "Menjalankan perintah shell non-interaktif di kernel Kaggle ini.",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string"},
            "cwd": {"type": "string"},
        }, "required": ["command"]},
    }},
    {"type": "function", "function": {
        "name": "web_search",
        "description": "Mencari informasi terkini di internet.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"}
        }, "required": ["query"]},
    }},
    {"type": "function", "function": {
        "name": "web_fetch",
        "description": "Mengambil isi lengkap halaman web dari URL spesifik.",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string"}
        }, "required": ["url"]},
    }},
    {"type": "function", "function": {
        "name": "inspect_image",
        "description": "Memuat file gambar dari disk agar dapat dianalisis secara visual.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "question": {"type": "string"},
        }, "required": ["path"]},
    }},
    {"type": "function", "function": {
        "name": "read_document",
        "description": (
            "Membaca file di disk Kaggle tanpa menempel seluruh isi ke konteks: "
            "PDF (teks, atau OCR jika scan), DOCX, PPTX, XLSX/CSV (pandas bawaan), "
            "gambar (OCR), audio/video (ffmpeg + faster-whisper), dan arsip zip/tar/gz. "
            "Pakai ini untuk file di /kaggle/working/uploads. Jangan pakai read_file untuk PDF/biner."
        ),
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Path absolut file, misal /kaggle/working/uploads/nama.pdf"},
            "max_chars": {"type": "integer", "description": "Batas karakter hasil, default 12000"},
        }, "required": ["path"]},
    }},
]


def _clean_err(e):
    return re.sub(r"\x1b\[[0-9;]*m", "", str(e)).strip()


def encode_image_b64(img_path):
    ext = os.path.splitext(img_path)[1].lower().lstrip(".") or "png"
    if ext == "jpg":
        ext = "jpeg"
    with open(img_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    return f"data:image/{ext};base64,{b64}"


def _is_blocked_html(text):
    low = text.lower()
    markers = ["anomaly-modal", "challenge", "captcha", "bot detection", "unusual traffic", "ddg-captcha"]
    return any(m in low for m in markers) and ("result-link" not in low and "b_algo" not in low)


def _search_ddg(query, max_results=6):
    encoded_q = urllib.parse.urlencode({"q": query})
    req = urllib.request.Request(f"https://lite.duckduckgo.com/lite/?{encoded_q}", headers=BROWSER_HEADERS)
    with urllib.request.urlopen(req, timeout=15) as r:
        raw = html.unescape(r.read().decode("utf-8", errors="ignore"))
    if _is_blocked_html(raw):
        return []
    snippets = re.findall(r"<td class=['\"]result-snippet['\"]>(.*?)</td>", raw, flags=re.DOTALL | re.IGNORECASE)
    links = re.findall(r"<a[^>]+class=['\"]result-link['\"][^>]*href=['\"]([^'\"]+)['\"][^>]*>(.*?)</a>", raw, flags=re.DOTALL | re.IGNORECASE)
    out = []
    for idx in range(min(max_results, max(len(links), len(snippets)))):
        title = re.sub(r"<[^>]+>", "", links[idx][1]).strip() if idx < len(links) else ""
        href = links[idx][0].strip() if idx < len(links) else ""
        snip = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", snippets[idx]).strip()) if idx < len(snippets) else ""
        if title or snip:
            out.append(f"- {title} ({href}): {snip}")
    return out


def perform_web_search(query, max_results=6):
    try:
        items = _search_ddg(query, max_results=max_results)
    except Exception as e:
        items = []
    if not items:
        return "[PENCARIAN WEB TIDAK MENGHASILKAN DATA VALID]"
    return "\n".join(items[:max_results])


# Paket dari dataset media-offline-pkgs. Dipasang ke folder terpisah dengan
# --no-deps supaya numpy/pillow bawaan Kaggle tidak tertimpa. Yang tidak ada
# di dataset (pandas, openpyxl, tesseract) memakai yang sudah terpasang di image,
# atau apt hanya jika biner itu memang belum ada.
_DOC_TARGET = "/tmp/docpkgs"
_DOC_WHEEL_GLOBS = (
    "pymupdf-*.whl", "pymupdf4llm-*.whl", "pymupdf_layout-*.whl",
    "python_docx-*.whl", "python_pptx-*.whl", "lxml-*.whl", "xlsxwriter-*.whl",
    "faster_whisper-*.whl", "ctranslate2-*.whl", "av-*.whl",
    "onnxruntime-*.whl", "tokenizers-*.whl",
)
_WHISPER_MODEL = None
_DOC_TEXT_LIMIT = 12000


def _wheel_matches_python(filename):
    low = filename.lower()
    if "py3-none-any" in low or "py2.py3-none-any" in low:
        return True
    current = sys.version_info.major * 100 + sys.version_info.minor
    for match in re.finditer(r"cp(\d+)", low):
        tag = int(match.group(1))
        if "abi3" in low and tag <= current:
            return True
        if tag == current:
            return True
    return False


def _run_quiet(cmd, timeout=180):
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout)


def _prepare_media_packages():
    """Pasang wheel offline seperti worker downloader, lalu lengkapi biner yang belum ada."""
    os.makedirs(BASE_DIR, exist_ok=True)
    os.makedirs(os.path.join(BASE_DIR, "uploads"), exist_ok=True)
    wheels = next(iter(glob.glob("/kaggle/input/**/wheels", recursive=True)), None)
    installed = []
    if wheels:
        os.makedirs(_DOC_TARGET, exist_ok=True)
        files = []
        for pattern in _DOC_WHEEL_GLOBS:
            for path in glob.glob(os.path.join(wheels, pattern)):
                if _wheel_matches_python(os.path.basename(path)):
                    files.append(path)
        if files:
            result = _run_quiet([
                sys.executable, "-m", "pip", "install", "-q", "--no-index", "--no-deps",
                "--target", _DOC_TARGET, *files,
            ], timeout=300)
            if result.returncode != 0:
                print(f"[setup] pip offline gagal: {(result.stdout or '')[-400:]}", flush=True)
            else:
                installed = [os.path.basename(path) for path in files]
        deb_dir = os.path.join(os.path.dirname(wheels), "deb")
        if not shutil.which("ffmpeg"):
            ffdebs = sorted(glob.glob(os.path.join(deb_dir, "ffmpeg*.deb")))
            if ffdebs:
                _run_quiet(["dpkg", "-i", *ffdebs], timeout=180)
    if _DOC_TARGET not in sys.path:
        sys.path.insert(0, _DOC_TARGET)
    os.environ["PYTHONPATH"] = _DOC_TARGET + os.pathsep + os.environ.get("PYTHONPATH", "")
    importlib.invalidate_caches()
    missing_apt = []
    if not shutil.which("ffmpeg"):
        missing_apt.append("ffmpeg")
    if not shutil.which("tesseract"):
        missing_apt.extend(["tesseract-ocr", "tesseract-ocr-eng", "tesseract-ocr-ind"])
    if missing_apt:
        print(f"[setup] tidak ada di dataset, memasang dari image Kaggle: {', '.join(missing_apt)}", flush=True)
        _run_quiet(["apt-get", "update", "-qq"], timeout=180)
        _run_quiet(["apt-get", "install", "-y", "-qq", *missing_apt], timeout=300)
    ready = []
    for module_name, label in (
        ("fitz", "pymupdf"), ("docx", "python-docx"), ("pptx", "python-pptx"),
        ("faster_whisper", "faster-whisper"), ("pandas", "pandas"),
    ):
        if importlib.util.find_spec(module_name) is not None:
            ready.append(label)
    print(
        f"[setup] media packages offline={len(installed)} siap={','.join(ready) or '-'} "
        f"ffmpeg={bool(shutil.which('ffmpeg'))} tesseract={bool(shutil.which('tesseract'))}",
        flush=True,
    )


def _clip_text(text, limit=_DOC_TEXT_LIMIT):
    text = (text or "").strip()
    if len(text) <= limit:
        return text or "(kosong)"
    return text[:limit] + f"\n\n... [dipotong, total {len(text)} karakter]"


def _ocr_image(path):
    if not shutil.which("tesseract"):
        return ""
    result = subprocess.run(
        ["tesseract", path, "stdout", "-l", "ind+eng"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=90,
    )
    return (result.stdout or "").strip()


def _read_pdf(path, limit):
    import fitz
    doc = fitz.open(path)
    parts = []
    ocr_budget = 6
    for index, page in enumerate(doc):
        if index >= 40:
            parts.append(f"... [{doc.page_count} halaman, hanya 40 pertama]")
            break
        text = page.get_text("text").strip()
        if len(text) < 40 and ocr_budget > 0 and shutil.which("tesseract"):
            image_path = f"/tmp/ocr_page_{index}.png"
            page.get_pixmap(matrix=fitz.Matrix(2, 2)).save(image_path)
            scanned = _ocr_image(image_path)
            if scanned:
                text = "[OCR]\n" + scanned
                ocr_budget -= 1
        parts.append(f"--- halaman {index + 1} ---\n{text}")
    return _clip_text("\n\n".join(parts), limit)


def _read_docx(path, limit):
    import docx
    document = docx.Document(path)
    chunks = [paragraph.text for paragraph in document.paragraphs if paragraph.text.strip()]
    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells]
            if any(cells):
                chunks.append(" | ".join(cells))
    return _clip_text("\n".join(chunks), limit)


def _read_pptx(path, limit):
    from pptx import Presentation
    presentation = Presentation(path)
    parts = []
    for index, slide in enumerate(presentation.slides, 1):
        texts = []
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False) and shape.text_frame.text.strip():
                texts.append(shape.text_frame.text.strip())
        parts.append(f"--- slide {index} ---\n" + ("\n".join(texts) or "(tanpa teks)"))
        if index >= 60:
            parts.append("... [slide selebihnya dipotong]")
            break
    return _clip_text("\n\n".join(parts), limit)


def _read_table(path, limit):
    ext = os.path.splitext(path)[1].lower()
    try:
        import pandas as pd
    except Exception as exc:
        return f"Error: pandas bawaan Kaggle tidak bisa diimpor ({exc})."
    if ext == ".csv" or ext == ".tsv":
        frame = pd.read_csv(path, sep="\t" if ext == ".tsv" else ",", nrows=200)
        return _clip_text(f"CSV preview {frame.shape[0]} baris x {frame.shape[1]} kolom\n{frame.to_csv(index=False)}", limit)
    book = pd.ExcelFile(path)
    parts = []
    for name in book.sheet_names[:8]:
        frame = book.parse(name, nrows=200)
        parts.append(f"Sheet: {name} ({frame.shape[0]} x {frame.shape[1]})\n{frame.to_csv(index=False)}")
    return _clip_text("\n\n".join(parts), limit)


def _reject_unsafe_archive_name(name):
    pure = os.path.normpath(name)
    if pure.startswith("/") or pure.startswith("..") or "/../" in f"/{pure}" or pure == "..":
        raise ValueError(f"arsip berisi path tidak aman: {name}")


def _extract_archive(path):
    base = os.path.splitext(os.path.basename(path))[0][:80] or "arsip"
    dest = os.path.join(BASE_DIR, "uploads", "_extracted", base)
    os.makedirs(dest, exist_ok=True)
    lower = path.lower()
    names = []
    if lower.endswith(".zip"):
        import zipfile
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > 500:
                return "Error: arsip berisi lebih dari 500 entri."
            total = 0
            for info in infos:
                _reject_unsafe_archive_name(info.filename)
                total += int(info.file_size or 0)
            if total > 200 * 1024 * 1024:
                return "Error: isi arsip lebih dari 200 MB."
            archive.extractall(dest)
            names = [info.filename for info in infos]
    elif lower.endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2")):
        import tarfile
        with tarfile.open(path) as archive:
            members = archive.getmembers()
            if len(members) > 500:
                return "Error: arsip berisi lebih dari 500 entri."
            total = 0
            for member in members:
                _reject_unsafe_archive_name(member.name)
                total += int(member.size or 0)
            if total > 200 * 1024 * 1024:
                return "Error: isi arsip lebih dari 200 MB."
            archive.extractall(dest)
            names = [member.name for member in members]
    else:
        return "Error: format arsip ini tidak didukung. Pakai zip, tar, atau tar.gz."
    preview = "\n".join(names[:80])
    extra = "" if len(names) <= 80 else f"\n... [{len(names) - 80} entri lagi]"
    return f"Diekstrak ke {dest} ({len(names)} entri)\n{preview}{extra}"


def _transcribe_audio(path):
    global _WHISPER_MODEL
    try:
        from faster_whisper import WhisperModel
    except Exception as exc:
        return f"Error: faster-whisper tidak siap ({exc}). Paket ada di dataset, tapi import gagal."
    if _WHISPER_MODEL is None:
        # CPU supaya tidak berebut VRAM dengan llama-server.
        _WHISPER_MODEL = WhisperModel("small", device="cpu", compute_type="int8")
    segments, _info = _WHISPER_MODEL.transcribe(path, vad_filter=True)
    text = " ".join(segment.text.strip() for segment in segments).strip()
    return text or "(tidak ada ucapan yang terdengar)"


def _read_media(path, limit):
    ext = os.path.splitext(path)[1].lower()
    audio_path = path
    notes = []
    if ext in {".mp4", ".mkv", ".webm", ".mov", ".avi"}:
        if not shutil.which("ffmpeg"):
            return "Error: ffmpeg tidak tersedia untuk memisahkan audio video."
        audio_path = "/tmp/media_audio.wav"
        frame_path = "/tmp/media_frame.jpg"
        subprocess.run(
            ["ffmpeg", "-y", "-i", path, "-vn", "-ac", "1", "-ar", "16000", audio_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180, check=False,
        )
        subprocess.run(
            ["ffmpeg", "-y", "-ss", "1", "-i", path, "-frames:v", "1", frame_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60, check=False,
        )
        if os.path.isfile(frame_path):
            scanned = _ocr_image(frame_path)
            if scanned:
                notes.append("Teks pada satu frame:\n" + scanned)
    transcript = _transcribe_audio(audio_path)
    body = "Transkrip:\n" + transcript
    if notes:
        body += "\n\n" + "\n\n".join(notes)
    return _clip_text(body, limit)


def read_document(path, max_chars=_DOC_TEXT_LIMIT):
    if not path or not os.path.isfile(path):
        return f"Error: File '{path}' tidak ditemukan."
    if os.path.getsize(path) > 200 * 1024 * 1024:
        return f"Error: File '{path}' terlalu besar (>200 MB)."
    limit = max(1000, min(int(max_chars or _DOC_TEXT_LIMIT), 20000))
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".pdf":
            return _read_pdf(path, limit)
        if ext == ".docx":
            return _read_docx(path, limit)
        if ext in {".doc", ".ppt"}:
            return "Format Office lama ini tidak ada di paket offline. Simpan sebagai DOCX, PPTX, atau PDF."
        if ext == ".pptx":
            return _read_pptx(path, limit)
        if ext in {".xlsx", ".xlsm", ".xls", ".csv", ".tsv"}:
            return _read_table(path, limit)
        if ext == ".parquet":
            import pandas as pd
            frame = pd.read_parquet(path)
            return _clip_text(f"Parquet {frame.shape}\n{frame.head(200).to_csv(index=False)}", limit)
        if ext in {".sqlite", ".db"}:
            import sqlite3
            connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
                parts = [f"Tabel: {', '.join(tables) or '(tidak ada)'}"]
                for table in tables[:8]:
                    safe_table = str(table).replace('"', '""')
                    rows = connection.execute(f'SELECT * FROM "{safe_table}" LIMIT 50').fetchall()
                    parts.append(f"--- {table} ({len(rows)} baris) ---\n{rows}")
            finally:
                connection.close()
            return _clip_text("\n\n".join(parts), limit)
        if ext in {".zip", ".tar", ".tgz", ".gz"} or path.lower().endswith(".tar.gz"):
            return _extract_archive(path)
        if ext in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}:
            scanned = _ocr_image(path)
            if not scanned:
                return "OCR tidak menghasilkan teks. Untuk isi visual, pakai inspect_image."
            return _clip_text(scanned, limit)
        if ext in {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac", ".mp4", ".mkv", ".webm", ".mov", ".avi"}:
            return _read_media(path, limit)
        if ext == ".ipynb":
            notebook = json.load(open(path, "r", encoding="utf-8"))
            cells = notebook.get("cells") if isinstance(notebook, dict) else None
            if not isinstance(cells, list):
                return "Error: notebook tidak punya cells."
            chunks = []
            for index, cell in enumerate(cells, 1):
                source = cell.get("source") if isinstance(cell, dict) else ""
                text = "".join(source) if isinstance(source, list) else str(source or "")
                kind = cell.get("cell_type") if isinstance(cell, dict) else ""
                chunks.append(f"Cell {index} ({kind})\n{text}")
            return _clip_text("\n\n".join(chunks), limit)
        return (
            "Format ini tidak punya pembaca khusus. "
            "CSV/JSON/teks bisa dibaca read_file atau pandas lewat run_bash. "
            f"Ukuran file: {os.path.getsize(path)} byte."
        )
    except Exception as exc:
        return f"Error membaca {path}: {exc}"


def exec_tool(name, args, base_dir=BASE_DIR):
    try:
        if name == "list_dir":
            target = args.get("path", base_dir)
            if not os.path.isdir(target):
                return f"Error: Direktori '{target}' tidak ditemukan."
            out = [f"STRUKTUR DIREKTORI: {target}"]
            max_depth = 1 if os.path.abspath(target) == "/" else 3
            for root, dirs, files in os.walk(target):
                rel = os.path.relpath(root, target)
                depth = 0 if rel == "." else rel.count(os.sep) + 1
                if depth >= max_depth:
                    dirs[:] = []
                else:
                    dirs[:] = sorted([d for d in dirs if d not in PROJECT_IGNORE_DIRS and (depth == 0 or d not in SYS_SHALLOW_DIRS)])
                indent = " " * depth
                if rel != ".":
                    out.append(f"{' ' * (depth - 1)}[dir] {os.path.basename(root)}/")
                for fn in sorted(files)[:40]:
                    fp = os.path.join(root, fn)
                    try:
                        fsize = os.path.getsize(fp)
                        out.append(f"{indent}[file] {fn} ({fsize} B)")
                    except Exception:
                        out.append(f"{indent}[file] {fn}")
                if len(out) > 120:
                    out.append("... [dipotong]")
                    break
            return "\n".join(out)

        if name == "read_file":
            fp = args["path"]
            if not os.path.exists(fp):
                return f"Error: File '{fp}' tidak ditemukan."
            if os.path.getsize(fp) > 5 * 1024 * 1024:
                return f"Error: File '{fp}' terlalu besar (>5 MB)."
            s = max(1, int(args.get("start_line", 1)))
            e = int(args.get("end_line", 7500))
            with open(fp, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
            total = len(lines)
            e = min(total, e, s + 7499)
            sliced = "".join(f"{i}: {lines[i - 1]}" for i in range(s, e + 1))
            return f"FILE: {fp} (baris {s}-{e} dari {total})\n\n{sliced}"

        if name == "patch_file":
            fp = args["path"]
            if not os.path.exists(fp):
                return f"Error: File '{fp}' tidak ditemukan."
            s, e, new_code = int(args["start_line"]), int(args["end_line"]), args["new_code"]
            if new_code and not new_code.endswith("\n"):
                new_code += "\n"
            shutil.copy2(fp, fp + ".bak")
            with open(fp, "r", encoding="utf-8") as f:
                lines = f.readlines()
            if s < 1 or e > len(lines) or s > e:
                return f"Error: Rentang baris {s}-{e} tidak valid ({len(lines)} baris)."
            lines[s - 1:e] = new_code.splitlines(keepends=True)
            with open(fp, "w", encoding="utf-8") as f:
                f.writelines(lines)
            return f"Sukses patch {fp} baris {s}-{e}. Total baris: {len(lines)} (backup {fp}.bak)."

        if name == "write_file":
            fp = args["path"]
            os.makedirs(os.path.dirname(os.path.abspath(fp)), exist_ok=True)
            with open(fp, "w", encoding="utf-8") as f:
                f.write(args["content"])
            return f"Sukses menulis file {fp} ({args['content'].count(chr(10)) + 1} baris)."

        if name == "run_bash":
            cwd = args.get("cwd", base_dir)
            if not os.path.isdir(cwd):
                cwd = base_dir
            bash_env = os.environ.copy()
            bash_env["CI"] = "true"
            bash_env["DEBIAN_FRONTEND"] = "noninteractive"
            res = subprocess.run(args["command"], shell=True, cwd=cwd, env=bash_env,
                                  stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=180)
            output = (res.stdout + "\n" + res.stderr).strip()
            if len(output) > 8000:
                output = output[:3500] + "\n\n... [DIPOTONG] ...\n\n" + output[-3500:]
            return f"EXIT CODE: {res.returncode}\nOUTPUT:\n{output or '(kosong)'}"

        if name == "web_search":
            q = args["query"]
            return f"HASIL PENCARIAN UNTUK '{q}':\n\n{perform_web_search(q)}"

        if name == "web_fetch":
            req = urllib.request.Request(args["url"], headers=BROWSER_HEADERS)
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = html.unescape(r.read().decode("utf-8", errors="ignore"))
            clean = re.sub(r"<(script|style).*?>.*?</\1>", "", raw, flags=re.DOTALL | re.IGNORECASE)
            clean = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", clean)).strip()
            return f"URL: {args['url']}\nKONTEN:\n{clean[:7000]}"

        if name == "inspect_image":
            fp = args["path"]
            if not os.path.exists(fp):
                return f"Error: Gambar '{fp}' tidak ditemukan."
            return {"__image_payload__": fp, "question": args.get("question", "Analisis gambar ini.")}

        if name == "read_document":
            return read_document(args.get("path") or "", args.get("max_chars") or _DOC_TEXT_LIMIT)

        return f"Error: Tool '{name}' tidak dikenal."
    except Exception as ex:
        return f"Error eksekusi {name}: {ex}"


def build_system_prompt(base_dir):
    return (
        "Kamu adalah Autonomous AI Partner profesional yang berjalan lokal (GPU Kaggle). "
        "Kamu bisa membaca/menulis file, menjalankan shell, mencari web, dan menganalisis gambar.\n"
        f"WORKSPACE UTAMA: `{base_dir}` (dan `/kaggle/input` untuk data read-only).\n"
        f"FILE UPLOAD PENGGUNA: `{base_dir}/uploads`. Untuk PDF, DOCX, PPTX, XLSX, CSV, gambar berisi teks, "
        "audio, video, dan arsip zip/tar gunakan tool read_document. Jangan memakai read_file pada file biner.\n"
        "TOOLS: list_dir, read_file, patch_file, write_file, run_bash, web_search, web_fetch, inspect_image, read_document.\n"
        "Cara kerja profesional:\n"
        "- Sebelum mengerjakan tugas besar, pahami dulu konteks: cek folder, baca file relevan, atau tanya user "
        "jika informasi penting kurang (nama proyek, API key, target deploy, kredensial, dsb).\n"
        "- Jangan mengarang. Jika butuh token/API key/secret, minta user menuliskannya (atau pastikan sudah ada di env/secret) "
        "sebelum menjalankan langkah yang bergantung padanya.\n"
        "- Kerjakan langkah demi langkah: investigasi → rencana singkat → eksekusi → verifikasi hasil.\n"
        "- Laporkan hasil yang konkret (path file, perintah yang dijalankan, error asli) tanpa basa-basi berlebih.\n"
        "- Jika diminta struktur lengkap / tree / daftar file, keluarkan SELURUH hasil tanpa '...' atau potongan di tengah.\n"
        "Paket dokumen (pymupdf, python-docx, python-pptx, faster-whisper) dipasang offline dari dataset "
        "media-offline-pkgs ke /tmp/docpkgs tanpa menimpa numpy atau pillow bawaan Kaggle. "
        "pandas dan openpyxl memakai paket yang sudah ada di image Kaggle. ffmpeg dan tesseract dipakai jika sudah ada, "
        "dan baru dipasang lewat apt bila belum ada.\n"
        "Jujur jika sebuah tool gagal atau data tidak ditemukan.\n"
        "MEMBUAT FILE: pilih cara paling sederhana yang pasti jalan. Teks/laporan/tabel sederhana: tulis langsung "
        ".txt/.md/.html/.csv dengan write_file. PDF: pakai pymupdf (`import fitz`) yang sudah terpasang; JANGAN pip install "
        "(internet bisa mati). Cek sekali dengan run_bash `python -c \"import fitz\"`; jika gagal, pakai format lain "
        "dan beri tahu pengguna.\n"
        "ANTI-LOOP: jangan mengulang tool call yang sama persis. Jika gagal dua kali, ganti pendekatan atau laporkan "
        "error aslinya sebagai jawaban akhir. Begitu file selesai ditulis, langsung beri jawaban akhir dengan marker "
        "[[file:...]] — jangan memeriksa berulang-ulang.\n"
        + _DELIVER_SYSTEM_HINT
    )


# ---------------------------------------------------------------------------
# Staging: copy the dataset's llama.cpp binaries + gguf model onto local disk
# so they are executable (the /kaggle/input mount is read-only).
# ---------------------------------------------------------------------------
def _stage_model_assets(base_dir):
    bin_dst = os.path.join(base_dir, "llama_bin")
    model_dst = os.path.join(base_dir, "model_gguf")
    if not os.path.isdir(bin_dst):
        src_bin = next(iter(glob.glob("/kaggle/input/**/llama_bin", recursive=True)), None)
        if not src_bin:
            raise FileNotFoundError("The dataset has no llama_bin folder")
        shutil.copytree(src_bin, bin_dst)
        for root, _, files in os.walk(bin_dst):
            for fn in files:
                try:
                    os.chmod(os.path.join(root, fn), 0o755)
                except Exception:
                    pass
    if not os.path.isdir(model_dst):
        src_model = next(iter(glob.glob("/kaggle/input/**/model_gguf", recursive=True)), None)
        if not src_model:
            raise FileNotFoundError("The dataset has no model_gguf folder")
        shutil.copytree(src_model, model_dst)
    return bin_dst, model_dst


def start_llama_server(base_dir, port, max_ctx):
    _stage_model_assets(base_dir)
    srv_matches = glob.glob(f"{base_dir}/llama_bin/**/llama-server", recursive=True)
    if not srv_matches:
        raise FileNotFoundError(f"Binary llama-server tidak ditemukan di {base_dir}/llama_bin")
    srv = srv_matches[0]
    d = os.path.dirname(srv)
    model_matches = sorted(glob.glob(f"{base_dir}/model_gguf/*.gguf"))
    model_matches_q4 = [m for m in model_matches if "mmproj" not in m.lower()]
    mmproj_matches = [m for m in model_matches if "mmproj" in m.lower()]
    if not model_matches_q4:
        raise FileNotFoundError(f"File model GGUF tidak ditemukan di {base_dir}/model_gguf")

    env = os.environ.copy()
    env["CUDA_VISIBLE_DEVICES"] = "0,1"
    env["LD_LIBRARY_PATH"] = d + ":" + env.get("LD_LIBRARY_PATH", "")
    subprocess.run(["pkill", "-x", "llama-server"], stderr=subprocess.DEVNULL)
    time.sleep(1)

    cmd = [srv, "-m", model_matches_q4[0], "-ngl", "99", "-c", str(max_ctx), "-np", "1",
           "--jinja", "--host", "127.0.0.1", "--port", str(port), "-fa", "on",
           "-b", "2048", "-ub", "512", "-sm", "layer",
           "--cache-type-k", "q8_0", "--cache-type-v", "q8_0"]
    if mmproj_matches:
        cmd[3:3] = ["--mmproj", mmproj_matches[0]]

    log_file = open("/tmp/server.log", "w")
    proc = subprocess.Popen(cmd, env=env, stdout=log_file, stderr=subprocess.STDOUT)
    model_id = os.path.basename(model_matches_q4[0])
    return proc, log_file, model_id


def wait_ready(proc, port, secs=300):
    health_url = f"http://127.0.0.1:{port}/health"
    for _ in range(secs // 2):
        if proc.poll() is not None:
            return False
        try:
            if urllib.request.urlopen(health_url, timeout=2).status == 200:
                return True
        except Exception:
            time.sleep(2)
    return False


_TOOL_ACTIVITY = {
    "list_dir": "Memeriksa isi folder…",
    "read_file": "Membaca file…",
    "patch_file": "Mengedit file…",
    "write_file": "Menulis file…",
    "run_bash": "Menjalankan perintah…",
    "web_search": "Mencari di web…",
    "web_fetch": "Mengambil halaman web…",
    "inspect_image": "Menganalisis gambar…",
    "read_document": "Membaca dokumen…",
}


AGENT_MAX_STEPS = int(os.getenv("WARTHA_AGENT_MAX_STEPS", "30"))
_REPEAT_LIMIT = 2        # panggilan tool identik ke-3 dan seterusnya ditolak
_FAIL_STREAK_LIMIT = 6   # tool gagal/ditolak berturut-turut sebelum dipaksa menjawab
_OUTPUT_DIR = os.path.join(BASE_DIR, "out")
_THINK_RE = re.compile(r"<think>(.*?)</think>", re.DOTALL | re.IGNORECASE)


def _short(text, limit=600):
    text = str(text if text is not None else "").strip()
    return text if len(text) <= limit else text[:limit] + f"… [+{len(text) - limit} karakter]"


def _split_thinking(message):
    """Pisahkan teks berpikir dari teks yang layak tampil sebagai jawaban."""
    content = message.get("content") or ""
    thought = str(message.get("reasoning_content") or "").strip()
    found = _THINK_RE.findall(content)
    if found:
        thought = (thought + "\n" + "\n".join(found)).strip()
        content = _THINK_RE.sub("", content)
    elif re.search(r"<think>", content, re.IGNORECASE):
        head, tail = re.split(r"<think>", content, maxsplit=1, flags=re.IGNORECASE)
        thought = (thought + "\n" + tail).strip()
        content = head
    return content.strip(), thought


def _describe_tool(fn_name, args):
    label = _TOOL_ACTIVITY.get(fn_name, f"Menjalankan {fn_name}…")
    detail = ""
    if fn_name in ("list_dir", "read_file", "write_file", "patch_file", "read_document", "inspect_image"):
        p = str(args.get("path") or "")
        if p:
            detail = f" ({p.rsplit('/', 1)[-1] or p})"
    elif fn_name == "run_bash":
        full = str(args.get("command") or "")
        if full:
            detail = f" ({full[:60]}…)" if len(full) > 60 else f" ({full})"
    elif fn_name == "web_search":
        q = str(args.get("query") or "")[:60]
        if q:
            detail = f" “{q}”"
    elif fn_name == "web_fetch":
        u = str(args.get("url") or "")[:70]
        if u:
            detail = f" ({u})"
    return label, detail


def _snapshot_out():
    snap = {}
    try:
        for root, dirs, files in os.walk(_OUTPUT_DIR):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for fn in files:
                if fn.startswith(".") or fn.endswith(".bak"):
                    continue
                fp = os.path.join(root, fn)
                try:
                    snap[fp] = os.path.getmtime(fp)
                except OSError:
                    pass
    except Exception:
        pass
    return snap


def _append_missing_markers(answer, before):
    """File baru/berubah di /kaggle/working/out yang lupa diberi marker tetap dikirim ke chat."""
    answer = answer or ""
    after = _snapshot_out()
    changed = [p for p, mtime in after.items() if before.get(p) != mtime]
    missing = [p for p in changed if f"[[file:{p}]]" not in answer]
    if not missing:
        return answer
    if len(missing) > _DELIVER_MAX_FILES:
        return (answer + f"\n\n[[file:{_OUTPUT_DIR}]]").strip()
    return (answer + "\n\n" + "\n".join(f"[[file:{p}]]" for p in sorted(missing))).strip()


def _llm_call(api_url, history, with_tools=True):
    payload = {"messages": history, "max_tokens": -1, "temperature": 0.25, "stream": False}
    if with_tools:
        payload["tools"] = TOOLS
        payload["tool_choice"] = "auto"
    req = urllib.request.Request(api_url, data=json.dumps(payload).encode(),
                                  headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as resp:
        body = json.loads(resp.read().decode("utf-8", errors="ignore"))
    return ((body.get("choices") or [{}])[0].get("message")) or {}


def _fallback_summary(ran_tools):
    if not ran_tools:
        return "Maaf, model tidak menghasilkan jawaban. Coba kirim ulang permintaannya dengan lebih spesifik."
    lines = ["Aku sudah menjalankan beberapa langkah tapi belum sampai ke hasil akhir. Langkah terakhir:"]
    for name, detail, ok, preview in ran_tools[-5:]:
        mark = "OK" if ok else "GAGAL"
        lines.append(f"- [{mark}] {name}{detail}: {_short(preview, 160)}")
    lines.append("Kirim pesan lagi (misalnya “lanjutkan”) atau minta format file yang lebih sederhana.")
    return "\n".join(lines)


def run_agent_turn(history, api_url, base_dir, user_content, max_steps=AGENT_MAX_STEPS, on_activity=None, on_step=None):
    def _act(msg):
        if on_activity:
            try:
                on_activity(msg)
            except Exception:
                pass

    counter = {"n": 0}

    def _new_id():
        counter["n"] += 1
        return f"s{counter['n']}"

    def _step(step_id, **fields):
        if on_step:
            try:
                on_step({"id": step_id, **fields})
            except Exception:
                pass

    out_before = _snapshot_out()
    history.append({"role": "user", "content": user_content})
    seen_calls = {}
    ran_tools = []        # (nama tool, detail, ok, preview)
    fail_streak = 0
    nudged = False
    empty_retry = False
    stalled = False

    for step_idx in range(max_steps):
        if max_steps - step_idx == 3 and not nudged:
            nudged = True
            history.append({"role": "user", "content": (
                "[SISTEM] Sisa 3 langkah tool. Berhenti menjelajah, selesaikan yang bisa diselesaikan, "
                "lalu beri jawaban akhir berisi hasil nyata dan marker [[file:...]] bila ada file.")})
        think_id = _new_id()
        _act("Sedang berpikir…")
        _step(think_id, kind="thinking", title="Sedang berpikir…", status="running")
        try:
            message = _llm_call(api_url, history, with_tools=True)
        except Exception as exc:
            _step(think_id, status="error", title="Model gagal menjawab", output=_short(_clean_err(exc)))
            raise
        visible, thought = _split_thinking(message)
        tool_calls = message.get("tool_calls") or []

        if not tool_calls:
            _step(think_id, status="done", title="Berpikir", output=_short(thought, 1200))
            if not visible and not empty_retry:
                empty_retry = True
                history.append({"role": "assistant", "content": ""})
                history.append({"role": "user", "content": "[SISTEM] Jawabanmu kosong. Tulis jawaban akhir sekarang."})
                continue
            history.append({"role": "assistant", "content": visible})
            _act("Menyusun jawaban…")
            return _append_missing_markers(visible or _fallback_summary(ran_tools), out_before)

        # Normalisasi tool_calls supaya riwayat selalu valid (id & arguments string).
        for tc in tool_calls:
            tc.setdefault("type", "function")
            tc["id"] = tc.get("id") or f"call_{uuid.uuid4().hex[:10]}"
            fn = tc.setdefault("function", {})
            if not isinstance(fn.get("arguments"), str):
                fn["arguments"] = json.dumps(fn.get("arguments") or {}, ensure_ascii=False)
        history.append({"role": "assistant", "content": visible, "tool_calls": tool_calls})
        _step(think_id, status="done", title="Berpikir", output=_short(thought or visible, 1200))

        pending_images = []
        for tc in tool_calls:
            fn_name = tc["function"].get("name") or "unknown"
            parse_error = None
            try:
                parsed_args = json.loads(tc["function"].get("arguments") or "{}")
            except json.JSONDecodeError as exc:
                parsed_args, parse_error = {}, str(exc)
            if not isinstance(parsed_args, dict):
                parsed_args, parse_error = {}, "argumen harus berupa objek JSON"

            label, detail = _describe_tool(fn_name, parsed_args)
            sid = _new_id()
            _act(label + detail)
            _step(sid, kind="tool", tool=fn_name, title=label.rstrip("…"), detail=detail.strip(), status="running")

            sig = fn_name + ":" + json.dumps(parsed_args, sort_keys=True, ensure_ascii=False)[:2000]
            seen_calls[sig] = seen_calls.get(sig, 0) + 1
            if parse_error:
                result = (f"Error: argumen tool '{fn_name}' bukan JSON valid ({parse_error}). "
                          "Kirim ulang dengan JSON valid. Untuk file panjang, tulis per bagian kecil.")
            elif seen_calls[sig] > _REPEAT_LIMIT:
                result = (f"Error: panggilan '{fn_name}' dengan argumen identik sudah dijalankan {seen_calls[sig] - 1} kali "
                          "dan hasilnya tidak akan berubah. JANGAN ulangi. Ganti pendekatan, atau beri jawaban akhir "
                          "yang jujur berisi hasil dan error aslinya.")
            else:
                result = exec_tool(fn_name, parsed_args, base_dir=base_dir)

            if isinstance(result, dict) and "__image_payload__" in result:
                img_fp, q_img = result["__image_payload__"], result["question"]
                text_result = f"Gambar '{img_fp}' dimuat untuk inspeksi visual."
                history.append({"role": "tool", "tool_call_id": tc["id"], "name": fn_name, "content": text_result})
                pending_images.append((img_fp, q_img))
                ok = True
            else:
                text_result = str(result)
                history.append({"role": "tool", "tool_call_id": tc["id"], "name": fn_name, "content": text_result})
                ok = not (text_result.startswith("Error") or re.match(r"EXIT CODE: (?!0\b)", text_result))

            _step(sid, status="done" if ok else "error", output=_short(text_result))
            ran_tools.append((fn_name, detail, ok, text_result))
            fail_streak = 0 if ok else fail_streak + 1

        for img_fp, q_img in pending_images:
            history.append({"role": "user", "content": [
                {"type": "text", "text": f"[Inspeksi Visual: {img_fp}] {q_img}"},
                {"type": "image_url", "image_url": {"url": encode_image_b64(img_fp)}},
            ]})

        if fail_streak >= _FAIL_STREAK_LIMIT:
            stalled = True
            break

    # Langkah habis / macet: minta jawaban akhir TANPA tool, jangan berhenti dengan pesan kosong.
    final_id = _new_id()
    _act("Menyusun jawaban akhir…")
    _step(final_id, kind="thinking", title="Menyusun jawaban akhir…", status="running")
    history.append({"role": "user", "content": (
        "[SISTEM] " + ("Pekerjaan macet karena tool gagal berulang. " if stalled else "Batas langkah tool tercapai. ") +
        "JANGAN panggil tool lagi. Tulis jawaban akhir sekarang dalam bahasa pengguna: apa yang sudah berhasil, "
        "apa yang gagal beserta error aslinya, dan saran langkah berikutnya. Jika ada file jadi di "
        "/kaggle/working/out, sertakan marker [[file:...]].")})
    try:
        final_visible, _ = _split_thinking(_llm_call(api_url, history, with_tools=False))
    except Exception:
        final_visible = ""
    if not final_visible:
        final_visible = _fallback_summary(ran_tools)
    history.append({"role": "assistant", "content": final_visible})
    _step(final_id, status="done", title="Menyusun jawaban akhir")
    return _append_missing_markers(final_visible, out_before)


def run_workspace_job(op, payload, base_dir):
    """Workspace ops so the Cloud model can also read/write /kaggle/working via this worker."""
    if op == "list":
        return {"ok": True, "result": exec_tool("list_dir", {"path": payload.get("path") or base_dir}, base_dir)}
    if op == "read":
        return {"ok": True, "result": exec_tool("read_file", {"path": payload["path"]}, base_dir)}
    if op == "write":
        b64 = payload.get("content_base64")
        if b64:
            fp = payload["path"]
            os.makedirs(os.path.dirname(os.path.abspath(fp)), exist_ok=True)
            with open(fp, "wb") as fh:
                fh.write(base64.b64decode(b64))
            return {"ok": True, "result": f"Sukses menulis file biner {fp} ({len(b64)} char base64)."}
        return {"ok": True, "result": exec_tool("write_file", {"path": payload["path"], "content": payload.get("content", "")}, base_dir)}
    if op == "move":
        src, dst = payload["source"], payload["destination"]
        os.makedirs(os.path.dirname(os.path.abspath(dst)) or ".", exist_ok=True)
        shutil.move(src, dst)
        return {"ok": True, "result": f"Dipindahkan {src} -> {dst}"}
    if op == "delete":
        fp = payload["path"]
        if os.path.isdir(fp):
            shutil.rmtree(fp, ignore_errors=True)
        elif os.path.exists(fp):
            os.remove(fp)
        return {"ok": True, "result": f"Dihapus {fp}"}
    return {"ok": False, "error": f"Operasi workspace '{op}' tidak dikenal"}


# ---------------------------------------------------------------------------
# Headless backend polling loop (this replaces codekaggle.py's input() loop)
# ---------------------------------------------------------------------------
def run_kaggle_worker(session_id=None, backend_url=None, worker_token=None, idle_timeout=None):
    if not IS_KAGGLE:
        raise RuntimeError("Worker ini hanya dapat dijalankan dari Kaggle.")
    print(f"[ai-local] worker version: {WORKER_VERSION}", flush=True)

    session_id = (session_id or os.environ.get("WORKER_SESSION_ID") or "").strip()
    backend_url = (backend_url or os.environ.get("WORKER_BACKEND_URL") or "").strip().rstrip("/")
    worker_token = (worker_token or os.environ.get("WORKER_TOKEN") or "").strip()
    if not session_id or not backend_url or not worker_token:
        raise RuntimeError("session_id, backend_url, dan worker_token wajib diisi")

    import requests

    gone = {"count": 0}  # berapa kali berturut-turut backend menjawab 410 (sesi hilang)

    def _post(path, json_body=None, timeout=15):
        try:
            r = requests.post(f"{backend_url}{path}", params={"session_id": session_id}, json=json_body or {},
                               headers={"X-Kaggle-Worker-Token": worker_token}, timeout=timeout)
            if r.status_code in (401, 410):
                gone["count"] += 1
                return None
            if r.ok:
                gone["count"] = 0
                return r.json()
        except Exception:
            pass
        return None

    def _status(status, model_id=None, error=None, phase=None):
        _post("/agent/local/kaggle-status", {"status": status, "model_id": model_id, "error": error, "phase": phase})

    _stop = threading.Event()
    state = {"proc": None}  # llama-server saat ini (diganti saat restart model)

    def _shutdown_now(reason):
        """Dipanggil dari thread heartbeat: berhenti walau main thread sedang sibuk memuat model."""
        print(f"[ai-local] {reason}, mematikan worker.", flush=True)
        try:
            _post("/agent/local/kaggle-poll", {"closing": True, "heartbeat_only": False, "reason": reason[:40]}, timeout=8)
        except Exception:
            pass
        try:
            proc_now = state.get("proc")
            if proc_now and proc_now.poll() is None:
                proc_now.terminate()
        except Exception:
            pass
        os._exit(0)  # kernel Kaggle selesai -> GPU dilepas

    def _heartbeat_loop():
        while not _stop.is_set():
            res = _post("/agent/local/kaggle-poll", {"closing": False, "heartbeat_only": True}, timeout=10)
            if isinstance(res, dict) and res.get("control") == "stop":
                _shutdown_now("stop diminta user")
            if gone["count"] >= 6:
                # Backend tidak mengenal sesi ini lagi (worker yatim / sesi diganti): jangan buang kuota GPU.
                _shutdown_now("sesi backend hilang")
            if _stop.wait(8):
                break

    # Idle 3-6 jam: model tidak dibangun ulang hanya karena user jeda sebentar.
    try:
        idle_shutdown_seconds = int(idle_timeout or os.environ.get("WORKER_IDLE_TIMEOUT") or 4 * 3600)
    except (TypeError, ValueError):
        idle_shutdown_seconds = 4 * 3600
    idle_shutdown_seconds = max(3 * 3600, min(6 * 3600, idle_shutdown_seconds))

    base_dir = BASE_DIR
    os.makedirs(base_dir, exist_ok=True)
    proc = None
    log_file = None
    hb_thread = threading.Thread(target=_heartbeat_loop, daemon=True)
    hb_thread.start()
    try:
        _status("booting", phase="worker")
        _status("booting", phase="packages")
        _prepare_media_packages()
        _status("booting", phase="model")
        proc, log_file, model_id = start_llama_server(base_dir, DEFAULT_PORT, DEFAULT_MAX_CTX)
        state["proc"] = proc
        if not wait_ready(proc, DEFAULT_PORT):
            tail = ""
            if os.path.exists("/tmp/server.log"):
                with open("/tmp/server.log", "r", errors="ignore") as lf:
                    tail = lf.read()[-800:]
            raise RuntimeError(f"llama-server gagal start: {tail}")
        _status("ready", model_id=model_id)
        print(f"[ai-local] ready, model={model_id}", flush=True)

        api_url = f"http://127.0.0.1:{DEFAULT_PORT}/v1/chat/completions"
        history = [{"role": "system", "content": build_system_prompt(base_dir)}]
        idle_since = time.time()
        print(f"[ai-local] idle shutdown after {idle_shutdown_seconds // 3600}h without activity", flush=True)

        while True:
            job_res = _post("/agent/local/kaggle-poll", {"closing": False, "heartbeat_only": False}, timeout=10)
            job = job_res.get("job") if isinstance(job_res, dict) else None
            control = job_res.get("control") if isinstance(job_res, dict) else None
            if control == "restart":
                # Restart di tempat: muat ulang model di worker yang SAMA (tanpa kernel Kaggle baru).
                print("[ai-local] restart model diminta user.", flush=True)
                _status("booting", phase="model")
                try:
                    if proc and proc.poll() is None:
                        proc.terminate()
                        proc.wait(timeout=30)
                except Exception:
                    pass
                if log_file:
                    try:
                        log_file.close()
                    except Exception:
                        pass
                proc, log_file, model_id = start_llama_server(base_dir, DEFAULT_PORT, DEFAULT_MAX_CTX)
                state["proc"] = proc
                if not wait_ready(proc, DEFAULT_PORT):
                    raise RuntimeError("llama-server gagal start setelah restart")
                history = [{"role": "system", "content": build_system_prompt(base_dir)}]
                _status("ready", model_id=model_id)
                idle_since = time.time()
                continue
            if gone["count"] >= 6:
                # Backend sudah tidak mengenal sesi ini (restart / user menjalankan ulang worker baru).
                # Berhenti supaya GPU Kaggle tidak terbuang sia-sia.
                print("[ai-local] sesi backend hilang, mematikan worker.", flush=True)
                break
            if not job:
                if time.time() - idle_since > idle_shutdown_seconds:
                    print("[ai-local] idle timeout, mematikan worker.", flush=True)
                    _post("/agent/local/kaggle-poll", {"closing": True, "heartbeat_only": False, "reason": "idle"}, timeout=10)
                    break
                time.sleep(2.5)
                continue

            idle_since = time.time()
            job_id = job.get("job_id")
            job_type = job.get("type")
            payload = job.get("payload") or {}
            try:
                if job_type == "chat":
                    content = payload.get("prompt") or ""
                    images = payload.get("images") or []
                    if images:
                        content = [{"type": "text", "text": payload.get("prompt") or ""}]
                        for img in images:
                            content.append({"type": "image_url", "image_url": {
                                "url": f"data:{img.get('mime_type')};base64,{img.get('data')}"}})
                    def _report_activity(msg):
                        try:
                            _post("/agent/local/kaggle-activity", {"activity": msg}, timeout=5)
                        except Exception:
                            pass
                    def _report_step(step):
                        _post("/agent/local/kaggle-activity", {"activity": step.get("title"), "step": step}, timeout=5)
                    _post("/agent/local/kaggle-activity", {"activity": "Memahami permintaan…", "reset": True}, timeout=5)
                    answer = run_agent_turn(history, api_url, base_dir, content,
                                            on_activity=lambda _m: None, on_step=_report_step)
                    _report_activity("Menyiapkan file dan jawaban…")
                    cleaned, attachments = extract_and_deliver(answer or "", session_id)
                    try:
                        _post("/agent/local/kaggle-activity", {"activity": None}, timeout=5)
                    except Exception:
                        pass
                    result_body = {"content": cleaned}
                    if attachments:
                        result_body["attachments"] = attachments
                    _post("/agent/local/kaggle-result", {"job_id": job_id, "ok": True, "result": result_body})
                elif job_type == "workspace":
                    out = run_workspace_job(payload.get("op"), payload.get("op_payload") or {}, base_dir)
                    _post("/agent/local/kaggle-result", {"job_id": job_id, "ok": out.get("ok", False), "result": out})
                else:
                    _post("/agent/local/kaggle-result", {"job_id": job_id, "ok": False, "error": f"unknown job type {job_type}"})
            except Exception as exc:
                _post("/agent/local/kaggle-result", {"job_id": job_id, "ok": False, "error": _clean_err(exc)[:500]})

        _post("/agent/local/kaggle-poll", {"closing": True, "heartbeat_only": False}, timeout=10)
    except Exception as exc:
        err = _clean_err(exc)[:400]
        print(f"[ai-local] FATAL: {err}", flush=True)
        _status("error", error=err)
    finally:
        _stop.set()
        if proc and proc.poll() is None:
            proc.terminate()
            proc.wait()
        if log_file:
            log_file.close()
        print("[ai-local] worker selesai.", flush=True)
