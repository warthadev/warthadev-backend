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

WORKER_VERSION = "2026-11-25-ai-local-cloud-bridge"
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
_DELIVER_ALLOWED_EXT = {
    "py", "js", "ts", "tsx", "jsx", "json", "md", "txt", "csv", "tsv", "html", "css", "yaml", "yml",
    "xml", "sql", "sh", "ipynb", "log", "toml", "ini", "cfg", "env.example", "srt", "vtt", "lrc",
    "pdf", "docx", "pptx", "xlsx", "xls",
    "zip", "tar", "gz", "tgz", "whl",
    "png", "jpg", "jpeg", "webp", "gif", "bmp", "svg",
    "mp3", "wav", "m4a", "ogg", "flac", "opus", "mp4", "mkv", "webm", "mov",
}
_DELIVER_BLOCKED = {"kaggle.json", ".env", ".netrc", "id_rsa", "id_ed25519"}
_DELIVER_MARKER = re.compile(r"\[\[file:([^\]\n]+)\]\]")


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
    path = _Path(raw.strip()).expanduser().resolve()
    if _DELIVER_WORK_ROOT not in path.parents and path != _DELIVER_WORK_ROOT:
        raise ValueError("file di luar /kaggle/working tidak boleh dikirim")
    if any(part.startswith(".") for part in path.relative_to(_DELIVER_WORK_ROOT).parts):
        raise ValueError("file tersembunyi tidak boleh dikirim")
    if path.name.lower() in _DELIVER_BLOCKED:
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


def deliver_file(raw_path, session_id=""):
    path = _deliver_safe_path(raw_path)
    if path.is_dir():
        path = _deliver_zip_dir(path)
    ext = path.suffix.lower().lstrip(".")
    if ext not in _DELIVER_ALLOWED_EXT:
        raise ValueError(f"ekstensi .{ext or '?'} tidak didukung")
    size = path.stat().st_size
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    entry = {"name": path.name, "mime_type": mime, "size": size, "path": str(path)}
    if size <= _DELIVER_INLINE_MAX:
        entry["data_base64"] = base64.b64encode(path.read_bytes()).decode("ascii")
        return entry
    if size > _DELIVER_UPLOAD_MAX:
        raise ValueError("file terlalu besar")
    try:
        entry["url"] = _deliver_upload_hf(path, session_id)
    except Exception as err:
        if size > _DELIVER_INLINE_FALLBACK_MAX:
            raise ValueError(f"upload ke Hugging Face gagal ({err}) dan file terlalu besar untuk inline")
        entry["data_base64"] = base64.b64encode(path.read_bytes()).decode("ascii")
    return entry


def extract_and_deliver(text, session_id=""):
    attachments, notes, seen = [], [], set()
    for match in _DELIVER_MARKER.finditer(text or ""):
        raw = match.group(1).strip()
        if raw in seen:
            continue
        seen.add(raw)
        try:
            attachments.append(deliver_file(raw, session_id))
        except Exception as err:
            notes.append(f"Gagal mengirim `{os.path.basename(raw)}`: {err}")
    cleaned = _DELIVER_MARKER.sub("", text or "").strip()
    if notes:
        cleaned = f"{cleaned}\n\n" + "\n".join(notes)
    return cleaned, attachments


# SHARED-BEGIN
# Generated from modules/agent_shared.py by scripts/sync_worker_shared.py.
# DO NOT EDIT BY HAND — edit agent_shared.py and re-run the script.
# The worker is pushed to Kaggle as one file and cannot import backend modules,
# so the shared definitions live here as generated literals.

TOOL_RESULT_LIMIT = 12000
TOOL_CALL_SIG_LIMIT = 2000
TOOL_REPEAT_PREVIEW_CHARS = 400
TOOL_ERROR_SIG_CHARS = 200

SHARED_RULES_ID = {'how_you_talk': 'CARA BICARA: bersikaplah seperti rekan kerja manusia yang ahli, kompeten, dan profesional — ramah, jelas, to the point, dan percaya diri tanpa berlebihan. Jawab selalu dalam bahasa yang dipakai user.', 'know_user': 'KONTEKS: setiap giliran chat disertai catatan [KONTEKS SAAT INI] berisi nama dan lokasi user, waktu dan tanggal saat ini, serta wilayahmu — pakai itu saat menjawab pertanyaan tentang nama, waktu, tanggal, dan tempat. Kamu tahu nama dan lokasi user dari awal; kamu TIDAK berada di lokasi user. Jangan pernah bilang tidak tahu nama atau lokasi user kalau catatannya menyebutkannya.', 'greet_first': 'SAPA DULU: setiap ada chat masuk, sebelum memanggil tool apa pun, tulis dulu satu pesan pembuka singkat (1-2 kalimat) di isi jawabanmu: apa yang kamu pahami dari permintaan user dan apa yang akan kamu lakukan pertama. Pesan ini dikirim langsung ke user sebagai sapaan, jadi harus natural seperti manusia yang menyapa, bukan seperti log mesin. Ini berlaku untuk proses berat maupun ringan — selalu sapa dulu, baru bertindak.', 'topic': 'TOPIK: pesan user yang terbaru adalah pembahasan yang sedang berjalan. Kalau user mengganti pembahasan, ikuti pembahasan yang baru dan jangan kembali ke topik lama kecuali user yang memintanya. Riwayat sebelumnya hanya konteks, bukan topik aktif. Jangan mengulang kata atau kalimat yang sama berulang kali; kalau sudah menyampaikan sesuatu, lanjut ke hal berikutnya.', 'typos': 'TYPO: kalau pesan user kemungkinan mengandung typo, jangan langsung berasumsi. Kalau maksudnya jelas dari konteks, kerjakan langsung sambil menyebutkan singkat asumsi kamu (misal: "saya anggap maksudmu X"). Kalau typo itu bikin perintah jadi ambigu — ada dua kemungkinan arti, atau mengubah makna perintah — tanyakan dulu ke user sebelum bertindak. Jangan menebak diam-diam untuk hal yang penting (hapus file, kirim sesuatu, ubah data).', 'rich_ui': 'RICH UI: untuk struktur, alur, perbandingan, linimasa, data, atau langkah — jangan hanya pakai markdown. Kirim blok ```ui berisi SATU objek JSON (tanpa teks lain di dalam blok). Jenis yang tersedia:\n- card: {"type":"card","tone":"info|success|warning|error|tip","icon":"info|check|warning|lightbulb|rocket|globe|calendar|clock|book|database|shield|star|zap","title":"..","body":".."} — sorotan/penting/peringatan.\n- steps: {"type":"steps","title":"..","items":[{"title":"..","text":".."}]} — langkah berurutan.\n- flow: {"type":"flow","title":"..","items":[{"label":"..","text":".."}]} — alur/diagram bentuk.\n- timeline: {"type":"timeline","title":"..","items":[{"date":"..","title":"..","text":".."}]} — urutan waktu.\n- compare: {"type":"compare","title":"..","columns":[{"title":"..","tone":"good|bad|neutral","items":[".."]}]} — perbandingan.\n- chart: {"type":"chart","title":"..","labels":[".."],"values":[angka]} — grafik batang data.\n- checklist: {"type":"checklist","title":"..","items":[{"text":"..","done":true}]} — daftar centang.\n- stats: {"type":"stats","title":"..","items":[{"label":"..","value":"..","note":".."}]} — angka penting.\nAturan: satu blok ui per struktur, JSON harus valid, teks singkat, maksimal 12 item per daftar. Prosa biasa, penjelasan, dan kode tetap pakai markdown/code block seperti biasa. Jangan pakai blok ui untuk hal yang cukup dijelaskan dengan satu paragraf.', 'anti_loop': 'ANTI-LOOP: jangan mengulang tool call yang sama persis. Jika sebuah cara gagal, ganti pendekatan atau laporkan error aslinya sebagai jawaban akhir. Begitu file selesai ditulis, langsung beri jawaban akhir dengan marker [[file:...]] — jangan memeriksa berulang-ulang.', 'deliver_files': "Jika kamu membuat file untuk pengguna, SELALU tulis file itu ke disk dengan tool write_file di /kaggle/working/out/ lalu tulis [[file:/kaggle/working/out/NAMA_FILE]] di jawabanmu — ini satu-satunya cara mengirim file ke pengguna. Folder juga boleh (akan otomatis di-zip). Jangan menempelkan isi file besar ke chat sebagai gantinya.\nATURAN KERAS SOAL PENGIRIMAN FILE:\n- Pengguna TIDAK bisa membuka Kaggle sendiri dan TIDAK punya akses ke notebook/kernel ini. Jangan PERNAH menyuruh pengguna 'download dari Kaggle', 'cek output Kaggle', 'buka notebook', atau instruksi sejenis — itu mustahil dilakukan pengguna dan merupakan kegagalan tugas.\n- Jangan berhenti di tengah pembuatan kode dengan alasan 'lanjutkan sendiri' atau 'sisanya bisa kamu generate'. Jika tugas berisi banyak file, kerjakan SATU PER SATU dengan write_file sampai semua selesai sebelum menjawab, baru sertakan marker [[file:...]] untuk tiap file (atau satu marker folder untuk semuanya sekaligus).\n- Kode yang kamu tulis lewat write_file harus lengkap dan bisa langsung dipakai, bukan potongan atau placeholder seperti '# TODO: lengkapi sisanya'.\n- Jika proyek benar-benar terlalu besar untuk diselesaikan dalam satu giliran, kirim dulu file-file yang sudah selesai lewat marker [[file:...]], lalu katakan dengan jelas file/bagian apa yang masih akan menyusul di balasan berikutnya — jangan menyuruh pengguna menyelesaikannya sendiri.\nJika user meminta struktur folder/file tree/arsip/daftar lengkap, SELALU berikan struktur LENGKAP tanpa memotong, tanpa ellipsis '...', dan tanpa meringkas sisa item. Jangan malas: jangan berhenti di tengah. Prefer kelengkapan daripada singkat. Jika output terlalu panjang dan terpaksa dipotong, sebutkan titik berhenti lalu lanjutkan dari titik itu di jawaban berikutnya."}
SHARED_RULE_ORDER_ID = ['topic', 'how_you_talk', 'greet_first', 'typos', 'know_user', 'rich_ui', 'anti_loop', 'deliver_files']

def _shared_rules_text():
    """The shared rules as one prompt block, worker order, single-newline
    separator — exactly how the worker prompt was already built."""
    return "\n".join(SHARED_RULES_ID[name] for name in SHARED_RULE_ORDER_ID)

TOOLS = [{'type': 'function', 'function': {'name': 'list_dir', 'description': 'Melihat daftar folder, file, dan ukuran/baris di direktori proyek (default workspace: /kaggle/working).', 'parameters': {'type': 'object', 'properties': {'path': {'type': 'string', 'description': 'Path direktori target, misal /kaggle/working atau /'}}, 'required': ['path']}}}, {'type': 'function', 'function': {'name': 'read_file', 'description': 'Membaca isi file dengan nomor baris (hingga 7.500 baris sekaligus).', 'parameters': {'type': 'object', 'properties': {'path': {'type': 'string'}, 'start_line': {'type': 'integer'}, 'end_line': {'type': 'integer'}}, 'required': ['path']}}}, {'type': 'function', 'function': {'name': 'patch_file', 'description': 'Mengganti baris tertentu pada file besar tanpa menulis ulang seluruh file (otomatis backup .bak).', 'parameters': {'type': 'object', 'properties': {'path': {'type': 'string'}, 'start_line': {'type': 'integer'}, 'end_line': {'type': 'integer'}, 'new_code': {'type': 'string'}}, 'required': ['path', 'start_line', 'end_line', 'new_code']}}}, {'type': 'function', 'function': {'name': 'write_file', 'description': 'Membuat file baru atau menimpa isi file kecil/menengah dengan konten lengkap.', 'parameters': {'type': 'object', 'properties': {'path': {'type': 'string'}, 'content': {'type': 'string'}}, 'required': ['path', 'content']}}}, {'type': 'function', 'function': {'name': 'run_bash', 'description': 'Menjalankan perintah shell non-interaktif di kernel Kaggle ini.', 'parameters': {'type': 'object', 'properties': {'command': {'type': 'string'}, 'cwd': {'type': 'string'}}, 'required': ['command']}}}, {'type': 'function', 'function': {'name': 'web_search', 'description': 'Mencari informasi terkini di internet.', 'parameters': {'type': 'object', 'properties': {'query': {'type': 'string'}}, 'required': ['query']}}}, {'type': 'function', 'function': {'name': 'web_image_search', 'description': 'Mencari gambar di internet. Mengembalikan URL thumbnail beserta halaman sumbernya. Thumbnail otomatis ditampilkan ke user sebagai strip — jelaskan hasilnya di jawaban, jangan tempel URL mentahnya.', 'parameters': {'type': 'object', 'properties': {'query': {'type': 'string'}}, 'required': ['query']}}}, {'type': 'function', 'function': {'name': 'web_fetch', 'description': 'Mengambil isi lengkap halaman web dari URL spesifik.', 'parameters': {'type': 'object', 'properties': {'url': {'type': 'string'}}, 'required': ['url']}}}, {'type': 'function', 'function': {'name': 'inspect_image', 'description': 'Memuat file gambar dari disk agar dapat dianalisis secara visual.', 'parameters': {'type': 'object', 'properties': {'path': {'type': 'string'}, 'question': {'type': 'string'}}, 'required': ['path']}}}, {'type': 'function', 'function': {'name': 'read_document', 'description': 'Membaca file di disk Kaggle tanpa menempel seluruh isi ke konteks: PDF (teks, atau OCR jika scan), DOCX, PPTX, XLSX/CSV (pandas bawaan), gambar (OCR), audio/video (ffmpeg + faster-whisper), dan arsip zip/tar/gz. Pakai ini untuk file di /kaggle/working/uploads. Jangan pakai read_file untuk PDF/biner.', 'parameters': {'type': 'object', 'properties': {'path': {'type': 'string', 'description': 'Path absolut file, misal /kaggle/working/uploads/nama.pdf'}, 'max_chars': {'type': 'integer', 'description': 'Batas karakter hasil, default 12000'}}, 'required': ['path']}}}]

def _short_preview(text, limit, lang):
    """Truncate a preview the way the worker's _short does: strip, then cap with
    a "+N characters" suffix when it overflows."""
    text = str(text if text is not None else "").strip()
    if len(text) <= limit:
        return text
    suffix = f"… [+{len(text) - limit} karakter]" if lang == "id" else f"… [+{len(text) - limit} characters]"
    return text[:limit] + suffix

def tool_call_signature(name, args):
    """Identical-call signature: name + args JSON, truncated so a giant argument
    cannot blow up the loop's memory."""
    return f"{name}:{json.dumps(args, sort_keys=True, ensure_ascii=False)[:TOOL_CALL_SIG_LIMIT]}"

def is_error_result(text):
    """True when a tool result means failure: an "Error..." string, or a shell
    run that exited non-zero. Judged from the result content, not from whether
    the transport raised — a tool that returns an error string failed."""
    if not isinstance(text, str):
        text = str(text)
    return text.startswith("Error") or re.match(r"EXIT CODE: (?!0\b)", text) is not None

def error_signature(name, text):
    """Same-failure signature: tool name + first line of the error, truncated."""
    if not isinstance(text, str):
        text = str(text)
    first = text.splitlines()[0][:TOOL_ERROR_SIG_CHARS] if text.splitlines() else ""
    return f"{name}:{first}"

def invalid_args_message(name, parse_error, lang):
    """A tool call arrived with arguments that are not valid JSON: do not run
    it — tell the model to resend valid JSON."""
    if lang == "id":
        return (
            f"Error: argumen tool '{name}' bukan JSON valid ({parse_error}). "
            "Kirim ulang dengan JSON valid. Untuk file panjang, tulis per bagian kecil."
        )
    return (
        f"Error: tool '{name}' arguments are not valid JSON ({parse_error}). "
        "Send them again as valid JSON. For long files, write in small parts."
    )

def repeat_stop_message(name, lang):
    """Second repeat of an identical call after one warning: the loop is stuck
    on the same call — stop and answer from what is already known."""
    if lang == "id":
        return (
            f"Panggilan '{name}' ini sudah kamu coba sebelumnya dan hasilnya tidak berubah. "
            "Berhenti mengulang hal yang sama."
        )
    return (
        f"This '{name}' call was already tried before and the result did not change. "
        "Stop repeating the same thing."
    )

def repeat_warning(cached_result, lang):
    """First repeat of an identical call: do not re-run it — hand the model the
    cached result (short preview) plus a warning not to call it again."""
    preview = _short_preview(cached_result, TOOL_REPEAT_PREVIEW_CHARS, lang)
    if lang == "id":
        return (
            "Kamu sudah menjalankan panggilan ini sebelumnya dan hasilnya sama seperti dulu, "
            "tidak akan berubah kalau diulang. Jangan ulangi. Coba cara lain, atau beri jawaban "
            f"akhir dari apa yang sudah kamu ketahui. Hasil sebelumnya: {preview}"
        )
    return (
        "This exact tool call was already made. Do not call it again — use the result above. "
        f"Previous result: {preview}"
    )

def stuck_final_message(stuck_reason, lang):
    """System message for the forced final text-only round after the loop
    recognizes it is stuck (same call repeated after a warning, or the same
    failure twice)."""
    if lang == "id":
        return (
            "[SISTEM] Kamu terlihat berputar di tempat yang sama"
            + (f" ({stuck_reason})" if stuck_reason else "") +
            ". Berhenti mencoba hal baru. Tulis jawaban akhir sekarang dalam bahasa pengguna: apa yang sudah berhasil, "
            "apa yang gagal beserta error aslinya, dan saran langkah berikutnya. Jika ada file jadi di "
            "/kaggle/working/out, sertakan marker [[file:...]]."
        )
    return (
        "[SYSTEM] You look stuck going around in circles"
        + (f" ({stuck_reason})" if stuck_reason else "") +
        ". Stop trying new things. Write the final answer now in the user's language: what worked, "
        "what failed with its original error, and suggested next steps. If there is a finished file in "
        "/kaggle/working/out, include a [[file:...]] marker."
    )

def empty_retry_message(lang):
    """One retry when a round returns no tool calls and no visible text."""
    if lang == "id":
        return "[SISTEM] Jawabanmu kosong. Tulis jawaban akhir sekarang."
    return "[SYSTEM] Your response was empty. Write the final answer now."

def clip_tool_result(text, lang):
    """Cap one tool result before it enters model context."""
    if not isinstance(text, str):
        text = str(text)
    if len(text) > TOOL_RESULT_LIMIT:
        note = "\n[...dipotong, hasil tool dibatasi 12000 karakter...]" if lang == "id" else "\n[...truncated, tool result limited to 12000 characters...]"
        return text[:TOOL_RESULT_LIMIT] + note
    return text

def search_image_urls(text, limit=6):
    """Pull thumbnail URLs out of a web_image_search result. The worker emits
    one line per image as ``- title | gambar: <thumbnail url> | sumber: <page
    url>``; the ``gambar:`` marker is the stable handle, whatever the page
    language is. Deduped, in order, capped — the strip shows at most 6."""
    if not isinstance(text, str):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for url in re.findall(r"gambar:\s*(https?://\S+)", text):
        url = url.rstrip(").,")
        if url and url not in seen:
            seen.add(url)
            out.append(url)
        if len(out) >= limit:
            break
    return out

def fallback_summary(ran_tools, lang):
    """Last resort when the model produces no final text: summarize what the
    tools did. ``ran_tools`` is a list of (name, detail, ok, preview) tuples —
    the same shape the worker's run_agent_turn records."""
    if not ran_tools:
        if lang == "id":
            return "Maaf, model tidak menghasilkan jawaban. Coba kirim ulang permintaannya dengan lebih spesifik."
        return "Sorry, the model did not produce an answer. Try sending your request again, more specifically."
    if lang == "id":
        lines = ["Aku sudah menjalankan beberapa langkah tapi belum sampai ke hasil akhir. Langkah terakhir:"]
        for name, detail, ok, preview in ran_tools[-5:]:
            mark = "OK" if ok else "GAGAL"
            lines.append(f"- [{mark}] {name}{detail}: {_short_preview(preview, 160, lang)}")
        lines.append("Kirim pesan lagi (misalnya “lanjutkan”) atau minta format file yang lebih sederhana.")
        return "\n".join(lines)
    lines = ["I ran a few steps but did not reach a final result yet. Last steps:"]
    for name, detail, ok, preview in ran_tools[-5:]:
        mark = "OK" if ok else "FAILED"
        lines.append(f"- [{mark}] {name}{detail}: {_short_preview(preview, 160, lang)}")
    lines.append("Send another message (for example “continue”) or ask for a simpler file format.")
    return "\n".join(lines)

def build_context_note(context, lang="en", side="cloud"):
    """[CURRENT CONTEXT] (cloud, en) / [KONTEKS SAAT INI] (worker, id): who the
    user is, where they are, and their real time/date right now, plus where the
    agent itself runs. Cloud returns None when the context carries nothing at
    all; the worker always returns the note (it also carries its own clock)."""
    ctx = context if isinstance(context, dict) else {}
    tz = str(ctx.get("timezone") or "").strip()
    local_time = str(ctx.get("local_time") or "").strip()
    date_str = str(ctx.get("date") or "").strip()
    time_str = str(ctx.get("time") or "").strip()
    user_name = str(ctx.get("user_name") or "").strip()
    user_location = str(ctx.get("user_location") or "").strip()
    if side == "worker":
        now_utc = time.strftime("%Y-%m-%d %H:%M", time.gmtime())
        lines = ["[KONTEKS SAAT INI]"]
        if user_name:
            lines.append(f"- Nama user: {user_name}. Kamu tahu namanya dari awal — pakai secara natural kalau cocok, dan jawab kalau dia bertanya.")
        if user_location:
            lines.append(f"- Lokasi user: {user_location}.")
        if tz or local_time or date_str or time_str:
            when = "- Waktu user sekarang: "
            if date_str:
                when += date_str
            if time_str:
                when += (", " if date_str else "") + time_str
            elif local_time:
                when += local_time
            when += f" (zona waktu {tz or 'tidak diketahui'})."
            lines.append(when)
        lines.append(f"- Waktumu sekarang (worker Kaggle, UTC): {now_utc} UTC.")
        lines.append("- Wilayahmu: kernel Kaggle (cloud Google), zona waktu UTC; backend di Oregon, Amerika Serikat. Kamu TIDAK berada di lokasi user.")
        if tz and not user_location:
            lines.append(f"- Wilayah user: zona waktu {tz}.")
        lines.append("Pakai info ini untuk pertanyaan tentang nama, waktu, tanggal, dan tempat. "
                     "Jangan menebak kota user dari zona waktu saja.")
        return "\n".join(lines)
    if not local_time and not tz and not date_str and not time_str and not user_name and not user_location:
        return None
    parts = ["[CURRENT CONTEXT]"]
    if user_name:
        parts.append(f"The user you are talking to is {user_name}. You know their name from the start — use it naturally when it fits, and answer if they ask for it.")
    if user_location:
        parts.append(f"The user is in {user_location}.")
    if date_str or time_str or local_time:
        when = "Right now it is "
        if date_str:
            when += date_str
        if time_str:
            when += (", " if date_str else "") + time_str
        elif local_time:
            when += local_time
        if tz:
            when += f" ({tz})"
        parts.append(when + ".")
    elif tz:
        parts.append(f"User's timezone: {tz}.")
    parts.append("Backend runs in Oregon, USA. Use this for time, date, and place questions; do not guess the user's city from the timezone alone.")
    return "\n".join(parts)

# SHARED-END


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


def _search_ddg_images(query, max_results=6):
    """DuckDuckGo image search: ambil token vqd dari halaman hasil, lalu
    panggil endpoint i.js (JSON). Mengembalikan list dict {title, thumbnail,
    image, url}. Thumbnail-nya dari CDN Bing (tse*.mm.bing.net) — stabil
    untuk di-embed langsung di chat."""
    encoded_q = urllib.parse.urlencode({"q": query, "iax": "images", "ia": "images"})
    req = urllib.request.Request(f"https://duckduckgo.com/?{encoded_q}", headers=BROWSER_HEADERS)
    with urllib.request.urlopen(req, timeout=15) as r:
        page = r.read().decode("utf-8", errors="ignore")
    m = re.search(r"vqd=['\"]([\d-]+)['\"]", page) or re.search(r"vqd=([\d-]+)", page)
    if not m:
        return []
    params = {"l": "us-en", "o": "json", "q": query, "vqd": m.group(1), "f": ",,,,,", "p": "1", "s": "0"}
    req2 = urllib.request.Request("https://duckduckgo.com/i.js?" + urllib.parse.urlencode(params),
                                  headers={**BROWSER_HEADERS,
                                           "Accept": "application/json, text/javascript, */*; q=0.01",
                                           "Referer": "https://duckduckgo.com/"})
    with urllib.request.urlopen(req2, timeout=15) as r2:
        data = json.loads(r2.read().decode("utf-8", errors="ignore"))
    out = []
    for item in (data.get("results") or [])[:max_results]:
        thumb = (item.get("thumbnail") or "").strip()
        if not thumb:
            continue
        out.append({"title": (item.get("title") or "").strip(),
                    "thumbnail": thumb,
                    "image": (item.get("image") or "").strip(),
                    "url": (item.get("url") or "").strip()})
    return out


def _search_openverse_images(query, max_results=6):
    """Fallback kalau DuckDuckGo menantang (challenge 202): API resmi
    Openverse (tanpa key, batas anonim 20/menit) — gambar berlisensi terbuka,
    thumbnail disajikan langsung oleh api.openverse.org."""
    url = "https://api.openverse.org/v1/images/?" + urllib.parse.urlencode(
        {"q": query, "page_size": max_results})
    req = urllib.request.Request(url, headers={
        "User-Agent": "WarthaSensei/1.0 (personal AI agent)",
        "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read().decode("utf-8", errors="ignore"))
    out = []
    for item in (data.get("results") or [])[:max_results]:
        thumb = (item.get("thumbnail") or "").strip()
        if not thumb:
            continue
        out.append({"title": (item.get("title") or "").strip(),
                    "thumbnail": thumb,
                    "image": (item.get("url") or "").strip(),
                    "url": (item.get("foreign_landing_url") or "").strip()})
    return out


def perform_web_image_search(query, max_results=6):
    try:
        items = _search_ddg_images(query, max_results=max_results)
    except Exception as e:
        items = []
    if not items:
        # DuckDuckGo sering menantang IP datacenter (challenge 202, 0 hasil)
        # — jangan pulang dengan tangan kosong, coba Openverse.
        try:
            items = _search_openverse_images(query, max_results=max_results)
        except Exception as e:
            items = []
    if not items:
        return "[PENCARIAN GAMBAR TIDAK MENGHASILKAN DATA VALID]"
    lines = []
    for item in items[:max_results]:
        title = re.sub(r"\s+", " ", item["title"]).strip() or "(tanpa judul)"
        lines.append(f"- {title} | gambar: {item['thumbnail']} | sumber: {item['url']}")
    return "\n".join(lines)


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
# Format yang butuh paket berat (pymupdf/docx/pptx/pandas/tesseract/ffmpeg/faster-whisper).
_MEDIA_EXTS = {".pdf", ".docx", ".pptx", ".xlsx", ".xlsm", ".xls", ".csv", ".tsv", ".parquet",
               ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff",
               ".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac", ".mp4", ".mkv", ".webm", ".mov", ".avi"}


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


_MEDIA_PACKAGES_READY = False


def _prepare_work_dirs():
    os.makedirs(BASE_DIR, exist_ok=True)
    os.makedirs(os.path.join(BASE_DIR, "uploads"), exist_ok=True)


def _ensure_media_packages():
    """Pasang paket media berat (wheel offline + ffmpeg/tesseract) sekali saja,
    saat pertama kali benar-benar dibutuhkan (baca dokumen/media) — bukan di boot.
    Sekali coba saja; gagal pun jangan ulangi di tiap baca."""
    global _MEDIA_PACKAGES_READY
    if _MEDIA_PACKAGES_READY:
        return
    _MEDIA_PACKAGES_READY = True
    try:
        _prepare_media_packages()
    except Exception as exc:
        print(f"[setup] media packages gagal (diabaikan): {_clean_err(exc)[:200]}", flush=True)


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
    if ext in _MEDIA_EXTS:
        _ensure_media_packages()
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

        if name == "web_image_search":
            q = args["query"]
            return f"HASIL PENCARIAN GAMBAR UNTUK '{q}':\n\n{perform_web_image_search(q)}"

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
    ) + _shared_rules_text()


# ---------------------------------------------------------------------------
# Staging: copy HANYA binary llama-server (kecil) supaya bisa dieksekusi.
# File model GGUF dibaca langsung dari /kaggle/input — read-only tidak masalah
# karena model hanya di-mmap (tidak ditulis/dieksekusi). Dulu seluruh folder
# model ikut di-copy (GB-an) di tiap boot; itu yang bikin boot lama.
# ---------------------------------------------------------------------------
def _stage_model_assets(base_dir):
    bin_dst = os.path.join(base_dir, "llama_bin")
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
    src_model = next(iter(glob.glob("/kaggle/input/**/model_gguf", recursive=True)), None)
    if not src_model:
        raise FileNotFoundError("The dataset has no model_gguf folder")
    return bin_dst, src_model


def start_llama_server(base_dir, port, max_ctx):
    bin_dst, model_dir = _stage_model_assets(base_dir)
    srv_matches = glob.glob(f"{bin_dst}/**/llama-server", recursive=True)
    if not srv_matches:
        raise FileNotFoundError(f"Binary llama-server tidak ditemukan di {bin_dst}")
    srv = srv_matches[0]
    d = os.path.dirname(srv)
    model_matches = sorted(glob.glob(f"{model_dir}/*.gguf"))
    model_matches_q4 = [m for m in model_matches if "mmproj" not in m.lower()]
    mmproj_matches = [m for m in model_matches if "mmproj" in m.lower()]
    if not model_matches_q4:
        raise FileNotFoundError(f"File model GGUF tidak ditemukan di {model_dir}")

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


def _warmup_model(api_url, base_dir):
    """Hot reload: prefill system prompt sekali setelah model naik, supaya chat
    pertama tidak membayar prefill dingin. Gagal warmup tidak menggagalkan boot:
    chat pertama tetap jalan, hanya lebih lambat."""
    try:
        payload = {"messages": [{"role": "system", "content": build_system_prompt(base_dir)},
                                 {"role": "user", "content": "hi"}],
                   "max_tokens": 8, "temperature": 0.25, "repeat_penalty": 1.1,
                   "cache_prompt": True, "stream": False}
        req = urllib.request.Request(api_url, data=json.dumps(payload).encode(),
                                      headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=300) as resp:
            resp.read()
        print("[ai-local] warmup selesai: system prompt sudah di-cache, model siap.", flush=True)
    except Exception as exc:
        print(f"[ai-local] warmup gagal (diabaikan, chat tetap jalan): {_clean_err(exc)[:200]}", flush=True)


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


# Label tampilan berpikir/bertindak mengikuti bahasa user (id/en).
# Tambah bahasa baru = tambah satu entri di sini + AGENT_PROCESS_STRINGS di frontend.
AGENT_STRINGS = {
    "id": {
        "tools": {
            "list_dir": "Memeriksa isi folder…",
            "read_file": "Membaca file…",
            "patch_file": "Mengedit file…",
            "write_file": "Menulis file…",
            "run_bash": "Menjalankan perintah…",
            "web_search": "Mencari di web…",
            "web_image_search": "Mencari gambar…",
            "web_fetch": "Mengambil halaman web…",
            "inspect_image": "Menganalisis gambar…",
            "read_document": "Membaca dokumen…",
        },
        "thinking": "Sedang berpikir…",
        "thought_done": "Berpikir",
        "composing": "Menyusun jawaban…",
        "final": "Menyusun jawaban akhir…",
        "final_done": "Menyusun jawaban akhir",
        "model_failed": "Model gagal menjawab",
        "understanding": "Memahami permintaan…",
        "preparing": "Menyiapkan file dan jawaban…",
        "running_tool": "Menjalankan",
        "tokens_used": "Token dipakai",
        "cancelled": "Dibatalkan oleh user.",
    },
    "en": {
        "tools": {
            "list_dir": "Checking folder contents…",
            "read_file": "Reading file…",
            "patch_file": "Editing file…",
            "write_file": "Writing file…",
            "run_bash": "Running command…",
            "web_search": "Searching the web…",
            "web_image_search": "Searching images…",
            "web_fetch": "Fetching web page…",
            "inspect_image": "Analyzing image…",
            "read_document": "Reading document…",
        },
        "thinking": "Thinking…",
        "thought_done": "Thought",
        "composing": "Composing answer…",
        "final": "Composing final answer…",
        "final_done": "Composing final answer",
        "model_failed": "Model failed to answer",
        "understanding": "Understanding request…",
        "preparing": "Preparing files and answer…",
        "running_tool": "Running",
        "tokens_used": "Tokens used",
        "cancelled": "Cancelled by user.",
    },
}


def _agent_strings(language):
    lang = str(language or "").strip().lower()
    if lang.startswith("id"):
        return AGENT_STRINGS["id"]
    if lang.startswith("en"):
        return AGENT_STRINGS["en"]
    return AGENT_STRINGS["id"]


# TIDAK ADA batas angka langkah di sini — berhentinya mengikuti cara otak manusia:
# loop jalan sampai model sendiri memutuskan menjawab. Yang menghentikan paksa
# hanya pola "mentok": mengulang panggilan yang sama persis (hasilnya sudah
# diketahui tidak berubah), atau gagal dengan cara yang sama berulang kali.
# Batas alami yang tersisa: konteks model (131072 token) dan tombol Stop user.
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


def _describe_tool(fn_name, args, language="id"):
    strings = _agent_strings(language)
    label = strings["tools"].get(fn_name, f"{strings['running_tool']} {fn_name}…")
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
    missing = [p for p in changed if p not in answer]
    if not missing:
        return answer
    if len(missing) > 8:
        return (answer + f"\n\n[[file:{_OUTPUT_DIR}]]").strip()
    return (answer + "\n\n" + "\n".join(f"[[file:{p}]]" for p in sorted(missing))).strip()


def _fmt_tokens(n, language):
    # 1.240 (id) vs 1,240 (en).
    grouped = f"{int(n):,}"
    return grouped.replace(",", ".") if str(language or "").startswith("id") else grouped


def _build_context_note(context):
    """Catatan [KONTEKS SAAT INI] di tiap giliran — built by the shared builder
    (see the SHARED block, generated from modules/agent_shared.py), the same
    one the Cloud agent uses for its [CURRENT CONTEXT] note."""
    return build_context_note(context, lang="id", side="worker")


def _llm_call(api_url, history, with_tools=True, on_token=None):
    # repeat_penalty menahan model mengulang kata/kalimat yang sama; tanpa ini
    # suhu rendah (0.25) membuatnya terus memilih token yang sama.
    # cache_prompt: awalan yang sama (system prompt + riwayat) dipakai ulang dari
    # KV cache, bukan dihitung dari nol di tiap panggilan.
    payload = {"messages": history, "max_tokens": -1, "temperature": 0.25,
               "repeat_penalty": 1.1, "cache_prompt": True, "stream": False}
    if with_tools:
        payload["tools"] = TOOLS
        payload["tool_choice"] = "auto"
    if on_token is None:
        req = urllib.request.Request(api_url, data=json.dumps(payload).encode(),
                                      headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=600) as resp:
            body = json.loads(resp.read().decode("utf-8", errors="ignore"))
        message = ((body.get("choices") or [{}])[0].get("message")) or {}
        usage = body.get("usage") or {}
        return message, usage
    # Live stream: baca SSE llama-server token demi token, teruskan tiap delta
    # ke on_token supaya chat tampil sambil jalan, bukan menunggu selesai.
    payload["stream"] = True
    payload["stream_options"] = {"include_usage": True}
    req = urllib.request.Request(api_url, data=json.dumps(payload).encode(),
                                  headers={"Content-Type": "application/json"})
    try:
        content_parts, thought_parts = [], []
        tool_calls: list[dict] = []
        usage: dict = {}
        with urllib.request.urlopen(req, timeout=600) as resp:
            for raw_line in resp:
                line = raw_line.decode("utf-8", errors="ignore").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                choice = (chunk.get("choices") or [{}])[0]
                if isinstance(chunk.get("usage"), dict):
                    usage = chunk["usage"]
                delta = choice.get("delta") or {}
                if isinstance(delta.get("content"), str) and delta["content"]:
                    content_parts.append(delta["content"])
                    try:
                        on_token(delta["content"])
                    except Exception:
                        pass
                if isinstance(delta.get("reasoning_content"), str) and delta["reasoning_content"]:
                    thought_parts.append(delta["reasoning_content"])
                for tc in delta.get("tool_calls") or []:
                    idx = tc.get("index", 0)
                    while len(tool_calls) <= idx:
                        tool_calls.append({"type": "function", "id": "", "function": {"name": "", "arguments": ""}})
                    cur = tool_calls[idx]
                    if tc.get("id"):
                        cur["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        cur["function"]["name"] = fn["name"]
                    if isinstance(fn.get("arguments"), str):
                        cur["function"]["arguments"] += fn["arguments"]
        message: dict = {"role": "assistant", "content": "".join(content_parts)}
        if thought_parts:
            message["reasoning_content"] = "".join(thought_parts)
        if tool_calls:
            message["tool_calls"] = tool_calls
        return message, usage
    except Exception:
        # Stream putus di tengah: ulangi sekali tanpa stream supaya jawaban tetap lengkap.
        req = urllib.request.Request(api_url, data=json.dumps({**payload, "stream": False}).encode(),
                                      headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=600) as resp:
            body = json.loads(resp.read().decode("utf-8", errors="ignore"))
        message = ((body.get("choices") or [{}])[0].get("message")) or {}
        usage = body.get("usage") or {}
        return message, usage


def _fallback_summary(ran_tools):
    # Shared definition (see the SHARED block, generated from agent_shared.py):
    # same summary the Cloud agent falls back to when a turn ends empty.
    return fallback_summary(ran_tools, "id")


def run_agent_turn(history, api_url, base_dir, user_content, on_activity=None, on_step=None, language="id", on_opening=None, cancelled=None, context=None, on_token=None, search_images_out=None):
    strings = _agent_strings(language)
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

    segment = {"n": 0}  # tiap panggilan model = satu segmen stream (sapaan, jawaban akhir, ...)

    def _stream_call(with_tools):
        segment["n"] += 1
        seg = segment["n"]
        if on_token is None:
            return _llm_call(api_url, history, with_tools=with_tools)
        return _llm_call(api_url, history, with_tools=with_tools,
                         on_token=lambda d: on_token(d, seg))

    out_before = _snapshot_out()
    history.append({"role": "user", "content": _build_context_note(context)})
    history.append({"role": "user", "content": user_content})
    # Ingatan kerja, bukan hitungan: panggilan apa yang sudah dicoba dan apa hasilnya,
    # supaya pengulangan bisa dikenali dari polanya, bukan dari angkanya.
    seen_calls = {}       # signature panggilan identik -> hasil terakhirnya
    warned_repeats = set()  # signature yang sudah sekali diperingatkan karena diulang
    ran_tools = []        # (nama tool, detail, ok, preview)
    last_error = None     # signature error terakhir, untuk mengenali jalan buntu yang sama
    empty_retry = False
    stuck_reason = None
    opening_sent = False  # sapaan pembuka dikirim sekali, sebelum tool pertama jalan
    total_tokens = 0       # akumulasi total_tokens dari semua panggilan model di jawaban ini
    prompt_tokens = 0      # akumulasi prompt_tokens (input) dari semua panggilan model
    completion_tokens = 0  # akumulasi completion_tokens (output) dari semua panggilan model

    def _report_tokens():
        total = total_tokens if total_tokens > 0 else prompt_tokens + completion_tokens
        if total <= 0:
            return
        if prompt_tokens <= 0 and completion_tokens <= 0:
            # llama-server tidak mengembalikan rincian input/output: tampilkan total saja seperti dulu.
            title = f"{strings['tokens_used']}: {_fmt_tokens(total, language)}"
        else:
            parts = []
            if prompt_tokens > 0:
                parts.append(f"{_fmt_tokens(prompt_tokens, language)} input")
            if completion_tokens > 0:
                parts.append(f"{_fmt_tokens(completion_tokens, language)} output")
            parts.append(f"{_fmt_tokens(total, language)} total")
            title = f"{strings['tokens_used']}: {' · '.join(parts)}"
        _step(_new_id(), kind="info", title=title, status="done")

    # Loop jalan sampai model sendiri memutuskan menjawab — seperti manusia yang
    # berhenti saat merasa sudah cukup, bukan karena kuota langkah habis.
    while True:
        think_id = _new_id()
        _act(strings["thinking"])
        _step(think_id, kind="thinking", title=strings["thinking"], status="running")
        if cancelled and cancelled():
            # User menekan Berhenti selagi model sedang berpikir.
            history.append({"role": "assistant", "content": strings["cancelled"]})
            _step(_new_id(), kind="info", title=strings["cancelled"], status="done")
            _report_tokens()
            return strings["cancelled"]
        try:
            message, usage = _stream_call(with_tools=True)
            total_tokens += int((usage or {}).get("total_tokens") or 0)
            prompt_tokens += int((usage or {}).get("prompt_tokens") or 0)
            completion_tokens += int((usage or {}).get("completion_tokens") or 0)
        except Exception as exc:
            _step(think_id, status="error", title=strings["model_failed"], output=_short(_clean_err(exc)))
            raise
        visible, thought = _split_thinking(message)
        tool_calls = message.get("tool_calls") or []

        if tool_calls and visible and not opening_sent:
            # Sapa dulu sebelum bertindak: kirim isi jawaban pertama model sebagai
            # pesan sapaan ke user, baru setelah itu tool mulai jalan.
            opening_sent = True
            if on_opening:
                try:
                    on_opening(visible.strip()[:600])
                except Exception:
                    pass

        if not tool_calls:
            _step(think_id, status="done", title=strings["thought_done"], output=_short(thought, 1200))
            if not visible and not empty_retry:
                empty_retry = True
                history.append({"role": "assistant", "content": ""})
                history.append({"role": "user", "content": empty_retry_message("id")})
                continue
            history.append({"role": "assistant", "content": visible})
            _act(strings["composing"])
            _report_tokens()
            return _append_missing_markers(visible or _fallback_summary(ran_tools), out_before)

        # Normalisasi tool_calls supaya riwayat selalu valid (id & arguments string).
        for tc in tool_calls:
            tc.setdefault("type", "function")
            tc["id"] = tc.get("id") or f"call_{uuid.uuid4().hex[:10]}"
            fn = tc.setdefault("function", {})
            if not isinstance(fn.get("arguments"), str):
                fn["arguments"] = json.dumps(fn.get("arguments") or {}, ensure_ascii=False)
        history.append({"role": "assistant", "content": visible, "tool_calls": tool_calls})
        _step(think_id, status="done", title=strings["thought_done"], output=_short(thought or visible, 1200))

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

            label, detail = _describe_tool(fn_name, parsed_args, language)
            sid = _new_id()
            _act(label + detail)
            _step(sid, kind="tool", tool=fn_name, title=label.rstrip("…"), detail=detail.strip(), status="running")

            sig = tool_call_signature(fn_name, parsed_args)
            repeat_after_warning = False
            if parse_error:
                result = invalid_args_message(fn_name, parse_error, "id")
            elif sig in warned_repeats:
                # Sudah sekali diperingatkan tapi tetap mengulang hal yang sama persis:
                # otaknya muter, sudahi putaran ini dengan jawaban akhir.
                repeat_after_warning = True
                result = repeat_stop_message(fn_name, "id")
            elif sig in seen_calls:
                # Pertama kali mengulang: ingatkan bahwa hasilnya sudah diketahui,
                # jangan jalankan ulang — kasih hasil lamanya supaya model bisa lanjut
                # dengan cara lain, atau langsung menjawab.
                warned_repeats.add(sig)
                result = repeat_warning(seen_calls[sig], "id")
            else:
                result = exec_tool(fn_name, parsed_args, base_dir=base_dir)
                seen_calls[sig] = str(result)
                # Pencarian gambar: kumpulkan thumbnail-nya untuk strip gambar
                # di chat (dikirim balik lewat result_body, bukan ditempel di teks).
                if fn_name == "web_image_search" and search_images_out is not None:
                    for url in search_image_urls(str(result)):
                        if url not in search_images_out and len(search_images_out) < 6:
                            search_images_out.append(url)

            if isinstance(result, dict) and "__image_payload__" in result:
                img_fp, q_img = result["__image_payload__"], result["question"]
                text_result = f"Gambar '{img_fp}' dimuat untuk inspeksi visual."
                history.append({"role": "tool", "tool_call_id": tc["id"], "name": fn_name, "content": text_result})
                pending_images.append((img_fp, q_img))
                ok = True
            else:
                text_result = clip_tool_result(str(result), "id")
                history.append({"role": "tool", "tool_call_id": tc["id"], "name": fn_name, "content": text_result})
                ok = not is_error_result(text_result)

            _step(sid, status="done" if ok else "error", output=_short(text_result))
            ran_tools.append((fn_name, detail, ok, text_result))
            if repeat_after_warning:
                stuck_reason = f"panggilan '{fn_name}' yang sama diulang setelah diperingatkan"
            elif not ok:
                # Gagal itu wajar — yang menandakan mentok adalah gagal dengan CARA YANG SAMA.
                # Selama errornya berubah-ubah, berarti model sedang mencoba metode lain.
                err_sig = error_signature(fn_name, text_result)
                if err_sig == last_error:
                    stuck_reason = f"tool '{fn_name}' gagal dengan cara yang sama berulang kali"
                else:
                    last_error = err_sig
            else:
                last_error = None

        for img_fp, q_img in pending_images:
            history.append({"role": "user", "content": [
                {"type": "text", "text": f"[Inspeksi Visual: {img_fp}] {q_img}"},
                {"type": "image_url", "image_url": {"url": encode_image_b64(img_fp)}},
            ]})

        if cancelled and cancelled():
            history.append({"role": "assistant", "content": strings["cancelled"]})
            _step(_new_id(), kind="info", title=strings["cancelled"], status="done")
            _report_tokens()
            return strings["cancelled"]
        if stuck_reason:
            break

    # Mentok (mengulang hal yang sama / gagal dengan cara yang sama): minta jawaban
    # akhir TANPA tool, jangan berhenti dengan pesan kosong.
    final_id = _new_id()
    _act(strings["final"])
    _step(final_id, kind="thinking", title=strings["final"], status="running")
    history.append({"role": "user", "content": stuck_final_message(stuck_reason, "id")})
    try:
        final_message, final_usage = _stream_call(with_tools=False)
        total_tokens += int((final_usage or {}).get("total_tokens") or 0)
        prompt_tokens += int((final_usage or {}).get("prompt_tokens") or 0)
        completion_tokens += int((final_usage or {}).get("completion_tokens") or 0)
        final_visible, _ = _split_thinking(final_message)
    except Exception:
        final_visible = ""
    if not final_visible:
        final_visible = _fallback_summary(ran_tools)
    history.append({"role": "assistant", "content": final_visible})
    _step(final_id, status="done", title=strings["final_done"])
    _report_tokens()
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
    _cancel_event = threading.Event()  # Berhenti: batalkan jawaban berjalan, worker tetap hidup
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
            if isinstance(res, dict) and res.get("control") == "cancel":
                _cancel_event.set()
            if gone["count"] >= 6:
                # Backend tidak mengenal sesi ini lagi (worker yatim / sesi diganti): jangan buang kuota GPU.
                _shutdown_now("sesi backend hilang")
            if _stop.wait(8):
                break

    # Stop saat kernel masih antre: kalau backend sudah tidak mengenal sesi ini
    # (user menekan Stop selagi kernel antre), jangan buang kuota GPU dengan
    # memuat paket dan model. Mati sekarang, sebelum heartbeat pertama.
    # Gangguan jaringan sesaat tidak menghentikan boot: heartbeat yang akan
    # menangkap sesi yang benar-benar sudah hilang.
    try:
        cfg = requests.get(
            f"{backend_url}/agent/local/kaggle-config",
            params={"session_id": session_id},
            headers={"X-Kaggle-Worker-Token": worker_token},
            timeout=10,
        )
        if cfg.status_code in (401, 410):
            print("[ai-local] sesi backend sudah tidak ada (stop saat antre), mematikan worker.", flush=True)
            return
    except Exception:
        pass

    # Tanpa batas idle: worker hidup selama Kaggle membiarkan kernel jalan.
    # Matinya hanya kalau user menekan Stop, sesi backend hilang, error,
    # atau Kaggle sendiri yang mengakhiri kernel.
    base_dir = BASE_DIR
    os.makedirs(base_dir, exist_ok=True)
    proc = None
    log_file = None
    hb_thread = threading.Thread(target=_heartbeat_loop, daemon=True)
    hb_thread.start()
    try:
        _status("booting", phase="worker")
        _status("booting", phase="packages")
        _prepare_work_dirs()  # paket berat dipasang lazy saat pertama baca dokumen/media
        _status("booting", phase="model")
        proc, log_file, model_id = start_llama_server(base_dir, DEFAULT_PORT, DEFAULT_MAX_CTX)
        state["proc"] = proc
        if not wait_ready(proc, DEFAULT_PORT):
            tail = ""
            if os.path.exists("/tmp/server.log"):
                with open("/tmp/server.log", "r", errors="ignore") as lf:
                    tail = lf.read()[-800:]
            raise RuntimeError(f"llama-server gagal start: {tail}")
        api_url = f"http://127.0.0.1:{DEFAULT_PORT}/v1/chat/completions"
        _warmup_model(api_url, base_dir)
        _status("ready", model_id=model_id)
        print(f"[ai-local] ready, model={model_id}", flush=True)

        history = [{"role": "system", "content": build_system_prompt(base_dir)}]
        print("[ai-local] worker hidup sampai Stop ditekan atau Kaggle mengakhiri kernel.", flush=True)

        while True:
            job_res = _post("/agent/local/kaggle-poll", {"closing": False, "heartbeat_only": False}, timeout=10)
            job = job_res.get("job") if isinstance(job_res, dict) else None
            control = job_res.get("control") if isinstance(job_res, dict) else None
            if control == "stop":
                # Stop harus bekerja walau thread heartbeat sedang tertahan:
                # loop utama juga melihat perintah stop di tiap poll-nya.
                print("[ai-local] stop diminta user.", flush=True)
                _post("/agent/local/kaggle-poll", {"closing": True, "heartbeat_only": False, "reason": "stop"}, timeout=10)
                break
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
                _warmup_model(api_url, base_dir)
                history = [{"role": "system", "content": build_system_prompt(base_dir)}]
                _status("ready", model_id=model_id)
                continue
            if gone["count"] >= 6:
                # Backend sudah tidak mengenal sesi ini (restart / user menjalankan ulang worker baru).
                # Berhenti supaya GPU Kaggle tidak terbuang sia-sia.
                print("[ai-local] sesi backend hilang, mematikan worker.", flush=True)
                break
            if not job:
                time.sleep(2.5)
                continue
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
                    def _report_opening(text):
                        try:
                            _post("/agent/local/kaggle-activity", {"opening": (text or "").strip()[:600]}, timeout=5)
                        except Exception:
                            pass
                    # Live stream: teruskan token ke backend (di-throttle supaya tidak
                    # membanjiri HTTP: flush tiap ~150ms atau tiap 400 karakter).
                    # Segmen = satu panggilan model; ganti segmen -> flush sisa segmen lama.
                    _stream_state = {"buf": "", "seg": 0, "last_flush": 0.0}
                    def _report_stream(delta, segment):
                        try:
                            st = _stream_state
                            if segment != st["seg"]:
                                if st["buf"]:
                                    _post("/agent/local/kaggle-stream", {"job_id": job_id, "segment": st["seg"], "delta": st["buf"]}, timeout=5)
                                st["buf"] = ""
                                st["seg"] = segment
                            st["buf"] += delta or ""
                            now = time.time()
                            if len(st["buf"]) >= 400 or now - st["last_flush"] >= 0.15:
                                _post("/agent/local/kaggle-stream", {"job_id": job_id, "segment": segment, "delta": st["buf"]}, timeout=5)
                                st["buf"] = ""
                                st["last_flush"] = now
                        except Exception:
                            pass
                    job_strings = _agent_strings(payload.get("language"))
                    # Riwayat bersama: sinkronisasi history worker dari percakapan
                    # yang dilihat user (cloud + lokal), supaya lokal paham apa yang
                    # cloud kerjakan dan sebaliknya. Detail tool-call di dalam satu
                    # giliran tetap tercatat selama giliran itu; giliran berikut
                    # sinkron lagi dari riwayat bersama.
                    shared = payload.get("history") or []
                    if isinstance(shared, list) and shared:
                        synced = [{"role": "system", "content": build_system_prompt(base_dir)}]
                        for item in shared[-40:]:
                            if not isinstance(item, dict):
                                continue
                            role = item.get("role")
                            shared_content = item.get("content")
                            if role in ("user", "assistant") and isinstance(shared_content, str) and shared_content.strip():
                                synced.append({"role": role, "content": shared_content[:8000]})
                        history = synced
                    _post("/agent/local/kaggle-activity", {"activity": job_strings["understanding"], "reset": True}, timeout=5)
                    _cancel_event.clear()
                    search_images: list[str] = []
                    answer = run_agent_turn(history, api_url, base_dir, content,
                                            on_activity=lambda _m: None, on_step=_report_step,
                                            language=payload.get("language"), on_opening=_report_opening,
                                            cancelled=_cancel_event.is_set, context=payload.get("context"),
                                            on_token=_report_stream, search_images_out=search_images)
                    if _stream_state["buf"]:
                        try:
                            _post("/agent/local/kaggle-stream", {"job_id": job_id, "segment": _stream_state["seg"], "delta": _stream_state["buf"]}, timeout=5)
                        except Exception:
                            pass
                    _report_activity(job_strings["preparing"])
                    cleaned, attachments = extract_and_deliver(answer or "", session_id)
                    try:
                        _post("/agent/local/kaggle-activity", {"activity": None}, timeout=5)
                    except Exception:
                        pass
                    result_body = {"content": cleaned}
                    if attachments:
                        result_body["attachments"] = attachments
                    if search_images:
                        result_body["search_images"] = search_images
                    _post("/agent/local/kaggle-result", {"job_id": job_id, "ok": True, "result": result_body})
                elif job_type == "workspace":
                    out = run_workspace_job(payload.get("op"), payload.get("op_payload") or {}, base_dir)
                    _post("/agent/local/kaggle-result", {"job_id": job_id, "ok": out.get("ok", False), "result": out})
                elif job_type == "tool":
                    # Satu tool call dari model cloud: eksekusi langsung, tanpa
                    # giliran agen. Tidak masuk ke history percakapan worker —
                    # cloud yang memegang pembahasannya dan menerima hasilnya.
                    result_text = exec_tool(payload.get("name") or "", payload.get("args") or {}, base_dir)
                    if isinstance(result_text, dict) and "__image_payload__" in result_text:
                        # inspect_image: hand the cloud model the image itself
                        # (base64), not the dict repr — otherwise the image never
                        # reaches it. run_kaggle_tool passes the image through.
                        img_fp = result_text["__image_payload__"]
                        img_q = result_text.get("question") or ""
                        try:
                            img_size = os.path.getsize(img_fp)
                        except OSError:
                            img_size = 0
                        if img_size > 3 * 1024 * 1024:
                            _post("/agent/local/kaggle-result", {"job_id": job_id, "ok": True, "result": {"content": f"Error: image '{img_fp}' is too large for cloud inspection ({img_size // 1024 // 1024} MB, limit 3 MB)."}})
                        else:
                            _post("/agent/local/kaggle-result", {"job_id": job_id, "ok": True, "result": {"content": f"Image '{img_fp}' loaded for visual inspection.", "image": {"url": encode_image_b64(img_fp), "question": img_q, "path": img_fp}}})
                    else:
                        _post("/agent/local/kaggle-result", {"job_id": job_id, "ok": True, "result": {"content": result_text}})
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
