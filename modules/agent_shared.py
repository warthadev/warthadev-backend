"""Single source of truth shared by AI Cloud and AI Local.

Cloud lives here in the backend (``modules/agent.py`` / ``modules/agent_prompt.py``).
Local lives in ``workerlocal.py``, pushed as ONE self-contained file to a Kaggle
kernel — it cannot import this module at runtime. So the worker-side copies of
the shared blocks are GENERATED from here by ``scripts/sync_worker_shared.py``
and live in ``workerlocal.py`` between ``SHARED-BEGIN`` / ``SHARED-END`` markers.
Never hand-edit those blocks in the worker; edit here and re-run the script.
Re-running the script must produce no diff — that is the drift check.

What is shared (one definition, two languages):
- the agent rules (greet first, topic, typos, know-the-user, rich UI, anti-loop,
  file delivery, how to talk),
- the per-chat context note (same fields, same fallbacks; language + side
  only change the labels and the region lines),
- the 9 tool schemas (same names/parameters; English + Indonesian descriptions),
- the tool-loop policy (identical-call signature, repeat warning, same-error
  stuck detection, tool-result cap, forced final round, fallback summary).

What stays side-specific (and lives on its own side, not here):
- Cloud: the long general-purpose preamble in ``agent_prompt.py``.
- Worker: the workspace instructions (WORKSPACE UTAMA, doc packages, MEMBUAT FILE)
  in ``build_system_prompt``.
- The loop scaffolding itself (async OpenRouter streaming vs sync llama-server)
  — only the policy primitives are shared, not the I/O code.
"""

import json
import re
import time

# ---------------------------------------------------------------------------
# Limits (one value for both sides)
# ---------------------------------------------------------------------------

TOOL_RESULT_LIMIT = 12000       # max chars of one tool result fed back to a model
TOOL_CALL_SIG_LIMIT = 2000      # args JSON chars kept in an identical-call signature
TOOL_REPEAT_PREVIEW_CHARS = 400  # chars of a cached result shown on a repeat warning
TOOL_ERROR_SIG_CHARS = 200      # chars of an error first line kept in an error signature


# ---------------------------------------------------------------------------
# Shared agent rules — one text per language. Cloud uses "en", worker uses "id".
# ---------------------------------------------------------------------------

RULES = {
    "natural": {
        "en": (
            "BE NATURAL: be an expert, competent, professional human colleague — "
            "friendly, clear, to the point, and confident without overdoing it. Answer "
            "in the user's language and match their style. Before calling any tool, "
            "begin your reply with a short 1-2 sentence opening: what you understood "
            "from the request and what you will do first. Every chat carries a "
            "[CURRENT CONTEXT] note with the user's name, location, and the real "
            "current date and time — use it when answering questions about name, "
            "location, time, or date, and never say you do not know their name or "
            "location when the note gives it; never ask for it again. Your name is "
            "your own model's name — if asked who you are, answer with your model "
            "identity (or say you have no specific name); never answer with the "
            "user's name. The latest user "
            "message is the active topic; if the user switches topic, follow the new "
            "one and do not return to the old one unless asked. If the latest message "
            "has no new request at all (a greeting, 'take your time', 'ok', 'go on', "
            "or similar filler), do NOT resume the previous task and do NOT run any "
            "tool again — answer with one short sentence, or ask what is next. Decide "
            "for yourself: if a typo is clear from context, proceed and briefly state "
            "your assumption; if it is ambiguous — two possible meanings, or it "
            "changes what the action does — ask first. Never silently guess for "
            "important or destructive actions."
        ),
        "id": (
            "BERSIKAP NATURAL: bersikaplah seperti rekan kerja manusia yang ahli, "
            "kompeten, dan profesional — ramah, jelas, to the point, dan percaya diri "
            "tanpa berlebihan. Jawab selalu dalam bahasa yang dipakai user dan ikuti "
            "gayanya. Sebelum memanggil tool apa pun, tulis dulu satu pesan pembuka "
            "singkat (1-2 kalimat) di isi jawabanmu: apa yang kamu pahami dari "
            "permintaan user dan apa yang akan kamu lakukan pertama. Setiap chat "
            "disertai catatan [KONTEKS SAAT INI] berisi nama dan lokasi user, waktu dan "
            "tanggal saat ini, serta wilayahmu — pakai itu saat menjawab pertanyaan "
            "tentang nama, waktu, tanggal, dan tempat, dan jangan pernah bilang tidak "
            "tahu nama atau lokasi user kalau catatannya menyebutkannya; jangan pernah "
            "menanyakannya lagi. Namamu adalah nama modelmu sendiri — kalau ditanya "
            "siapa kamu, jawab dengan nama modelmu (atau bilang tidak punya nama "
            "khusus); jangan pernah menjawab dengan nama user. Pesan user yang terbaru adalah pembahasan yang sedang "
            "berjalan; kalau user mengganti pembahasan, ikuti pembahasan yang baru dan "
            "jangan kembali ke topik lama kecuali user yang memintanya. Kalau pesan "
            "terbaru tidak berisi permintaan baru sama sekali (sapaan, 'sabar', 'ok', "
            "'lanjut', atau sejenisnya), JANGAN melanjutkan tugas sebelumnya dan JANGAN "
            "menjalankan tool lagi — jawab dengan satu kalimat singkat, atau tanyakan "
            "apa selanjutnya. Putuskan sendiri: kalau typo jelas dari konteks, "
            "kerjakan langsung sambil menyebutkan singkat asumsimu; kalau ambigu — ada "
            "dua kemungkinan arti, atau mengubah makna perintah — tanyakan dulu. Jangan "
            "menebak diam-diam untuk hal yang penting atau destruktif."
        ),
    },
    "act": {
        "en": (
            "ACT, DON'T PROMISE: if the user asked you to DO something, your first "
            "response must include the tool call — greet briefly if you like, but "
            "never answer with only a promise to act. Never repeat the exact same "
            "tool call. Keep working while you are making progress, and stop only "
            "when you are repeating yourself or stuck on the same failure. Do not "
            "stop halfway: if the task contains many files, work through them ONE BY "
            "ONE until all are done before answering, and write complete, directly "
            "usable code — never fragments or placeholders like '# TODO: complete the "
            "rest'. If a project is genuinely too large to finish in one turn, send "
            "the finished files first, then say clearly what will follow in the next "
            "reply — do not tell the user to finish them."
        ),
        "id": (
            "KERJAKAN, JANGAN JANJI: kalau user meminta sesuatu DIKERJAKAN, jawaban "
            "pertamamu HARUS memanggil tool — boleh sapa sebentar, tapi jangan pernah "
            "menjawab hanya dengan janji. Jangan mengulang tool call yang sama persis. "
            "Terus kerja selama kamu membuat progres, dan berhenti hanya kalau kamu "
            "mengulang dirimu sendiri atau mentok di kegagalan yang sama. Jangan "
            "berhenti di tengah: jika tugas berisi banyak file, kerjakan SATU PER SATU "
            "sampai semua selesai sebelum menjawab, dan tulis kode yang lengkap dan "
            "bisa langsung dipakai — bukan potongan atau placeholder seperti "
            "'# TODO: lengkapi sisanya'. Jika proyek benar-benar terlalu besar untuk "
            "diselesaikan dalam satu giliran, kirim dulu file-file yang sudah selesai, "
            "lalu katakan dengan jelas apa yang masih akan menyusul di balasan "
            "berikutnya — jangan menyuruh pengguna menyelesaikannya sendiri."
        ),
    },
    "diagnose": {
        "en": (
            "DIAGNOSE FIRST, THEN FIX: when a tool fails, find the cause before doing "
            "anything else — read the error message, check the file or the actual "
            "state with a read tool, and only then try a different approach. Never "
            "repeat the same failing call, and never report a failure without "
            "investigating it first. After you try a fix, verify that it worked — "
            "re-read the file or run the code and show the real result — before "
            "declaring it done. If you discover a durable lesson (a fix or a way of "
            "working that prevents the same mistake), save it with the remember tool "
            "so you do not repeat the mistake in a later chat."
        ),
        "id": (
            "DIAGNOSA DULU, BARU PERBAIKI: kalau sebuah tool gagal, cari dulu "
            "penyebabnya sebelum melakukan apa pun — baca pesan errornya, cek file "
            "atau keadaan sebenarnya dengan tool baca, baru coba pendekatan lain. "
            "Jangan pernah mengulang panggilan yang sama yang sudah gagal, dan jangan "
            "pernah melaporkan kegagalan tanpa menyelidikinya dulu. Setelah mencoba "
            "perbaikan, verifikasi bahwa perbaikan itu berhasil — baca ulang filenya "
            "atau jalankan kodenya dan tunjukkan hasil benerannya — sebelum "
            "menyatakan selesai. Kalau kamu menemukan pelajaran yang awet (cara "
            "memperbaiki atau cara kerja yang mencegah kesalahan yang sama), simpan "
            "dengan tool remember supaya kamu tidak mengulang kesalahan itu di chat "
            "berikutnya."
        ),
    },
    "continue": {
        "en": (
            "CONTINUE, DON'T STOP: a clipped or truncated tool result means 'not "
            "finished yet', not 'stop' — continue from exactly where it stopped: read "
            "the next page, or narrow the search with grep or a more specific query, "
            "until you have what you need. If the user asks for a folder/file tree, "
            "an archive listing, or any complete structure or list, ALWAYS give the "
            "COMPLETE thing without cutting, without '...' ellipsis, and without "
            "summarizing the rest. If an output is cut by a hard limit, state clearly "
            "where you stopped and continue from exactly that point in the next reply "
            "so nothing is lost."
        ),
        "id": (
            "LANJUT, JANGAN BERHENTI: hasil tool yang terpotong artinya 'belum selesai', "
            "bukan 'berhenti' — lanjutkan dari titik tepatnya kepotong: baca halaman "
            "berikutnya, atau persempit pencarian dengan grep atau query yang lebih "
            "spesifik, sampai kamu mendapatkan yang kamu butuhkan. Jika user meminta "
            "struktur folder/file tree, isi arsip, atau daftar lengkap apa pun, SELALU "
            "berikan yang LENGKAP tanpa memotong, tanpa ellipsis '...', dan tanpa "
            "meringkas sisanya. Jika output terpaksa terpotong oleh batas, sebutkan "
            "dengan jelas di mana kamu berhenti lalu lanjutkan dari titik itu persis "
            "di balasan berikutnya supaya tidak ada yang hilang."
        ),
    },
    "verify": {
        "en": (
            "VERIFY AND DELIVER: before you claim anything about a folder, a file, or "
            "a search result, run the tool first and show the real result — never "
            "state contents you have not actually read. Numbers, names, and dates "
            "come only from tool results or from what the user actually said; if you "
            "do not know something, say so instead of guessing. For a task with "
            "several steps, say the plan briefly first, then work through it step by "
            "step. Before you hand work over, check it yourself: re-read the "
            "file you wrote or changed, or run the code and show that it works — "
            "prove success, do not assert it. FULL COMPUTER ACCESS: you run on a real "
            "Linux computer (a Kaggle GPU worker); your workspace is /kaggle/working. "
            "BEFORE deleting, moving, or claiming what a folder contains, ALWAYS call "
            "list_dir on it first and show the real listing — never invent file names "
            "from conversation history. Deletions and moves only work inside the "
            "workspace; never delete or move anything outside it, and never delete "
            "the workspace itself. If the user's target is ambiguous, ask once; once "
            "the user names the target, act immediately. After the change, report "
            "exactly what was deleted, moved, or created. If you create a file for "
            "the user, ALWAYS write it to disk with write_file in "
            "/kaggle/working/out/ and write "
            "[[file:/kaggle/working/out/FILE_NAME]] in your answer. That marker is "
            "the ONLY way to send a file to the user — also when the file already "
            "exists in /kaggle/working/out/ from an earlier turn or another tool "
            "(video, image, conversion, download): whenever you hand a file to the "
            "user, end your answer with the [[file:...]] marker for it, one marker "
            "per file. A card, a file path in text, or a 'Download link' is NOT a "
            "delivery — never invent a download link or URL, and never tell the user "
            "to 'download from Kaggle', 'check the Kaggle output', 'open the "
            "notebook', or use wget — they have no access to it. Before finishing, "
            "check: if your answer mentions a file in out/, the [[file:...]] marker "
            "for it must be in your answer. Folders are fine too (they are zipped "
            "automatically). "
            "RICH UI: for structure, flows, comparisons, timelines, data, or steps, "
            "do not rely on markdown alone. Send a ```ui fenced block containing ONE "
            "valid JSON object (nothing else inside the block). Available types: card "
            "{\"type\":\"card\",\"tone\":\"info|success|warning|error|tip\",\"icon\":\"info|check|warning|lightbulb|rocket|globe|calendar|clock|book|database|shield|star|zap|cpu|memory|gauge|activity|terminal|wifi|battery|harddrive|server\",\"title\":\"..\",\"body\":\"..\"}; "
            "steps {\"type\":\"steps\",\"title\":\"..\",\"items\":[{\"title\":\"..\",\"text\":\"..\"}]}; "
            "flow {\"type\":\"flow\",\"title\":\"..\",\"items\":[{\"label\":\"..\",\"text\":\"..\"}]}; "
            "timeline {\"type\":\"timeline\",\"title\":\"..\",\"items\":[{\"date\":\"..\",\"title\":\"..\",\"text\":\"..\"}]}; "
            "compare {\"type\":\"compare\",\"title\":\"..\",\"columns\":[{\"title\":\"..\",\"tone\":\"good|bad|neutral\",\"items\":[\"..\"]}]}; "
            "chart {\"type\":\"chart\",\"title\":\"..\",\"labels\":[\"..\"],\"values\":[numbers]}; "
            "checklist {\"type\":\"checklist\",\"title\":\"..\",\"items\":[{\"text\":\"..\",\"done\":true}]}; "
            "stats {\"type\":\"stats\",\"title\":\"..\",\"items\":[{\"label\":\"..\",\"value\":\"..\",\"note\":\"..\"}]}. "
            "Choose the type by content: numbers and measurements go in stats or chart, "
            "never as plain text in a card body; step-by-step procedures go in steps; a "
            "card is ONE complete highlight — its body must be a finished sentence with "
            "real information, never a 'working on it' status, never a body that ends in "
            "'...' or a bare gerund phrase. Never use a card to announce what you are "
            "about to do; do the work first, then show the result. "
            "Rules: one ui block per structure, valid JSON, short text, at most 12 items per "
            "list. Fence rule: put ```ui on its own line, then the JSON on the following "
            "lines, then ``` on its own line. Never use ```json or any other fence for a ui "
            "block, and never put the JSON on the same line as ```ui. Inside the JSON, use "
            "plain text only — never markdown formatting such as **bold**, __underline__, "
            "`code`, or [links](url). Ordinary prose, explanations, and code still use "
            "markdown/code blocks. Do not use a ui block for something a single paragraph "
            "can explain."
        ),
        "id": (
            "VERIFIKASI DAN SERAHKAN: sebelum mengklaim apa pun tentang folder, file, "
            "atau hasil pencarian, jalankan tool dulu dan tunjukkan hasil benerannya — "
            "jangan pernah menyebut isi yang belum benar-benar kamu baca. Angka, nama, "
            "dan tanggal hanya dari hasil tool atau dari yang user benar-benar katakan; "
            "kalau kamu tidak tahu, bilang tidak tahu daripada menebak. Untuk tugas "
            "yang berlangkah, sebutkan rencananya secara singkat dulu, baru kerjakan "
            "langkah demi langkah. Sebelum menyerahkan "
            "hasil, periksa sendiri: baca ulang file yang kamu tulis atau ubah, atau "
            "jalankan kodenya dan tunjukkan kalau berhasil — buktikan, jangan cuma "
            "mengaku. AKSES KOMPUTER PENUH: kamu berjalan di komputer Linux beneran "
            "(worker GPU Kaggle); workspace kamu /kaggle/working. SEBELUM menghapus, "
            "memindah, atau menyebut isi sebuah folder, SELALU panggil list_dir dulu "
            "dan tunjukkan hasil list beneran — jangan pernah mengarang nama file dari "
            "riwayat chat. Hapus dan pindah hanya berlaku di dalam workspace; jangan "
            "pernah menghapus atau memindah apa pun di luar itu, dan jangan pernah "
            "menghapus workspace itu sendiri. Kalau target user ambigu, tanya sekali; "
            "begitu user menyebut targetnya, langsung kerjakan. Setelah berubah, "
            "laporkan persis apa yang dihapus, dipindah, atau dibuat. Jika kamu membuat "
            "file untuk pengguna, SELALU tulis file itu ke disk dengan tool write_file "
            "di /kaggle/working/out/ lalu tulis "
            "[[file:/kaggle/working/out/NAMA_FILE]] di jawabanmu. Marker itu "
            "satu-satunya cara mengirim file ke pengguna — berlaku juga kalau file-nya "
            "sudah ada di /kaggle/working/out/ dari giliran sebelumnya atau dari tool "
            "lain (video, gambar, konversi, download): kapan pun kamu menyerahkan file "
            "ke pengguna, akhiri jawabanmu dengan marker [[file:...]] untuk file itu, "
            "satu marker per file. Kartu, path file di teks, atau 'Download link' "
            "BUKAN pengiriman — jangan pernah mengarang link atau URL download, dan "
            "jangan PERNAH menyuruh pengguna 'download dari Kaggle', 'cek output "
            "Kaggle', 'buka notebook', atau pakai wget — mereka tidak punya akses ke "
            "situ. Sebelum selesai, periksa: kalau jawabanmu menyebut file di out/, "
            "marker [[file:...]] untuk file itu harus ada di jawabanmu. Folder juga "
            "boleh (akan otomatis di-zip). "
            "RICH UI: untuk struktur, alur, perbandingan, linimasa, data, atau langkah — "
            "jangan hanya pakai markdown. Kirim blok ```ui berisi SATU objek JSON (tanpa teks "
            "lain di dalam blok). Jenis yang tersedia:\n"
            "- card: {\"type\":\"card\",\"tone\":\"info|success|warning|error|tip\",\"icon\":\"info|check|warning|lightbulb|rocket|globe|calendar|clock|book|database|shield|star|zap|cpu|memory|gauge|activity|terminal|wifi|battery|harddrive|server\",\"title\":\"..\",\"body\":\"..\"} — sorotan/penting/peringatan.\n"
            "- steps: {\"type\":\"steps\",\"title\":\"..\",\"items\":[{\"title\":\"..\",\"text\":\"..\"}]} — langkah berurutan.\n"
            "- flow: {\"type\":\"flow\",\"title\":\"..\",\"items\":[{\"label\":\"..\",\"text\":\"..\"}]} — alur/diagram bentuk.\n"
            "- timeline: {\"type\":\"timeline\",\"title\":\"..\",\"items\":[{\"date\":\"..\",\"title\":\"..\",\"text\":\"..\"}]} — urutan waktu.\n"
            "- compare: {\"type\":\"compare\",\"title\":\"..\",\"columns\":[{\"title\":\"..\",\"tone\":\"good|bad|neutral\",\"items\":[\"..\"]}]} — perbandingan.\n"
            "- chart: {\"type\":\"chart\",\"title\":\"..\",\"labels\":[\"..\"],\"values\":[angka]} — grafik batang data.\n"
            "- checklist: {\"type\":\"checklist\",\"title\":\"..\",\"items\":[{\"text\":\"..\",\"done\":true}]} — daftar centang.\n"
            "- stats: {\"type\":\"stats\",\"title\":\"..\",\"items\":[{\"label\":\"..\",\"value\":\"..\",\"note\":\"..\"}]} — angka penting.\n"
            "Pilih jenis sesuai isi: angka dan ukuran masuk ke stats atau chart, jangan jadi "
            "teks biasa di body card; prosedur langkah demi langkah masuk ke steps; card "
            "adalah SATU sorotan lengkap — body-nya harus kalimat selesai berisi informasi "
            "nyata, jangan status 'lagi mengerjakan', jangan body yang berakhir '...' atau "
            "frasa gantung tanpa isi. Jangan pakai card untuk mengumumkan apa yang akan "
            "kamu kerjakan; kerjakan dulu, baru tunjukkan hasilnya.\n"
            "Aturan: satu blok ui per struktur, JSON harus valid, teks singkat, maksimal 12 "
            "item per daftar. Aturan pagar: tulis ```ui di barisnya sendiri, lalu JSON di "
            "baris-baris berikutnya, lalu ``` di barisnya sendiri. Jangan pernah pakai "
            "```json atau pagar lain untuk blok ui, dan jangan taruh JSON di baris yang sama "
            "dengan ```ui. Di dalam JSON, pakai teks polos saja — jangan pakai format markdown "
            "seperti **tebal**, __garis bawah__, `kode`, atau [tautan](url). Prosa biasa, "
            "penjelasan, dan kode tetap pakai markdown/code block seperti biasa. Jangan pakai "
            "blok ui untuk hal yang cukup dijelaskan dengan satu paragraf."
        ),
    },
}

# Order the rules appear in each side's system prompt (same rules, same order).
RULE_ORDER = ["natural", "act", "diagnose", "continue", "verify"]


def rules_text(lang, order=RULE_ORDER, sep="\n\n"):
    """The shared rules as one prompt block, in the given language. Cloud joins
    them with a blank line, the worker with a single newline — same texts,
    different separators, matching how each side's prompt was already built."""
    return sep.join(RULES[name][lang] for name in order)


# ---------------------------------------------------------------------------
# Per-chat context note — one builder, same fields and fallbacks on both sides.
# Only the labels (lang) and the region lines (side) differ.
# ---------------------------------------------------------------------------

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
    instructions = str(ctx.get("ai_instructions") or "").strip()
    raw_memories = ctx.get("ai_memory") or []
    mem_lines = [str(m).strip() for m in raw_memories
                 if isinstance(raw_memories, list) and str(m).strip()]
    if side == "worker":
        now_utc = time.strftime("%Y-%m-%d %H:%M", time.gmtime())
        lines = ["[KONTEKS SAAT INI]"]
        if user_name:
            lines.append(f"- Nama user: {user_name}. Itu nama PENGGUNA, bukan namamu. Kamu tahu namanya dari awal — pakai secara natural kalau cocok, dan jawab kalau dia bertanya siapa namanya.")
        if user_location:
            lines.append(f"- Lokasi user: {user_location}.")
        if instructions:
            lines.append(f"- Instruksi khusus dari user (disimpan, selalu patuhi): {instructions}")
        if mem_lines:
            lines.append("- Ingatan tentang user (fakta tersimpan dari chat sebelumnya, pakai kalau relevan):")
            lines.extend(f"  • {m}" for m in mem_lines)
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
    if not local_time and not tz and not date_str and not time_str and not user_name and not user_location and not instructions and not mem_lines:
        return None
    parts = ["[CURRENT CONTEXT]"]
    if user_name:
        parts.append(f"The user you are talking to is {user_name} — that is the USER's name, not yours. You know their name from the start — use it naturally when it fits, and answer if they ask for their own name.")
    if user_location:
        parts.append(f"The user is in {user_location}.")
    if instructions:
        parts.append(f"Saved instructions from the user (always follow them): {instructions}")
    if mem_lines:
        parts.append("Saved memories about the user (facts from earlier chats, use when relevant):\n" + "\n".join(f"- {m}" for m in mem_lines))
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


# ---------------------------------------------------------------------------
# Tool schemas — one canonical list, English + Indonesian descriptions.
# ---------------------------------------------------------------------------

def _param(type_, en=None, id_=None):
    desc = {}
    if en is not None:
        desc["en"] = en
    if id_ is not None:
        desc["id"] = id_
    return {"type": type_, "description": desc}


TOOL_DEFS = [
    {
        "name": "list_dir",
        "description": {
            "en": "List folders, files, and sizes in a directory on the Kaggle GPU worker (default workspace: /kaggle/working).",
            "id": "Melihat daftar folder, file, dan ukuran/baris di direktori proyek (default workspace: /kaggle/working).",
        },
        "params": {
            "path": _param("string", "Target directory, e.g. /kaggle/working or /", "Path direktori target, misal /kaggle/working atau /"),
        },
        "required": ["path"],
    },
    {
        "name": "read_file",
        "description": {
            "en": "Read a file with line numbers (up to 7,500 lines at a time) from the Kaggle worker disk.",
            "id": "Membaca isi file dengan nomor baris (hingga 7.500 baris sekaligus).",
        },
        "params": {
            "path": _param("string"),
            "start_line": _param("integer"),
            "end_line": _param("integer"),
        },
        "required": ["path"],
    },
    {
        "name": "patch_file",
        "description": {
            "en": "Replace specific lines in a large file without rewriting the whole file (automatic .bak backup).",
            "id": "Mengganti baris tertentu pada file besar tanpa menulis ulang seluruh file (otomatis backup .bak).",
        },
        "params": {
            "path": _param("string"),
            "start_line": _param("integer"),
            "end_line": _param("integer"),
            "new_code": _param("string"),
        },
        "required": ["path", "start_line", "end_line", "new_code"],
    },
    {
        "name": "write_file",
        "description": {
            "en": "Create a new file or overwrite a small/medium file with complete content on the Kaggle worker disk.",
            "id": "Membuat file baru atau menimpa isi file kecil/menengah dengan konten lengkap.",
        },
        "params": {
            "path": _param("string"),
            "content": _param("string"),
        },
        "required": ["path", "content"],
    },
    {
        "name": "delete_path",
        "description": {
            "en": "Delete a file or an entire folder on the Kaggle worker disk. Only works inside the workspace (/kaggle/working); the workspace itself cannot be deleted. Always call list_dir on the target first.",
            "id": "Menghapus file atau seluruh folder di disk worker Kaggle. Hanya berlaku di dalam workspace (/kaggle/working); workspace itu sendiri tidak bisa dihapus. Selalu panggil list_dir dulu ke targetnya.",
        },
        "params": {
            "path": _param("string", "File or folder to delete, e.g. /kaggle/working/out/old.txt", "File atau folder yang dihapus, misal /kaggle/working/out/lama.txt"),
        },
        "required": ["path"],
    },
    {
        "name": "move_path",
        "description": {
            "en": "Move or rename a file or folder on the Kaggle worker disk. Source and destination must both be inside the workspace (/kaggle/working).",
            "id": "Memindah atau me-rename file atau folder di disk worker Kaggle. Sumber dan tujuan harus di dalam workspace (/kaggle/working).",
        },
        "params": {
            "path": _param("string", "File or folder to move, e.g. /kaggle/working/out/a.txt", "File atau folder yang dipindah, misal /kaggle/working/out/a.txt"),
            "destination": _param("string", "New location, e.g. /kaggle/working/archive/a.txt", "Lokasi baru, misal /kaggle/working/arsip/a.txt"),
        },
        "required": ["path", "destination"],
    },
    {
        "name": "run_bash",
        "description": {
            "en": "Run a non-interactive shell command on this Kaggle kernel.",
            "id": "Menjalankan perintah shell non-interaktif di kernel Kaggle ini.",
        },
        "params": {
            "command": _param("string"),
            "cwd": _param("string"),
        },
        "required": ["command"],
    },
    {
        "name": "web_search",
        "description": {
            "en": "Search the internet for current information.",
            "id": "Mencari informasi terkini di internet.",
        },
        "params": {
            "query": _param("string"),
        },
        "required": ["query"],
    },
    {
        "name": "web_image_search",
        "description": {
            "en": "Search the web for images of something. Returns thumbnail URLs with their source pages. The thumbnails are shown to the user as a strip automatically — describe the results in your answer, do not paste the raw URLs. Only say images were found if this tool actually returned thumbnails; if it returned nothing, say no images were found instead of claiming they were.",
            "id": "Mencari gambar di internet. Mengembalikan URL thumbnail beserta halaman sumbernya. Thumbnail otomatis ditampilkan ke user sebagai strip — jelaskan hasilnya di jawaban, jangan tempel URL mentahnya. Hanya bilang gambar ditemukan kalau tool ini benar-benar mengembalikan thumbnail; kalau tidak ada hasil, bilang tidak ada gambar yang ditemukan, jangan mengaku sudah menemukannya.",
        },
        "params": {
            "query": _param("string"),
        },
        "required": ["query"],
    },
    {
        "name": "web_fetch",
        "description": {
            "en": "Fetch the full content of a web page from a specific URL.",
            "id": "Mengambil isi lengkap halaman web dari URL spesifik.",
        },
        "params": {
            "url": _param("string"),
        },
        "required": ["url"],
    },
    {
        "name": "inspect_image",
        "description": {
            "en": "Load an image file from the Kaggle worker disk so it can be analyzed visually.",
            "id": "Memuat file gambar dari disk agar dapat dianalisis secara visual.",
        },
        "params": {
            "path": _param("string"),
            "question": _param("string"),
        },
        "required": ["path"],
    },
    {
        "name": "read_document",
        "description": {
            "en": (
                "Read a file on the Kaggle worker disk without pasting its whole content into context: "
                "PDF (text, or OCR if scanned), DOCX, PPTX, XLSX/CSV (built-in pandas), "
                "images (OCR), audio/video (ffmpeg + faster-whisper), and zip/tar/gz archives. "
                "Use this for files in /kaggle/working/uploads. Do not use read_file for PDF/binary files."
            ),
            "id": (
                "Membaca file di disk Kaggle tanpa menempel seluruh isi ke konteks: "
                "PDF (teks, atau OCR jika scan), DOCX, PPTX, XLSX/CSV (pandas bawaan), "
                "gambar (OCR), audio/video (ffmpeg + faster-whisper), dan arsip zip/tar/gz. "
                "Pakai ini untuk file di /kaggle/working/uploads. Jangan pakai read_file untuk PDF/biner."
            ),
        },
        "params": {
            "path": _param("string", "Absolute file path, e.g. /kaggle/working/uploads/name.pdf", "Path absolut file, misal /kaggle/working/uploads/nama.pdf"),
            "max_chars": _param("integer", "Result character limit, default 12000", "Batas karakter hasil, default 12000"),
        },
        "required": ["path"],
    },
    {
        "name": "remember",
        "description": {
            "en": (
                "Remember a durable fact about the user for future chats: a preference, "
                "a project detail, a name, or something the user asked you to remember. "
                "Save ONLY facts that stay true across chats — never passwords, secrets, "
                "API keys, or private codes, never a whole conversation, and never "
                "something the user only mentioned in passing. One short sentence per call. "
                "The user can see and delete everything you save."
            ),
            "id": (
                "Mengingat fakta awet tentang user untuk chat berikutnya: preferensi, "
                "detail proyek, nama, atau hal yang user minta diingat. Simpan HANYA fakta "
                "yang tetap benar antar chat — jangan pernah password, rahasia, API key, "
                "atau kode pribadi, jangan seluruh percakapan, dan jangan hal yang user "
                "sebut sekilas saja. Satu kalimat pendek per panggilan. User bisa melihat "
                "dan menghapus semua yang kamu simpan."
            ),
        },
        "params": {
            "text": _param("string", "One short fact to remember.", "Satu fakta pendek yang diingat."),
        },
        "required": ["text"],
    },
]


def tools_for(lang):
    """The 13 tool schemas in OpenAI function-call shape, in the given language."""
    out = []
    for tool in TOOL_DEFS:
        properties = {}
        for name, spec in tool["params"].items():
            prop = {"type": spec["type"]}
            desc = spec["description"].get(lang)
            if desc:
                prop["description"] = desc
            properties[name] = prop
        out.append({"type": "function", "function": {
            "name": tool["name"],
            "description": tool["description"][lang],
            "parameters": {"type": "object", "properties": properties, "required": list(tool["required"])},
        }})
    return out


# ---------------------------------------------------------------------------
# Tool-loop policy — the same human-like stopping on both sides. No fixed step
# count: the loop runs until the model answers, and only forces a final
# text-only round when it recognizes a stuck pattern.
# ---------------------------------------------------------------------------

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


def _short_preview(text, limit, lang):
    """Truncate a preview the way the worker's _short does: strip, then cap with
    a "+N characters" suffix when it overflows."""
    text = str(text if text is not None else "").strip()
    if len(text) <= limit:
        return text
    suffix = f"… [+{len(text) - limit} karakter]" if lang == "id" else f"… [+{len(text) - limit} characters]"
    return text[:limit] + suffix


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
        note = ("\n[...dipotong, hasil tool dibatasi 12000 karakter. Ini BUKAN tanda "
                "berhenti — lanjutkan dari titik ini: baca halaman berikutnya atau "
                "persempit dengan grep/pencarian.]") if lang == "id" else (
            "\n[...truncated, tool result limited to 12000 characters. This is NOT "
            "a stop sign — continue from this point: read the next page or narrow "
            "it down with grep/search.]")
        return text[:TOOL_RESULT_LIMIT] + note
    return text


def tool_detail(name, args):
    """Short human-readable detail for one tool call, shown in the Process
    block next to the step title (and as the terminal line for run_bash).
    Only the argument that identifies the call — never file contents.
    Kept to 200 chars: the frontend keeps at most 200 chars of detail."""
    if not isinstance(args, dict):
        args = {}
    if name == "run_bash":
        cmd = str(args.get("command") or "")
        if not cmd:
            return ""
        return f" ({cmd[:200]}…)" if len(cmd) > 200 else f" ({cmd})"
    if name == "move_path":
        src = str(args.get("path") or "").rsplit("/", 1)[-1]
        dst = str(args.get("destination") or "").rsplit("/", 1)[-1]
        if src and dst:
            return f" ({src} → {dst})"
        if src:
            return f" ({src})"
        return ""
    if name in ("list_dir", "read_file", "write_file", "patch_file", "read_document", "inspect_image", "delete_path"):
        p = str(args.get("path") or "")
        if p:
            return f" ({p.rsplit('/', 1)[-1] or p})"
        return ""
    if name in ("web_search", "web_image_search"):
        q = str(args.get("query") or "")[:60]
        return f" “{q}”" if q else ""
    if name == "remember":
        t = str(args.get("text") or "")[:60]
        return f" “{t}”" if t else ""
    if name == "web_fetch":
        u = str(args.get("url") or "")[:70]
        return f" ({u})" if u else ""
    return ""


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


def add_usage(acc, usage):
    """Accumulate prompt/completion/total tokens across model rounds into ``acc``
    (a dict with prompt_tokens/completion_tokens/total_tokens ints)."""
    if not isinstance(usage, dict):
        return acc
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        try:
            acc[key] = int(acc.get(key) or 0) + int(usage.get(key) or 0)
        except (TypeError, ValueError):
            pass
    return acc
