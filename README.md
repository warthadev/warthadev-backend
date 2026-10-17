## Wartha Sensei Backend API

Backend infrastructure for the Wartha Sensei platform, a comprehensive all in one web application providing cloud management, universal media downloading, and AI tools. Engineered with FastAPI and deployed via Docker on Hugging Face Spaces.

## Core Architecture

The backend is structured into distinct, independent routers to ensure high maintainability and separation of concerns:

| Module | Endpoint Prefix | Capabilities |
| :--- | :--- | :--- |
| Torrent | `/torrent` | Metadata extraction via aria2c, Google Colab session orchestration, real time progress tracking, and data synchronization. |
| Universal | `/universal` | Orchestration of universal media downloads via Google Colab. Supports extraction from YouTube, Instagram, TikTok, Twitter, Facebook, direct media URLs, and HLS/DASH streams. |
| Telegram | `/telegram` | Telethon client wrapper, session validation, secure file streaming with HTTP Range requests, and Firestore metadata synchronization. |
| Hugging Face | `/hf` | Comprehensive repository management including creation, deletion, renaming, file and folder movement/copying, cross account transfers, and history squashing. |
| Proxy | `/proxy` | Media streaming proxy, direct file downloads, and Fernet based signed URL generation for integration with external media players. |
| Token API | `/token` | Gemini API proxy for retrieving available models and validating API Keys. |
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