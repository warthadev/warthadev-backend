"""Chat-only API routes and account-isolated 9Router credential handling."""
from __future__ import annotations

import hashlib
import logging
import os
from typing import Any

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import Response
from firebase_admin import auth
from pydantic import BaseModel, Field

from .firestore_storage import init_firestore

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/chat", tags=["chat"])

DEFAULT_UPSTREAM = "https://20128-agq2firibhstlf6sr7k3ydjoim.instabox.tech/v1"
UPSTREAM = os.getenv("ROUTER_UPSTREAM", DEFAULT_UPSTREAM).rstrip("/")
TIMEOUT = httpx.Timeout(connect=20.0, read=600.0, write=60.0, pool=20.0)
LEGACY_PROVIDER = "9router"
DEFAULT_MODEL = "openrouter/openrouter/free"


class CredentialsPayload(BaseModel):
    api_key: str = Field(min_length=1, max_length=500)
    model: str = Field(min_length=1, max_length=200)


def _fernet() -> Fernet:
    value = os.getenv("AGENT_ENCRYPTION_KEY", "").strip()
    if not value:
        raise HTTPException(status_code=500, detail="Chat encryption is not configured")
    try:
        return Fernet(value.encode())
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Chat encryption key is invalid") from exc


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def _claims(authorization: str | None) -> dict[str, Any]:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authentication required")
    token = authorization[7:].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Authentication required")
    try:
        init_firestore()
        claims = auth.verify_id_token(token)
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Invalid authentication") from exc
    if not claims.get("uid"):
        raise HTTPException(status_code=401, detail="Invalid authentication")
    return claims


def _key_doc(user_id: str):
    return init_firestore().collection("agent_users").document(user_id).collection("keys").document(LEGACY_PROVIDER)


def _legacy_data(user_id: str) -> dict[str, Any]:
    snapshot = init_firestore().collection("users").document(user_id).get()
    return (snapshot.to_dict() or {}) if snapshot.exists else {}


def _get_key_snapshot(user_id: str):
    """Read the current encrypted key, or migrate a legacy plaintext key without deleting it."""
    reference = _key_doc(user_id)
    snapshot = reference.get()
    if snapshot.exists:
        return snapshot
    legacy = _legacy_data(user_id)
    legacy_key = legacy.get("nineRouterApiKey")
    if not isinstance(legacy_key, str) or not legacy_key.strip():
        return snapshot
    api_key = legacy_key.strip()
    legacy_model = legacy.get("nineRouterModel")
    model = legacy_model.strip() if isinstance(legacy_model, str) and legacy_model.strip() else DEFAULT_MODEL
    reference.set({
        "encryptedKey": _fernet().encrypt(api_key.encode()).decode(),
        "model": model,
        "fingerprint": _fingerprint(api_key),
        "enabled": True,
    }, merge=True)
    # Intentionally retain legacy fields for backwards compatibility and recovery.
    return reference.get()


def _saved_key(user_id: str) -> tuple[str, dict[str, Any]] | None:
    snapshot = _get_key_snapshot(user_id)
    data = snapshot.to_dict() or {} if snapshot.exists else {}
    if not data or not data.get("enabled", True):
        return None
    encrypted = data.get("encryptedKey")
    if not isinstance(encrypted, str) or not encrypted:
        raise HTTPException(status_code=500, detail="Stored chat credential is invalid")
    try:
        value = _fernet().decrypt(encrypted.encode()).decode()
    except (InvalidToken, ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=500, detail="Stored chat credential is invalid") from exc
    return value, data


def _extract_model_ids(payload: Any) -> list[dict[str, str]]:
    values: Any = payload
    if isinstance(payload, dict):
        values = payload.get("data", payload.get("models", payload.get("result", [])))
    if not isinstance(values, list):
        return []
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in values:
        model_id = item.strip() if isinstance(item, str) else item.get("id", "").strip() if isinstance(item, dict) and isinstance(item.get("id"), str) else ""
        if model_id and model_id not in seen:
            seen.add(model_id)
            result.append({"id": model_id})
    return result


async def _fetch_models(api_key: str) -> list[dict[str, str]]:
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            response = await client.get(f"{UPSTREAM}/models", headers={"Authorization": f"Bearer {api_key}"})
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Model catalog unavailable") from exc
    if response.status_code in (401, 403):
        raise HTTPException(status_code=401, detail="Upstream rejected the saved API key")
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail="Model catalog request failed")
    try:
        return _extract_model_ids(response.json())
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="Upstream returned an invalid model catalog") from exc


@router.get("/credentials")
def get_credentials(authorization: str | None = Header(default=None)):
    claims = _claims(authorization)
    saved = _saved_key(str(claims["uid"]))
    if saved is None:
        return {"configured": False, "model": "", "fingerprint": ""}
    _, data = saved
    return {"configured": True, "model": str(data.get("model", "")), "fingerprint": str(data.get("fingerprint", ""))}


@router.put("/credentials")
def put_credentials(payload: CredentialsPayload, authorization: str | None = Header(default=None)):
    claims = _claims(authorization)
    api_key = payload.api_key.strip()
    model = payload.model.strip()
    if not api_key or not model:
        raise HTTPException(status_code=400, detail="API key and model are required")
    fingerprint = _fingerprint(api_key)
    _key_doc(str(claims["uid"])).set({
        "encryptedKey": _fernet().encrypt(api_key.encode()).decode(),
        "model": model,
        "fingerprint": fingerprint,
        "enabled": True,
    }, merge=True)
    return {"success": True, "model": model, "fingerprint": fingerprint}


@router.delete("/credentials")
def delete_credentials(authorization: str | None = Header(default=None)):
    claims = _claims(authorization)
    _key_doc(str(claims["uid"])).delete()
    return {"success": True}


@router.get("/models")
async def get_models(authorization: str | None = Header(default=None)):
    claims = _claims(authorization)
    saved = _saved_key(str(claims["uid"]))
    if saved is None:
        return {"configured": False, "models": []}
    return {"configured": True, "models": await _fetch_models(saved[0])}


_ALLOWED_TOP_LEVEL = {"model", "messages", "stream", "temperature", "top_p", "max_tokens", "max_completion_tokens", "stop", "presence_penalty", "frequency_penalty", "n", "seed", "user"}
_ALLOWED_MESSAGE = {"role", "content", "name"}
_ALLOWED_ROLES = {"system", "user", "assistant"}


def _validate_completion(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="JSON object required")
    unknown = set(body) - _ALLOWED_TOP_LEVEL
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unsupported completion fields: {', '.join(sorted(unknown))}")
    if body.get("stream") is True:
        raise HTTPException(status_code=400, detail="Streaming is not supported")
    if body.get("stream", False) not in (False, None):
        raise HTTPException(status_code=400, detail="stream must be false")
    model = body.get("model")
    messages = body.get("messages")
    if not isinstance(model, str) or not model.strip() or not isinstance(messages, list) or not messages:
        raise HTTPException(status_code=400, detail="model and non-empty messages are required")
    clean_messages = []
    for message in messages:
        if not isinstance(message, dict) or set(message) - _ALLOWED_MESSAGE:
            raise HTTPException(status_code=400, detail="Only text chat messages are supported")
        if message.get("role") not in _ALLOWED_ROLES or not isinstance(message.get("content"), str):
            raise HTTPException(status_code=400, detail="Only system, user, and assistant text messages are supported")
        clean_messages.append({key: message[key] for key in ("role", "content", "name") if key in message})
    clean = {key: value for key, value in body.items() if key in _ALLOWED_TOP_LEVEL and key != "stream"}
    clean["model"] = model.strip()
    clean["messages"] = clean_messages
    clean["stream"] = False
    return clean


@router.post("/completions")
async def completions(request: Request, authorization: str | None = Header(default=None)):
    claims = _claims(authorization)
    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Valid JSON is required") from exc
    payload = _validate_completion(body)
    saved = _saved_key(str(claims["uid"]))
    if saved is None:
        raise HTTPException(status_code=403, detail="Chat credentials are not configured")
    api_key, _ = saved
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            upstream = await client.post(f"{UPSTREAM}/chat/completions", headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, json=payload)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Chat upstream unavailable") from exc
    content_type = upstream.headers.get("content-type", "application/json")
    return Response(content=upstream.content, status_code=upstream.status_code, media_type=content_type.split(";", 1)[0])
