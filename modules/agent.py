# /warthadev-backend/modules/agent.py
import hashlib
import os
from typing import Any

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Header, HTTPException
from firebase_admin import auth
from pydantic import BaseModel, Field

from .firestore_storage import init_firestore
from .router import UPSTREAM as ROUTER_UPSTREAM

router = APIRouter(prefix="/agent", tags=["agent"])
LOCAL_MODEL_ALIAS = "openai-compatible-chat-0082e0a8-8fc6-41da-99d3-3c9f2f986404/mimo-v2.6:latest"


class AgentKeyPayload(BaseModel):
    provider: str = Field(min_length=1, max_length=80)
    api_key: str = Field(min_length=1, max_length=500)
    model: str = Field(min_length=1, max_length=200)


class AgentChatPayload(BaseModel):
    prompt: str = Field(min_length=1, max_length=24000)
    provider: str = Field(min_length=1, max_length=80)
    model: str | None = Field(default=None, max_length=200)
    history: list[dict[str, Any]] = Field(default_factory=list, max_length=40)


def _fernet() -> Fernet:
    value = os.getenv("AGENT_ENCRYPTION_KEY", "")
    if not value:
        raise HTTPException(status_code=500, detail="Agent encryption is not configured")
    try:
        return Fernet(value.encode())
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Agent encryption key is invalid") from exc


def _claims(authorization: str | None) -> dict[str, Any]:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authentication required")
    try:
        init_firestore()
        decoded = auth.verify_id_token(authorization[7:].strip())
        if not decoded.get("uid"):
            raise HTTPException(status_code=401, detail="Invalid authentication")
        return decoded
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Invalid authentication") from exc


def _key_doc(user_id: str, provider: str):
    return init_firestore().collection("agent_users").document(user_id).collection("keys").document(provider)


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def _get_key_snapshot(user_id: str, provider: str):
    """Load a saved key, migrating legacy plaintext 9Router keys server-side once."""
    key_reference = _key_doc(user_id, provider)
    snapshot = key_reference.get()
    if snapshot.exists or provider != "9router":
        return snapshot

    database = init_firestore()
    legacy_reference = database.collection("users").document(user_id)
    legacy_snapshot = legacy_reference.get()
    legacy_data = legacy_snapshot.to_dict() if legacy_snapshot.exists else {}
    legacy_key = legacy_data.get("nineRouterApiKey")
    if not isinstance(legacy_key, str) or not legacy_key.strip():
        return snapshot

    normalized_key = legacy_key.strip()
    legacy_model = legacy_data.get("nineRouterModel")
    model = legacy_model.strip() if isinstance(legacy_model, str) and legacy_model.strip() else "openrouter/openrouter/free"
    key_reference.set({
        "encryptedKey": _fernet().encrypt(normalized_key.encode()).decode(),
        "model": model,
        "fingerprint": _fingerprint(normalized_key),
        "enabled": True,
    }, merge=True)
    legacy_reference.set({"nineRouterApiKey": None, "nineRouterModel": None}, merge=True)
    return key_reference.get()


def _saved_key(user_id: str, provider: str) -> tuple[str, dict[str, Any]] | None:
    snapshot = _get_key_snapshot(user_id, provider)
    if not snapshot.exists:
        return None
    data = snapshot.to_dict() or {}
    if not data.get("enabled", True):
        return None
    encrypted_key = data.get("encryptedKey")
    if not isinstance(encrypted_key, str) or not encrypted_key:
        raise HTTPException(status_code=500, detail="Stored provider key is invalid")
    try:
        api_key = _fernet().decrypt(encrypted_key.encode()).decode()
    except (InvalidToken, ValueError) as exc:
        raise HTTPException(status_code=500, detail="Stored provider key is invalid") from exc
    return api_key, data


def _extract_model_ids(payload: Any) -> list[str]:
    """Extract only model IDs from OpenAI-compatible catalog response shapes."""
    if isinstance(payload, list):
        result: list[str] = []
        for item in payload:
            if isinstance(item, str) and item.strip():
                result.append(item.strip())
            elif isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"].strip():
                result.append(item["id"].strip())
        return list(dict.fromkeys(result))
    if isinstance(payload, dict):
        for key in ("data", "models", "result"):
            if key in payload:
                found = _extract_model_ids(payload[key])
                if found or isinstance(payload[key], (list, dict)):
                    return found
    return []


def _router_models_url() -> str:
    upstream = os.getenv("AGENT_ROUTER_BASE_URL", ROUTER_UPSTREAM).rstrip("/")
    return f"{upstream}/models"


async def _fetch_router_models(api_key: str) -> list[str]:
    try:
        async with httpx.AsyncClient(timeout=float(os.getenv("AGENT_TIMEOUT", "45"))) as client:
            response = await client.get(
                _router_models_url(),
                headers={"Authorization": f"Bearer {api_key}"},
            )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="9Router model catalog is unavailable") from exc

    if response.status_code in (401, 403):
        raise HTTPException(status_code=401, detail="9Router rejected the saved API key")
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail="9Router model catalog request failed")
    try:
        payload = response.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="9Router returned an invalid model catalog") from exc
    return _extract_model_ids(payload)


def _runtime_model(model: str) -> str:
    local_model = os.getenv("AGENT_LOCAL_MODEL", "mimo-v2.6:latest").strip().lower()
    normalized = model.strip().lower()
    alias = os.getenv("AGENT_LOCAL_MODEL_ALIAS", LOCAL_MODEL_ALIAS).strip().lower()
    return os.getenv("AGENT_LOCAL_MODEL", "mimo-v2.6:latest").strip() if normalized in {local_model, alias} else model


@router.put("/keys")
def save_agent_key(payload: AgentKeyPayload, authorization: str | None = Header(default=None)):
    claims = _claims(authorization)
    user_id = str(claims["uid"])
    provider = payload.provider.strip().lower()
    api_key = payload.api_key.strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="Provider API key is required")
    encrypted = _fernet().encrypt(api_key.encode()).decode()
    fingerprint = _fingerprint(api_key)
    if provider == "9router":
        _get_key_snapshot(user_id, provider)
    _key_doc(user_id, provider).set({
        "encryptedKey": encrypted,
        "model": payload.model.strip(),
        "fingerprint": fingerprint,
        "enabled": True,
    }, merge=True)
    return {"success": True, "provider": provider, "model": payload.model.strip(), "fingerprint": fingerprint}


@router.get("/keys")
def list_agent_keys(authorization: str | None = Header(default=None)):
    claims = _claims(authorization)
    user_id = str(claims["uid"])
    _get_key_snapshot(user_id, "9router")
    docs = _key_doc(user_id, "__index__").parent.stream()
    return {
        "keys": [
            {
                "provider": doc.id,
                "model": (doc.to_dict() or {}).get("model", ""),
                "enabled": (doc.to_dict() or {}).get("enabled", True),
                "fingerprint": (doc.to_dict() or {}).get("fingerprint", ""),
            }
            for doc in docs
        ]
    }


@router.delete("/keys/{provider}")
def delete_agent_key(provider: str, authorization: str | None = Header(default=None)):
    claims = _claims(authorization)
    user_id = str(claims["uid"])
    normalized_provider = provider.strip().lower()
    if normalized_provider == "9router":
        _get_key_snapshot(user_id, normalized_provider)
    _key_doc(user_id, normalized_provider).delete()
    return {"success": True, "provider": normalized_provider}


@router.get("/models")
async def agent_models(authorization: str | None = Header(default=None)):
    claims = _claims(authorization)
    user_id = str(claims["uid"])
    saved = _saved_key(user_id, "9router")
    if saved is None:
        return {"configured": False, "models": []}
    api_key, _ = saved
    models = await _fetch_router_models(api_key)
    return {"configured": True, "models": models}


@router.post("/chat")
async def agent_chat(payload: AgentChatPayload, authorization: str | None = Header(default=None)):
    claims = _claims(authorization)
    user_id = str(claims["uid"])
    provider = payload.provider.strip().lower()
    if provider != "9router":
        raise HTTPException(status_code=400, detail="Unsupported agent provider")

    saved = _saved_key(user_id, provider)
    if saved is None:
        raise HTTPException(status_code=403, detail="A 9Router API key is not configured for this account")
    api_key, _ = saved
    selected_model = (payload.model or "").strip()
    if not selected_model:
        raise HTTPException(status_code=400, detail="Select an available model")

    available_models = await _fetch_router_models(api_key)
    if selected_model not in available_models:
        raise HTTPException(status_code=403, detail="Model is not available for this account")

    runtime_model = _runtime_model(selected_model)
    local_model = os.getenv("AGENT_LOCAL_MODEL", "mimo-v2.6:latest").strip().lower()
    provider_api_key = "" if runtime_model.strip().lower() == local_model else api_key
    agent_url = os.getenv("AGENT_RUNTIME_URL", "http://127.0.0.1:8788").rstrip("/")
    internal_key = os.getenv("AGENT_INTERNAL_API_KEY", "")
    if not internal_key:
        raise HTTPException(status_code=500, detail="Agent runtime is not configured")
    body = {
        "prompt": payload.prompt,
        "model": runtime_model,
        "provider_api_key": provider_api_key,
        "user_email": str(claims.get("email") or user_id).strip().lower(),
        "history": payload.history,
    }
    try:
        async with httpx.AsyncClient(timeout=float(os.getenv("AGENT_TIMEOUT", "210"))) as client:
            response = await client.post(
                f"{agent_url}/v1/agent/run",
                headers={"X-Agent-Internal-Key": internal_key},
                json=body,
            )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Agent runtime unavailable") from exc
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail="Agent runtime request failed")
    return {"success": True, "userId": user_id, "provider": provider, "data": response.json()}
