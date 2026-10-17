# ⚡ Wartha Sensei Backend API

Backend service for the **Wartha Sensei** platform — an all-in-one web application encompassing cloud management (Telegram & Hugging Face), universal downloader, media extractor, and AI tools. Built with **FastAPI** (Python 3.11) and deployed as a Docker container on Hugging Face Spaces.

---

## 🏗️ Architecture & Modules

The backend is split into independent modules (routers) for maintainability:

| Module | Endpoint Prefix | Description |
| :--- | :--- | :--- |
| **Torrent** | `/torrent` | Metadata extraction via `aria2c`, Google Colab session orchestration, real-time progress tracking (download & upload), and synchronization. |
| **Universal** | `/universal` | Universal download orchestration via Google Colab. Supports extraction from YouTube, Instagram, TikTok, Twitter, Facebook, direct media URLs, and HLS/DASH streams. |
| **Telegram** | `/telegram` | Telethon client wrapper, session string validation, file streaming (HTTP Range requests), file downloads, and Firestore metadata sync. |
| **Hugging Face** | `/hf` | Complete repository management: create, delete, rename, move/copy files & folders, cross-account transfers, and history squashing. |
| **Proxy** | `/proxy` | Media streaming proxy, direct file downloads, and Fernet-based signed URL generation for external players (VLC, MX Player). |
| **Token API** | `/token` | Gemini API proxy for fetching available models and validating API Keys. |
| **Media Extractor** | `/extract` | Direct video/audio link extraction from various platforms (YouTube, IG, TikTok, etc.) using `yt-dlp` with a `Cobalt API` fallback. |

---

## ⚙️ Environment Variables (Secrets)

Ensure the following variables are set in your deployment environment (e.g., Hugging Face Spaces Secrets):

### Firebase (Required for Auth & Firestore)
- `FIREBASE_API_KEY`
- `FIREBASE_AUTH_DOMAIN`
- `FIREBASE_PROJECT_ID`
- `FIREBASE_STORAGE_BUCKET`
- `FIREBASE_MESSAGING_SENDER_ID`
- `FIREBASE_APP_ID`
- `FIREBASE_MEASUREMENT_ID`
- `FIREBASE_SERVICE_ACCOUNT_JSON` — *Stringified JSON from the Service Account (Admin SDK) for Firestore access.*

### Telegram Integration
- `TELEGRAM_API_ID`
- `TELEGRAM_API_HASH`

### AI & Proxy Security
- `GEMINI_API_KEY`
- `STREAM_TICKET_SECRET` — *Secret key (min 32 chars) for encrypting signed streaming URLs (Fernet).*

### CORS Configuration
- `ALLOWED_ORIGINS` — *Comma-separated list of allowed origins (e.g., `https://warthasensei.web.app,http://localhost:5173`).*

---

## 🚀 Deployment

This project is configured to run on **Hugging Face Spaces (Docker SDK)**.
- **Base Image:** `python:3.11-slim-bookworm`
- **System Dependencies:** `aria2` (torrent), `ffmpeg` (media processing), `p7zip`, `unrar-free`.
- **Port:** `7860` (Hugging Face Spaces default)

To run locally:
```bash
pip install -r requirements.txt
python main.py