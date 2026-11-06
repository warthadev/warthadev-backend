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

def _user_id(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authentication required")
    try:
        init_firestore()
        decoded = auth.verify_id_token(authorization[7:].strip())
        uid = decoded.get("uid")
        if not uid:
            raise HTTPException(status_code=401, detail="Invalid authentication")
        return uid
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Invalid authentication") from exc

def _key_doc(user_id: str, provider: str):
    return init_firestore().collection("agent_users").document(user_id).collection("keys").document(provider)

def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:12]

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

@router.post("/chat")
async def agent_chat(payload: AgentChatPayload, authorization: str | None = Header(default=None)):
    user_id = _user_id(authorization)
    document = _key_doc(user_id, payload.provider).get()
    if not document.exists:
        raise HTTPException(status_code=404, detail="Provider key not found")
    data = document.to_dict() or {}
    try:
        provider_api_key = _fernet().decrypt(data["encryptedKey"].encode()).decode()
    except (KeyError, InvalidToken) as exc:
        raise HTTPException(status_code=500, detail="Stored provider key is invalid") from exc
    agent_url = os.getenv("AGENT_RUNTIME_URL", "http://127.0.0.1:8788").rstrip("/")
    internal_key = os.getenv("AGENT_INTERNAL_API_KEY", "")
    if not internal_key:
        raise HTTPException(status_code=500, detail="Agent runtime is not configured")
    body = {"prompt": payload.prompt, "model": payload.model or data.get("model"), "provider_api_key": provider_api_key, "history": payload.history}
    try:
        async with httpx.AsyncClient(timeout=float(os.getenv("AGENT_TIMEOUT", "210"))) as client:
            response = await client.post(f"{agent_url}/v1/agent/run", headers={"X-Agent-Internal-Key": internal_key}, json=body)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Agent runtime unavailable") from exc
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail="Agent runtime request failed")
    return {"success": True, "userId": user_id, "provider": payload.provider, "data": response.json()}
