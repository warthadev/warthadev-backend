---
title: Wartha Backend
emoji: ⚡
colorFrom: gray
colorTo: indigo
sdk: docker
pinned: false
---

# ⚡ Wartha Sensei Backend API

Backend service untuk platform **Wartha Sensei** — sebuah all-in-one web application yang mencakup manajemen cloud (Telegram & Hugging Face), downloader universal, media extractor, dan AI tools. Dibangun menggunakan **FastAPI** (Python 3.11) dan di-deploy menggunakan Docker di Hugging Face Spaces.

---

## 🏗️ Arsitektur & Modul

Backend dipecah menjadi beberapa modul (routers) independen agar mudah di-maintain:

| Modul | Prefix Endpoint | Deskripsi |
| :--- | :--- | :--- |
| **Torrent** | `/torrent` | Ekstraksi metadata via `aria2c`, manajemen sesi Google Colab, pelacakan progress real-time (download & upload), dan sinkronisasi. |
| **Universal** | `/universal` | Orkestrasi download universal via Google Colab. Mendukung ekstraksi dari YouTube, Instagram, TikTok, Twitter, Facebook, direct media URL, dan HLS/DASH streams. |
| **Telegram** | `/telegram` | Wrapper Telethon client, validasi session string, streaming file (HTTP Range requests), download file, dan sinkronisasi metadata ke Firestore. |
| **Hugging Face** | `/hf` | Manajemen repositori lengkap: buat, hapus, rename, pindah/copy file & folder, transfer antar akun (cross-copy), dan pembersihan history (squash). |
| **Proxy** | `/proxy` | Proxy streaming media, download langsung, dan pembuatan signed URL (Fernet) untuk memutar file di aplikasi eksternal (VLC, MX Player). |
| **Token API** | `/token` | Proxy ke Gemini API untuk mengecek daftar model yang tersedia dan memvalidasi API Key. |
| **Media Extractor** | `/extract` | Ekstraksi direct link video/audio dari berbagai platform (YouTube, IG, TikTok, dll) menggunakan `yt-dlp` dan fallback ke `Cobalt API`. |

---

## ⚙️ Environment Variables (Secrets)

Pastikan variabel berikut di-set di environment deployment (misal: Hugging Face Spaces Secrets):

### Firebase (Wajib untuk Auth & Firestore)
- `FIREBASE_API_KEY`
- `FIREBASE_AUTH_DOMAIN`
- `FIREBASE_PROJECT_ID`
- `FIREBASE_STORAGE_BUCKET`
- `FIREBASE_MESSAGING_SENDER_ID`
- `FIREBASE_APP_ID`
- `FIREBASE_MEASUREMENT_ID`
- `FIREBASE_SERVICE_ACCOUNT_JSON` — *Stringified JSON dari Service Account (Admin SDK) untuk akses Firestore.*

### Telegram Integration
- `TELEGRAM_API_ID`
- `TELEGRAM_API_HASH`

### AI & Proxy Security
- `GEMINI_API_KEY`
- `STREAM_TICKET_SECRET` — *Secret key (min 32 chars) untuk enkripsi signed URL streaming (Fernet).*

### Konfigurasi CORS
- `ALLOWED_ORIGINS` — *Daftar origin yang diizinkan, pisahkan dengan koma. (e.g. `https://warthasensei.web.app,http://localhost:5173`)*

---

## 🚀 Deployment

Proyek ini dikonfigurasi untuk berjalan di **Hugging Face Spaces (Docker SDK)**.
- **Base Image:** `python:3.11-slim-bookworm`
- **System Dependencies:** `aria2` (torrent), `ffmpeg` (media processing), `p7zip`, `unrar-free`.
- **Port:** `7860` (default HF Spaces)

Untuk menjalankan secara lokal:
```bash
pip install -r requirements.txt
python main.py