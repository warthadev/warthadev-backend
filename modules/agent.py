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

router = APIRouter(prefix="/agent", tags=["agent"])

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

def _user_id(authorization: str | None) -> str:
    return str(_claims(authorization)["uid"])

def _user_email(authorization: str | None) -> str:
    email = str(_claims(authorization).get("email") or "").strip().lower()
    if not email:
        raise HTTPException(status_code=403, detail="Verified email is required")
    return email

def _key_doc(user_id: str, provider: str):
    return init_firestore().collection("agent_users").document(user_id).collection("keys").document(provider)

def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:12]

def _is_local_model(model: str | None) -> bool:
    return bool(model and model.strip().lower() == os.getenv("AGENT_LOCAL_MODEL", "mimo-v2.6:latest").strip().lower())

def _ensure_local_access(user_id: str, model: str | None) -> None:
    if not _is_local_model(model):
        return
    key_document = _key_doc(user_id, "9router").get()
    key_data = key_document.to_dict() if key_document.exists else {}
    if key_document.exists and not key_data.get("enabled", True):
        raise HTTPException(status_code=403, detail="Local agent access requires a saved 9Router key")
    encrypted_key = key_data.get("encryptedKey")
    if isinstance(encrypted_key, str) and encrypted_key:
        return
    user_document = init_firestore().collection("users").document(user_id).get()
    user_data = user_document.to_dict() if user_document.exists else {}
    legacy_key = user_data.get("nineRouterApiKey")
    if isinstance(legacy_key, str) and legacy_key.strip():
        return
    raise HTTPException(status_code=403, detail="Local agent access requires a saved 9Router key")

@router.put("/keys")
def save_agent_key(payload: AgentKeyPayload, authorization: str | None = Header(default=None)):
    user_id = _user_id(authorization)
    encrypted = _fernet().encrypt(payload.api_key.encode()).decode()
    _key_doc(user_id, payload.provider).set({"encryptedKey": encrypted, "model": payload.model, "fingerprint": _fingerprint(payload.api_key), "enabled": True}, merge=True)
    return {"success": True, "provider": payload.provider, "model": payload.model, "fingerprint": _fingerprint(payload.api_key)}

@router.get("/keys")
def list_agent_keys(authorization: str | None = Header(default=None)):
    user_id = _user_id(authorization)
    docs = _key_doc(user_id, "__index__").parent.stream()
    return {"keys": [{"provider": doc.id, "model": (doc.to_dict() or {}).get("model", ""), "enabled": (doc.to_dict() or {}).get("enabled", True), "fingerprint": (doc.to_dict() or {}).get("fingerprint", "")} for doc in docs]}

@router.delete("/keys/{provider}")
def delete_agent_key(provider: str, authorization: str | None = Header(default=None)):
    user_id = _user_id(authorization)
    _key_doc(user_id, provider).delete()
    return {"success": True, "provider": provider}

@router.get("/models")
async def agent_models(authorization: str | None = Header(default=None)):
    _claims(authorization)
    agent_url = os.getenv("AGENT_RUNTIME_URL", "http://127.0.0.1:8788").rstrip("/")
    internal_key = os.getenv("AGENT_INTERNAL_API_KEY", "")
    if not internal_key:
        raise HTTPException(status_code=500, detail="Agent runtime is not configured")
    try:
        async with httpx.AsyncClient(timeout=float(os.getenv("AGENT_TIMEOUT", "210"))) as client:
            response = await client.get(f"{agent_url}/v1/models", headers={"X-Agent-Internal-Key": internal_key})
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Agent runtime unavailable") from exc
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail="Agent runtime request failed")
    return response.json()

@router.post("/chat")
async def agent_chat(payload: AgentChatPayload, authorization: str | None = Header(default=None)):
    claims = _claims(authorization)
    user_id = str(claims["uid"])
    email = str(claims.get("email") or "").strip().lower()
    provider = payload.provider.strip().lower()
    document = _key_doc(user_id, provider).get()
    server_managed = provider == "9router"
    if not document.exists and not server_managed:
        raise HTTPException(status_code=404, detail="Provider key not found")
    data = document.to_dict() or {}
    selected_model = payload.model or data.get("model") or os.getenv("AGENT_DEFAULT_MODEL", "mimo-v2.6:latest")
    _ensure_local_access(user_id, selected_model)
    provider_api_key = ""
    if document.exists and not _is_local_model(selected_model):
        try:
            provider_api_key = _fernet().decrypt(data["encryptedKey"].encode()).decode()
        except (KeyError, InvalidToken) as exc:
            raise HTTPException(status_code=500, detail="Stored provider key is invalid") from exc
    agent_url = os.getenv("AGENT_RUNTIME_URL", "http://127.0.0.1:8788").rstrip("/")
    internal_key = os.getenv("AGENT_INTERNAL_API_KEY", "")
    if not internal_key:
        raise HTTPException(status_code=500, detail="Agent runtime is not configured")
    body = {"prompt": payload.prompt, "model": selected_model, "provider_api_key": provider_api_key, "user_email": email or user_id, "history": payload.history}
    try:
        async with httpx.AsyncClient(timeout=float(os.getenv("AGENT_TIMEOUT", "210"))) as client:
            response = await client.post(f"{agent_url}/v1/agent/run", headers={"X-Agent-Internal-Key": internal_key}, json=body)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Agent runtime unavailable") from exc
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail="Agent runtime request failed")
    return {"success": True, "userId": user_id, "provider": provider, "data": response.json()}
