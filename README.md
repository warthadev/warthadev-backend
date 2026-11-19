# Wartha Sensei Chat Backend

A FastAPI backend for authenticated, text-only chat. The public API contains only health/root/config endpoints and the `/chat` contract; agent runtime, workspace, media, download, Telegram, Hugging Face, and token routes are not loaded.

## Chat API

All `/chat` endpoints require `Authorization: Bearer <Firebase ID token>`.

- `GET /chat/credentials` returns `{configured, model, fingerprint}`.
- `PUT /chat/credentials` accepts `{api_key, model}` and returns `{success, model, fingerprint}`.
- `DELETE /chat/credentials` disables the current encrypted credential document.
- `GET /chat/models` returns `{configured, models:[{id}]}` from the upstream model catalog.
- `POST /chat/completions` accepts standard OpenAI text-chat JSON and returns the upstream OpenAI response. `stream:true`, tool/function fields, multimodal content, and unsupported message roles are rejected.

Credentials are encrypted with `AGENT_ENCRYPTION_KEY` and stored per Firebase UID under `agent_users/{uid}/keys/9router`. Existing `users/{uid}` legacy `nineRouterApiKey`/`nineRouterModel` values are read and migrated into the encrypted document without deleting the legacy fields.

## Configuration

```env
FIREBASE_API_KEY=""
FIREBASE_AUTH_DOMAIN=""
FIREBASE_PROJECT_ID=""
FIREBASE_STORAGE_BUCKET=""
FIREBASE_MESSAGING_SENDER_ID=""
FIREBASE_APP_ID=""
FIREBASE_MEASUREMENT_ID=""
FIREBASE_SERVICE_ACCOUNT_JSON=""
AGENT_ENCRYPTION_KEY=""
ROUTER_UPSTREAM="https://20128-agq2firibhstlf6sr7k3ydjoim.instabox.tech/v1"
ALLOWED_ORIGINS="*"
```

`GET /config` exposes Firebase client configuration only. `GET /health` and `GET /` are unauthenticated readiness endpoints.
