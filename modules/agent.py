# /warthadev-backend/modules/agent.py
import base64
import binascii
import hashlib
import logging
import os
import re
from typing import Any

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Header, HTTPException
from firebase_admin import auth, firestore
from pydantic import BaseModel, Field

from .agent_prompt import DEFAULT_SYSTEM_INSTRUCTION
from .firestore_storage import init_firestore

router = APIRouter(prefix="/agent", tags=["agent"])
logger = logging.getLogger(__name__)
ROUTER_PROVIDER = "9router"


class AgentKeyPayload(BaseModel):
    provider: str = Field(min_length=1, max_length=80)
    api_key: str = Field(min_length=1, max_length=500)
    # Accepted for compatibility with older clients; model choice is now per chat.
    model: str | None = Field(default=None, max_length=200)


class AgentImagePayload(BaseModel):
    mime_type: str = Field(min_length=1, max_length=32)
    data: str = Field(min_length=1, max_length=12_000_000)


class AgentChatPayload(BaseModel):
    prompt: str = Field(min_length=1)
    provider: str = Field(default=ROUTER_PROVIDER, min_length=1, max_length=80)
    model: str | None = Field(default=None, max_length=200)
    # Kept in the request schema for compatibility, but no search/tools are sent upstream.
    web_search: bool = False
    search_query: str = Field(default="", max_length=500)
    timezone: str = Field(default="Asia/Jakarta", max_length=64)
    history: list[dict[str, Any]] = Field(default_factory=list, max_length=40)
    images: list[AgentImagePayload] = Field(default_factory=list, max_length=12)


def _validate_chat_images(images: list[AgentImagePayload]) -> list[dict[str, str]]:
    validated: list[dict[str, str]] = []
    for image in images:
        mime_type = image.mime_type.strip().lower()
        encoded = image.data
        if not mime_type.startswith("image/") or not encoded:
            raise HTTPException(status_code=400, detail={"code": "bad_request", "message": "Image MIME type and data are required"})
        try:
            decoded = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise HTTPException(status_code=400, detail={"code": "bad_request", "message": "An image payload is not valid base64"}) from exc
        if not decoded:
            raise HTTPException(status_code=400, detail={"code": "bad_request", "message": "Image data cannot be empty"})
        validated.append({"mime_type": mime_type, "data": encoded})
    return validated


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


def _user_doc(user_id: str):
    return init_firestore().collection("users").document(user_id)


def _legacy_key_doc(user_id: str, provider: str):
    return init_firestore().collection("agent_users").document(user_id).collection("keys").document(provider)


def _normalize_provider(provider: str) -> str:
    normalized = provider.strip().lower()
    if not re.fullmatch(r"[a-z0-9_-]{1,80}", normalized):
        raise HTTPException(status_code=400, detail="Invalid provider")
    if normalized != ROUTER_PROVIDER:
        raise HTTPException(status_code=400, detail="Only per-account 9Router API keys are supported")
    return normalized


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def _get_key_record(user_id: str, provider: str) -> dict[str, Any] | None:
    """Load a key from users/{uid}, migrating older per-user formats without exposing it."""
    user_reference = _user_doc(user_id)
    user_snapshot = user_reference.get()
    user_data = user_snapshot.to_dict() if user_snapshot.exists else {}
    user_data = user_data or {}
    key_map = user_data.get("agentKeys")
    record = key_map.get(provider) if isinstance(key_map, dict) else None
    if isinstance(record, dict):
        return record

    # Migrate the previous encrypted subcollection format on access, preserving account scope.
    legacy_reference = _legacy_key_doc(user_id, provider)
    legacy_snapshot = legacy_reference.get()
    legacy_data = legacy_snapshot.to_dict() if legacy_snapshot.exists else {}
    encrypted_key = legacy_data.get("encryptedKey") if isinstance(legacy_data, dict) else None
    if isinstance(encrypted_key, str) and encrypted_key:
        record = {
            "encryptedKey": encrypted_key,
            "model": str(legacy_data.get("model") or ""),
            "fingerprint": str(legacy_data.get("fingerprint") or ""),
            "enabled": bool(legacy_data.get("enabled", True)),
        }
        migration = {"agentKeys": {provider: record}}
        if provider == ROUTER_PROVIDER:
            migration.update({"nineRouterApiKey": None, "nineRouterModel": None})
        user_reference.set(migration, merge=True)
        legacy_reference.delete()
        return record

    # Migrate original plaintext 9Router fields, encrypting before the write and clearing them.
    if provider != ROUTER_PROVIDER:
        return None
    legacy_key = user_data.get("nineRouterApiKey")
    if not isinstance(legacy_key, str) or not legacy_key.strip():
        return None

    normalized_key = legacy_key.strip()
    legacy_model = user_data.get("nineRouterModel")
    model = legacy_model.strip() if isinstance(legacy_model, str) and legacy_model.strip() else ""
    record = {
        "encryptedKey": _fernet().encrypt(normalized_key.encode()).decode(),
        "model": model,
        "fingerprint": _fingerprint(normalized_key),
        "enabled": True,
    }
    user_reference.set({
        "agentKeys": {provider: record},
        "nineRouterApiKey": None,
        "nineRouterModel": None,
    }, merge=True)
    return record


def _set_key_record(user_id: str, provider: str, record: dict[str, Any]) -> None:
    _user_doc(user_id).set({"agentKeys": {provider: record}}, merge=True)


def _delete_key_record(user_id: str, provider: str) -> None:
    user_reference = _user_doc(user_id)
    user_snapshot = user_reference.get()
    user_data = user_snapshot.to_dict() if user_snapshot.exists else {}
    key_map = (user_data or {}).get("agentKeys")
    if isinstance(key_map, dict) and provider in key_map:
        user_reference.update({f"agentKeys.{provider}": firestore.DELETE_FIELD})
    if provider == ROUTER_PROVIDER:
        user_reference.set({"nineRouterApiKey": None, "nineRouterModel": None}, merge=True)
    _legacy_key_doc(user_id, provider).delete()


def _saved_router_key(user_id: str) -> tuple[str, dict[str, Any]] | None:
    record = _get_key_record(user_id, ROUTER_PROVIDER)
    if not isinstance(record, dict) or not record.get("enabled", True):
        return None
    encrypted_key = record.get("encryptedKey")
    if not isinstance(encrypted_key, str) or not encrypted_key:
        raise HTTPException(status_code=500, detail="Stored 9Router key is invalid")
    try:
        api_key = _fernet().decrypt(encrypted_key.encode()).decode()
    except (InvalidToken, ValueError) as exc:
        raise HTTPException(status_code=500, detail="Stored 9Router key is invalid") from exc
    return api_key, record


def _router_base_url() -> str:
    """Require an explicitly approved HTTPS 9Router endpoint; never fall back to an old host."""
    value = os.getenv("AGENT_9ROUTER_BASE_URL", "").strip()
    if not value:
        raise HTTPException(status_code=503, detail="No approved 9Router endpoint is configured for this deployment")
    try:
        parsed = httpx.URL(value)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="The configured 9Router endpoint is invalid") from exc
    if parsed.scheme != "https" or not parsed.host or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise HTTPException(status_code=503, detail="The configured 9Router endpoint must be an HTTPS base URL")
    return value.rstrip("/")


def _router_url(endpoint: str) -> str:
    return f"{_router_base_url()}/{endpoint.lstrip('/')}"


async def _router_request(
    method: str,
    endpoint: str,
    api_key: str,
    json_body: dict[str, Any] | None = None,
) -> httpx.Response:
    url = _router_url(endpoint)
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    if json_body is not None:
        headers["Content-Type"] = "application/json"
    try:
        async with httpx.AsyncClient(
            timeout=float(os.getenv("AGENT_TIMEOUT", "90")),
            follow_redirects=False,
        ) as client:
            return await client.request(method, url, headers=headers, json=json_body)
    except httpx.HTTPError as exc:
        logger.warning("9Router request failed endpoint=%s error=%s", endpoint, type(exc).__name__)
        raise HTTPException(status_code=502, detail="9Router is unavailable") from exc


def _raise_router_http_error(response: httpx.Response, action: str) -> None:
    if response.status_code in (401, 403):
        raise HTTPException(status_code=401, detail="9Router rejected the saved API key or model access")
    if response.status_code == 429:
        raise HTTPException(status_code=429, detail="9Router rate limit (429)")
    if response.status_code in (502, 503, 504):
        raise HTTPException(status_code=503, detail="9Router is temporarily unavailable")
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"9Router {action} request failed ({response.status_code})")


def _extract_models(payload: Any) -> list[dict[str, Any]]:
    """Extract model IDs and documented capability metadata from an OpenAI-compatible catalog."""
    if isinstance(payload, list):
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in payload:
            if isinstance(item, str) and item.strip():
                model_id = item.strip()
                model: dict[str, Any] = {"id": model_id}
            elif isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"].strip():
                model_id = item["id"].strip()
                model = {"id": model_id}
                sources = [source for source in (item.get("capabilities"), item, item.get("limits")) if isinstance(source, dict)]
                safe_capabilities: dict[str, Any] = {}
                for name in ("reasoning", "thinkingCanDisable", "vision", "tools"):
                    value = next((source[name] for source in sources if name in source), None)
                    if isinstance(value, bool):
                        safe_capabilities[name] = value
                aliases = {
                    "contextLength": ("contextLength", "context_length", "contextWindow", "context_window"),
                    "maxOutputTokens": ("maxOutputTokens", "max_output_tokens", "maxCompletionTokens", "max_completion_tokens"),
                }
                for normalized_name, names in aliases.items():
                    value = next((source[name] for source in sources for name in names if name in source), None)
                    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                        safe_capabilities[normalized_name] = value
                if safe_capabilities:
                    model["capabilities"] = safe_capabilities
            else:
                continue
            if model_id not in seen:
                seen.add(model_id)
                result.append(model)
        return result
    if isinstance(payload, dict):
        for key in ("data", "models", "result"):
            if key in payload:
                found = _extract_models(payload[key])
                if found or isinstance(payload[key], (list, dict)):
                    return found
    return []


async def _fetch_router_models(api_key: str) -> list[dict[str, Any]]:
    response = await _router_request("GET", "models", api_key)
    _raise_router_http_error(response, "model catalog")
    try:
        payload = response.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="9Router returned an invalid model catalog") from exc
    return _extract_models(payload)


def _chat_message_content(prompt: str, images: list[dict[str, str]]) -> str | list[dict[str, Any]]:
    if not images:
        return prompt
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for image in images:
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{image['mime_type']};base64,{image['data']}"},
        })
    return content


def _chat_messages(payload: AgentChatPayload, images: list[dict[str, str]]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = [{"role": "system", "content": DEFAULT_SYSTEM_INSTRUCTION}]
    for item in payload.history:
        role = item.get("role")
        content = item.get("content")
        if role in {"user", "assistant"} and isinstance(content, str) and content.strip():
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": _chat_message_content(payload.prompt, images)})
    return messages


def _completion_text(payload: Any) -> tuple[str, dict[str, Any]]:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=502, detail="9Router returned an invalid chat response")
    choices = payload.get("choices")
    choice = choices[0] if isinstance(choices, list) and choices else None
    message = choice.get("message") if isinstance(choice, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        text = content.strip()
    elif isinstance(content, list):
        text = "\n".join(
            part["text"] for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        ).strip()
    else:
        text = ""
    if not text:
        raise HTTPException(status_code=502, detail="9Router returned an empty chat response")
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    return text, usage


@router.put("/keys")
def save_agent_key(payload: AgentKeyPayload, authorization: str | None = Header(default=None)):
    claims = _claims(authorization)
    user_id = str(claims["uid"])
    provider = _normalize_provider(payload.provider)
    api_key = payload.api_key.strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="A 9Router API key is required")
    record = {
        "encryptedKey": _fernet().encrypt(api_key.encode()).decode(),
        "model": payload.model.strip() if isinstance(payload.model, str) else "",
        "fingerprint": _fingerprint(api_key),
        "enabled": True,
    }
    _set_key_record(user_id, provider, record)
    return {"success": True, "provider": provider, "fingerprint": record["fingerprint"]}


@router.get("/keys")
def list_agent_keys(authorization: str | None = Header(default=None)):
    claims = _claims(authorization)
    user_id = str(claims["uid"])
    record = _get_key_record(user_id, ROUTER_PROVIDER)
    if not isinstance(record, dict):
        return {"configured": False, "keys": []}
    return {
        "configured": bool(record.get("enabled", True)),
        "keys": [{
            "provider": ROUTER_PROVIDER,
            "enabled": bool(record.get("enabled", True)),
            "fingerprint": str(record.get("fingerprint") or ""),
        }],
    }


@router.delete("/keys/{provider}")
def delete_agent_key(provider: str, authorization: str | None = Header(default=None)):
    claims = _claims(authorization)
    user_id = str(claims["uid"])
    normalized_provider = _normalize_provider(provider)
    _delete_key_record(user_id, normalized_provider)
    return {"success": True, "provider": normalized_provider}


@router.get("/models")
async def agent_models(authorization: str | None = Header(default=None)):
    claims = _claims(authorization)
    user_id = str(claims["uid"])
    saved = _saved_router_key(user_id)
    if saved is None:
        return {"configured": False, "provider": ROUTER_PROVIDER, "models": []}
    api_key, _ = saved
    models = await _fetch_router_models(api_key)
    return {"configured": True, "provider": ROUTER_PROVIDER, "models": models}


@router.post("/chat")
async def agent_chat(payload: AgentChatPayload, authorization: str | None = Header(default=None)):
    claims = _claims(authorization)
    images = _validate_chat_images(payload.images)
    user_id = str(claims["uid"])
    provider = _normalize_provider(payload.provider)
    selected_model = (payload.model or "").strip()
    if not selected_model:
        raise HTTPException(status_code=400, detail="Select an available model")
    saved = _saved_router_key(user_id)
    if saved is None:
        raise HTTPException(status_code=403, detail="A 9Router API key is not configured for this account")
    api_key, _ = saved
    available_models = await _fetch_router_models(api_key)
    if selected_model not in {model["id"] for model in available_models}:
        raise HTTPException(status_code=403, detail="Model is not available to this 9Router API key")

    request_body = {
        "model": selected_model,
        "messages": _chat_messages(payload, images),
        "temperature": 0.2,
        "stream": False,
    }
    response = await _router_request("POST", "chat/completions", api_key, request_body)
    _raise_router_http_error(response, "chat")
    try:
        upstream_payload = response.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="9Router returned an invalid chat response") from exc
    content, usage = _completion_text(upstream_payload)
    result_model = upstream_payload.get("model") if isinstance(upstream_payload, dict) else None
    return {
        "success": True,
        "userId": user_id,
        "provider": provider,
        "data": {
            "result": {
                "content": content,
                "model": result_model if isinstance(result_model, str) and result_model else selected_model,
                "usage": usage,
                "sources": [],
            }
        },
    }
