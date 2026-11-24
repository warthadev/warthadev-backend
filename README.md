## Wartha Sensei Backend API

Backend infrastructure for the Wartha Sensei platform, a comprehensive all in one web application providing cloud management, universal media downloading, and AI tools. Engineered with FastAPI and deployed via Docker on Hugging Face Spaces.

## Core Architecture

The backend is structured into distinct, independent routers to ensure high maintainability and separation of concerns:

| Module | Endpoint Prefix | Capabilities |
| :--- | :--- | :--- |
| Torrent | `/torrent` | Metadata extraction via aria2c, Kaggle worker orchestration through the Kaggle API, real time progress tracking, and data synchronization. |
| Universal | `/universal` | Orchestration of Kaggle-backed media downloads. Supports extraction from YouTube, Instagram, TikTok, Twitter, Facebook, direct media URLs, and HLS/DASH streams. |
| Telegram | `/telegram` | Telethon client wrapper, session validation, secure file streaming with HTTP Range requests, and Firestore metadata synchronization. |
| Hugging Face | `/hf` | Comprehensive repository management including creation, deletion, renaming, file and folder movement/copying, cross account transfers, and history squashing. |
| Proxy | `/proxy` | Media streaming proxy, direct file downloads, and Fernet based signed URL generation for integration with external media players. |
| Token API | `/token` | AI Agent streaming (SSE) via provider gateway per akun, termasuk model Gemini. |
| Media Extractor | `/extract` | Direct video and audio link extraction using yt dlp, with a Cobalt API fallback for supported platforms. |

## Configuration

The following environment variables must be configured in the deployment environment:

### Firebase Integration
Required for authentication and database operations.
```env
FIREBASE_API_KEY=""
FIREBASE_AUTH_DOMAIN=""
FIREBASE_PROJECT_ID=""
FIREBASE_STORAGE_BUCKET=""
FIREBASE_MESSAGING_SENDER_ID=""
FIREBASE_APP_ID=""
FIREBASE_MEASUREMENT_ID=""
FIREBASE_SERVICE_ACCOUNT_JSON=""
```

### Agent Integration
Required for authenticated multi-user model key storage and agent runtime requests. `AGENT_RUNTIME_URL` must always be set explicitly; the localhost example is for local development only. In production, use the runtime's reachable published URL.
```env
AGENT_ENCRYPTION_KEY=""
# Local development only; production must point to the deployed runtime.
AGENT_RUNTIME_URL="http://127.0.0.1:8788"
AGENT_INTERNAL_API_KEY=""
AGENT_TIMEOUT="210"
```

### Kaggle downloader worker
Set these on the backend deployment. The backend pushes and runs a private Kaggle kernel through the Kaggle CLI; the frontend only polls Render for progress, so no manual notebook launch is needed.
The worker keeps downloads temporarily in `/kaggle/working/Temp`, uploads each completed file to Hugging Face, removes each per-download folder, and removes the `Temp` directory when the worker session ends. It is not a permanent archive.
```env
KAGGLE_API_TOKEN="KGAT_..."
KAGGLE_USERNAME="your-kaggle-username"
KAGGLE_DATASET="your-kaggle-username/media-offline-pkgs"
PUBLIC_BACKEND_URL="https://your-backend.onrender.com"
# Optional kernel slug; defaults to wartha-worker
KAGGLE_KERNEL_SLUG="wartha-worker"
```
