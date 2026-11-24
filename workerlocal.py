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
import shutil
import subprocess
import threading
import importlib
import importlib.util
import urllib.parse
import urllib.request

os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"

WORKER_VERSION = "2026-11-24-ai-local-kaggle-uploads"
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
        "Kamu adalah Autonomous AI Partner yang berjalan lokal (GPU Kaggle), bisa membaca/menulis file, "
        "menjalankan perintah shell, mencari web, dan menganalisis gambar.\n"
        f"WORKSPACE UTAMA: `{base_dir}` (dan `/kaggle/input` untuk data read-only).\n"
        f"FILE UPLOAD PENGGUNA: `{base_dir}/uploads`. Untuk PDF, DOCX, PPTX, XLSX, CSV, gambar berisi teks, "
        "audio, video, dan arsip zip/tar gunakan tool read_document. Jangan memakai read_file pada file biner.\n"
        "TOOLS: list_dir, read_file, patch_file, write_file, run_bash, web_search, web_fetch, inspect_image, read_document.\n"
        "Paket dokumen (pymupdf, python-docx, python-pptx, faster-whisper) dipasang offline dari dataset "
        "media-offline-pkgs ke /tmp/docpkgs tanpa menimpa numpy atau pillow bawaan Kaggle. "
        "pandas dan openpyxl memakai paket yang sudah ada di image Kaggle. ffmpeg dan tesseract dipakai jika sudah ada, "
        "dan baru dipasang lewat apt bila belum ada.\n"
        "Jangan mengarang data; gunakan tool untuk memverifikasi sebelum menjawab fakta terkini. "
        "Jujur jika sebuah tool gagal atau data tidak ditemukan."
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


def run_agent_turn(history, api_url, base_dir, user_content, max_steps=8):
    history.append({"role": "user", "content": user_content})
    for _step in range(max_steps):
        payload = {"messages": history, "tools": TOOLS, "tool_choice": "auto",
                   "max_tokens": -1, "temperature": 0.25, "stream": False}
        req = urllib.request.Request(api_url, data=json.dumps(payload).encode(),
                                      headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=600) as resp:
            body = json.loads(resp.read().decode("utf-8", errors="ignore"))
        choice = (body.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        tool_calls = message.get("tool_calls") or []
        history.append({"role": "assistant", "content": message.get("content"),
                         **({"tool_calls": tool_calls} if tool_calls else {})})
        if not tool_calls:
            return message.get("content") or ""

        pending_images = []
        for tc in tool_calls:
            fn_name = tc["function"]["name"]
            try:
                parsed_args = json.loads(tc["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                parsed_args = {}
            result = exec_tool(fn_name, parsed_args, base_dir=base_dir)
            if isinstance(result, dict) and "__image_payload__" in result:
                img_fp, q_img = result["__image_payload__"], result["question"]
                history.append({"role": "tool", "tool_call_id": tc["id"], "name": fn_name,
                                 "content": f"Gambar '{img_fp}' dimuat untuk inspeksi visual."})
                pending_images.append((img_fp, q_img))
            else:
                history.append({"role": "tool", "tool_call_id": tc["id"], "name": fn_name, "content": str(result)})
        for img_fp, q_img in pending_images:
            history.append({"role": "user", "content": [
                {"type": "text", "text": f"[Inspeksi Visual: {img_fp}] {q_img}"},
                {"type": "image_url", "image_url": {"url": encode_image_b64(img_fp)}},
            ]})
    return "[Berhenti: terlalu banyak langkah tool berturut-turut tanpa jawaban akhir.]"


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
def run_kaggle_worker(session_id=None, backend_url=None, worker_token=None):
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

    def _heartbeat_loop():
        while not _stop.is_set():
            _post("/agent/local/kaggle-poll", {"closing": False, "heartbeat_only": True}, timeout=10)
            if _stop.wait(8):
                break

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
        idle_shutdown_seconds = int(os.environ.get("WORKER_IDLE_TIMEOUT", "900"))

        while True:
            job_res = _post("/agent/local/kaggle-poll", {"closing": False, "heartbeat_only": False}, timeout=10)
            job = job_res.get("job") if isinstance(job_res, dict) else None
            if gone["count"] >= 6:
                # Backend sudah tidak mengenal sesi ini (restart / user menjalankan ulang worker baru).
                # Berhenti supaya GPU Kaggle tidak terbuang sia-sia.
                print("[ai-local] sesi backend hilang, mematikan worker.", flush=True)
                break
            if not job:
                if time.time() - idle_since > idle_shutdown_seconds:
                    print("[ai-local] idle timeout, mematikan worker.", flush=True)
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
                    answer = run_agent_turn(history, api_url, base_dir, content)
                    _post("/agent/local/kaggle-result", {"job_id": job_id, "ok": True, "result": {"content": answer}})
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
