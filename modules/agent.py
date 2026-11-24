# /warthadev-backend/modules/agent.py
import asyncio
import base64
import binascii
import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any

import aiohttp
from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import StreamingResponse
from firebase_admin import auth, firestore
from pydantic import BaseModel, Field

from .agent_prompt import DEFAULT_SYSTEM_INSTRUCTION
from .agent_ssrf import RouterTarget, make_pinned_connector, validate_router_base_url
from .firestore_storage import init_firestore

router = APIRouter(prefix="/agent", tags=["agent"])
logger = logging.getLogger(__name__)
OPENROUTER_PROVIDER = "openrouter"
OPENROUTER_API_BASE_URL = "https://openrouter.ai/api/v1"
MAX_ROUTER_RESPONSE_BYTES = 8 * 1024 * 1024


class AgentKeyPayload(BaseModel):
    provider: str = Field(min_length=1, max_length=80)
    base_url: str = Field(default="", max_length=2048)
    api_key: str | None = Field(default=None, max_length=500)
    # Accepted for compatibility with older clients; model choice is per chat.
    model: str | None = Field(default=None, max_length=200)


class AgentImagePayload(BaseModel):
    mime_type: str = Field(min_length=1, max_length=32)
    data: str = Field(min_length=1, max_length=12_000_000)


class AgentChatPayload(BaseModel):
    prompt: str = Field(min_length=1)
    provider: str = Field(default=OPENROUTER_PROVIDER, min_length=1, max_length=80)
    model: str | None = Field(default=None, max_length=200)
    # Kept in the request schema for compatibility, but no search/tools are sent upstream.
    web_search: bool = False
    search_query: str = Field(default="", max_length=500)
    timezone: str = Field(default="Asia/Jakarta", max_length=64)
    history: list[dict[str, Any]] = Field(default_factory=list, max_length=40)
    images: list[AgentImagePayload] = Field(default_factory=list, max_length=12)


@dataclass(frozen=True)
class RouterHTTPResponse:
    status_code: int
    body: bytes

    def json(self) -> Any:
        try:
            return json.loads(self.body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Invalid JSON") from exc


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
    if normalized != OPENROUTER_PROVIDER:
        raise HTTPException(status_code=400, detail="Only per-account OpenRouter API keys are supported")
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
    return None


def _set_key_record(user_id: str, provider: str, record: dict[str, Any]) -> None:
    _user_doc(user_id).set({"agentKeys": {provider: record}}, merge=True)


def _delete_key_record(user_id: str, provider: str) -> None:
    user_reference = _user_doc(user_id)
    user_snapshot = user_reference.get()
    user_data = user_snapshot.to_dict() if user_snapshot.exists else {}
    key_map = (user_data or {}).get("agentKeys")
    if isinstance(key_map, dict) and provider in key_map:
        user_reference.update({f"agentKeys.{provider}": firestore.DELETE_FIELD})
    _legacy_key_doc(user_id, provider).delete()


def _delete_legacy_router_config(user_id: str) -> None:
    """Remove this user's former plaintext legacy fields after an OpenRouter key is saved."""
    user_reference = _user_doc(user_id)
    snapshot = user_reference.get()
    user_data = snapshot.to_dict() if snapshot.exists else {}
    user_data = user_data or {}
    updates: dict[str, Any] = {}
    for field_name in ("nineRouterApiKey", "nineRouterModel"):
        if field_name in user_data:
            updates[field_name] = firestore.DELETE_FIELD
    if updates:
        user_reference.update(updates)


def _saved_openrouter_credentials(user_id: str) -> tuple[str, dict[str, Any]] | None:
    record = _get_key_record(user_id, OPENROUTER_PROVIDER)
    if not isinstance(record, dict) or not record.get("enabled", True):
        return None
    encrypted_key = record.get("encryptedKey")
    if isinstance(encrypted_key, str) and encrypted_key:
        try:
            api_key = _fernet().decrypt(encrypted_key.encode()).decode()
        except (InvalidToken, ValueError) as exc:
            raise HTTPException(status_code=500, detail="Stored OpenRouter key is invalid") from exc
        return api_key, record

    # During replacement, keep using the previous value until the new plaintext
    # value has been written and read back successfully.
    previous_key = record.get("previousApiKey")
    if isinstance(previous_key, str) and previous_key:
        return previous_key, record
    api_key = record.get("apiKey")
    if not isinstance(api_key, str) or not api_key:
        return None
    return api_key, record


def _persist_openrouter_plaintext_key(user_id: str, api_key: str, previous_record: dict[str, Any] | None) -> None:
    """Stage, read back, activate, read back, then remove legacy ciphertext."""
    user_reference = _user_doc(user_id)
    path = f"agentKeys.{OPENROUTER_PROVIDER}"
    record = previous_record if isinstance(previous_record, dict) else {}

    # The candidate is written separately so a failed write/readback leaves the
    # currently active encrypted or plaintext key untouched.
    user_reference.set({"agentKeys": {OPENROUTER_PROVIDER: {"pendingApiKey": api_key}}}, merge=True)
    staged_snapshot = user_reference.get()
    staged_data = staged_snapshot.to_dict() if staged_snapshot.exists else {}
    staged_record = ((staged_data or {}).get("agentKeys") or {}).get(OPENROUTER_PROVIDER, {})
    if not isinstance(staged_record, dict) or staged_record.get("pendingApiKey") != api_key:
        raise HTTPException(status_code=500, detail="OpenRouter key write could not be verified; the previous key was retained")

    updates: dict[str, Any] = {
        f"{path}.apiKey": api_key,
        f"{path}.pendingApiKey": firestore.DELETE_FIELD,
    }
    if not record.get("encryptedKey"):
        prior_plaintext = record.get("previousApiKey") or record.get("apiKey")
        if isinstance(prior_plaintext, str) and prior_plaintext:
            updates[f"{path}.previousApiKey"] = prior_plaintext
    user_reference.update(updates)

    active_snapshot = user_reference.get()
    active_data = active_snapshot.to_dict() if active_snapshot.exists else {}
    active_record = ((active_data or {}).get("agentKeys") or {}).get(OPENROUTER_PROVIDER, {})
    if not isinstance(active_record, dict) or active_record.get("apiKey") != api_key:
        raise HTTPException(status_code=500, detail="OpenRouter key readback failed; the previous key was retained")

    # Only now is the new value confirmed in the active field. Remove the old
    # encrypted/backup fields without touching any other account data.
    user_reference.update({
        f"{path}.encryptedKey": firestore.DELETE_FIELD,
        f"{path}.previousApiKey": firestore.DELETE_FIELD,
        f"{path}.pendingApiKey": firestore.DELETE_FIELD,
        f"{path}.fingerprint": firestore.DELETE_FIELD,
        f"{path}.enabled": True,
    })
    final_snapshot = user_reference.get()
    final_data = final_snapshot.to_dict() if final_snapshot.exists else {}
    final_record = ((final_data or {}).get("agentKeys") or {}).get(OPENROUTER_PROVIDER, {})
    if (
        not isinstance(final_record, dict)
        or final_record.get("apiKey") != api_key
        or "encryptedKey" in final_record
        or "previousApiKey" in final_record
        or "pendingApiKey" in final_record
    ):
        raise HTTPException(status_code=500, detail="OpenRouter plaintext key could not be verified after migration")


async def _read_router_response(content: Any) -> bytes:
    body = bytearray()
    async for chunk in content.iter_chunked(64 * 1024):
        body.extend(chunk)
        if len(body) > MAX_ROUTER_RESPONSE_BYTES:
            raise HTTPException(status_code=502, detail="OpenRouter response exceeded the size limit")
    return bytes(body)


async def _router_request(
    method: str,
    base_url: str,
    endpoint: str,
    api_key: str,
    json_body: dict[str, Any] | None = None,
) -> RouterHTTPResponse:
    target: RouterTarget = await validate_router_base_url(base_url)
    url = f"{target.base_url}/{endpoint.lstrip('/')}"
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    if json_body is not None:
        headers["Content-Type"] = "application/json"
    try:
        connector = make_pinned_connector(target)
        timeout = aiohttp.ClientTimeout(total=float(os.getenv("AGENT_TIMEOUT", "90")))
        async with aiohttp.ClientSession(connector=connector, timeout=timeout, trust_env=False) as client:
            async with client.request(
                method,
                url,
                headers=headers,
                json=json_body,
                allow_redirects=False,
            ) as response:
                if 300 <= response.status < 400:
                    raise HTTPException(status_code=502, detail="OpenRouter redirects are not allowed")
                body = await _read_router_response(response.content)
                return RouterHTTPResponse(status_code=response.status, body=body)
    except HTTPException:
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
        logger.warning("OpenRouter request failed endpoint=%s error=%s", endpoint, type(exc).__name__)
        raise HTTPException(status_code=502, detail="OpenRouter is unavailable") from exc


def _raise_router_http_error(response: RouterHTTPResponse, action: str) -> None:
    if 300 <= response.status_code < 400:
        raise HTTPException(status_code=502, detail="OpenRouter redirects are not allowed")
    if response.status_code in (401, 403):
        raise HTTPException(status_code=401, detail="OpenRouter rejected this account's API key or model access")
    if response.status_code == 429:
        raise HTTPException(status_code=429, detail="OpenRouter rate limit (429)")
    if response.status_code in (502, 503, 504):
        raise HTTPException(status_code=503, detail="OpenRouter is temporarily unavailable")
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"OpenRouter {action} request failed ({response.status_code})")


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


def _is_free_openrouter_model(item: Any) -> bool:
    if isinstance(item, str):
        model_id = item.strip().lower()
        return model_id == "openrouter/free" or model_id.endswith(":free")
    if not isinstance(item, dict) or not isinstance(item.get("id"), str):
        return False
    model_id = item["id"].strip().lower()
    if model_id == "openrouter/free" or model_id.endswith(":free"):
        return True
    pricing = item.get("pricing")
    if not isinstance(pricing, dict):
        return False
    try:
        prompt_price = float(pricing.get("prompt"))
        completion_price = float(pricing.get("completion"))
    except (TypeError, ValueError):
        return False
    return prompt_price == 0 and completion_price == 0


def _extract_free_openrouter_models(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        for key in ("data", "models", "result"):
            if isinstance(payload.get(key), (list, dict)):
                return _extract_free_openrouter_models(payload[key])
        return []
    if not isinstance(payload, list):
        return []
    free_entries = [entry for entry in payload if _is_free_openrouter_model(entry)]
    return _extract_models(free_entries)


async def _fetch_openrouter_models(api_key: str) -> list[dict[str, Any]]:
    response = await _router_request("GET", OPENROUTER_API_BASE_URL, "models", api_key)
    _raise_router_http_error(response, "model catalog")
    try:
        payload = response.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="OpenRouter returned an invalid model catalog") from exc
    return _extract_free_openrouter_models(payload)


async def _verify_openrouter_authentication(api_key: str) -> None:
    # The public models endpoint returns 200 even for invalid keys, so it cannot
    # authenticate a credential. /key requires an accepted API key.
    response = await _router_request("GET", OPENROUTER_API_BASE_URL, "key", api_key)
    _raise_router_http_error(response, "key verification")
    try:
        payload = response.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="OpenRouter returned invalid key metadata") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise HTTPException(status_code=502, detail="OpenRouter returned invalid key metadata")


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
        raise HTTPException(status_code=502, detail="OpenRouter returned an invalid chat response")
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
        raise HTTPException(status_code=502, detail="OpenRouter returned an empty chat response")
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    return text, usage


@router.put("/keys")
async def save_agent_key(payload: AgentKeyPayload, authorization: str | None = Header(default=None)):
    claims = _claims(authorization)
    user_id = str(claims["uid"])
    provider = _normalize_provider(payload.provider)
    previous_record = _get_key_record(user_id, provider)
    normalized_key = payload.api_key.strip() if isinstance(payload.api_key, str) else ""

    if normalized_key:
        api_key = normalized_key
    else:
        saved = _saved_openrouter_credentials(user_id)
        if saved is None:
            raise HTTPException(status_code=400, detail="An OpenRouter API key is required for this account")
        api_key = saved[0]

    await _verify_openrouter_authentication(api_key)
    models = await _fetch_openrouter_models(api_key)
    _persist_openrouter_plaintext_key(user_id, api_key, previous_record)
    # Remove only this user's former legacy plaintext fields after verification,
    # catalog validation, and plaintext Firestore readback have all succeeded.
    _delete_legacy_router_config(user_id)
    return {
        "success": True,
        "provider": provider,
        "apiKey": api_key,
        "migrationRequired": False,
        "freeModelCount": len(models),
    }


@router.get("/keys")
def list_agent_keys(authorization: str | None = Header(default=None)):
    claims = _claims(authorization)
    user_id = str(claims["uid"])
    saved = _saved_openrouter_credentials(user_id)
    if saved is None:
        return {"configured": False, "keys": []}
    api_key, record = saved
    return {
        "configured": True,
        "keys": [{
            "provider": OPENROUTER_PROVIDER,
            "enabled": True,
            "apiKey": api_key,
            "migrationRequired": bool(record.get("encryptedKey") or record.get("previousApiKey")),
        }],
    }


@router.delete("/keys/{provider}")
def delete_agent_key(provider: str, authorization: str | None = Header(default=None)):
    claims = _claims(authorization)
    user_id = str(claims["uid"])
    normalized_provider = _normalize_provider(provider)
    _delete_key_record(user_id, normalized_provider)
    return {"success": True, "provider": normalized_provider}


@router.post("/keys/openrouter/verify")
async def verify_openrouter_key(authorization: str | None = Header(default=None)):
    claims = _claims(authorization)
    user_id = str(claims["uid"])
    saved = _saved_openrouter_credentials(user_id)
    if saved is None:
        raise HTTPException(status_code=403, detail="Save this account's OpenRouter API key before verifying it")
    api_key, _ = saved
    await _verify_openrouter_authentication(api_key)
    models = await _fetch_openrouter_models(api_key)
    _persist_openrouter_plaintext_key(user_id, api_key, saved[1])
    # Remove only this user's former legacy plaintext fields after authenticated
    # verification, catalog success, and plaintext Firestore readback.
    _delete_legacy_router_config(user_id)
    return {
        "success": True,
        "provider": OPENROUTER_PROVIDER,
        "apiKey": api_key,
        "migrationRequired": False,
        "freeModelCount": len(models),
    }


@router.get("/models")
async def agent_models(authorization: str | None = Header(default=None)):
    claims = _claims(authorization)
    user_id = str(claims["uid"])
    saved = _saved_openrouter_credentials(user_id)
    if saved is None:
        return {"configured": False, "provider": OPENROUTER_PROVIDER, "models": []}
    api_key, _ = saved
    models = await _fetch_openrouter_models(api_key)
    return {"configured": True, "provider": OPENROUTER_PROVIDER, "models": models}


@router.post("/chat")
async def agent_chat(payload: AgentChatPayload, authorization: str | None = Header(default=None)):
    claims = _claims(authorization)
    images = _validate_chat_images(payload.images)
    user_id = str(claims["uid"])
    provider = _normalize_provider(payload.provider)
    selected_model = (payload.model or "").strip()
    if not selected_model:
        raise HTTPException(status_code=400, detail="Select an available model")
    saved = _saved_openrouter_credentials(user_id)
    if saved is None:
        raise HTTPException(status_code=403, detail="An OpenRouter API key is not configured for this account")
    api_key, _ = saved
    available_models = await _fetch_openrouter_models(api_key)
    if selected_model not in {model["id"] for model in available_models}:
        raise HTTPException(status_code=403, detail="Model is not available as a free model to this account's OpenRouter key")

    request_body = {
        "model": selected_model,
        "messages": _chat_messages(payload, images),
        "temperature": 0.2,
        "stream": False,
    }
    response = await _router_request("POST", OPENROUTER_API_BASE_URL, "chat/completions", api_key, request_body)
    _raise_router_http_error(response, "chat")
    try:
        upstream_payload = response.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="OpenRouter returned an invalid chat response") from exc
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


def _runtime_stream_url() -> str:
    runtime_url = os.getenv("AGENT_RUNTIME_URL", "").strip().rstrip("/")
    if not runtime_url:
        raise HTTPException(status_code=503, detail="Agent runtime is not configured")
    return f"{runtime_url}/v1/agent/run/stream"


@router.post("/chat/stream")
async def agent_chat_stream(payload: AgentChatPayload, authorization: str | None = Header(default=None)):
    """Relay the runtime's SSE stream to the browser: tokens, status, and tool
    activity flow through as they happen instead of one final payload."""
    claims = _claims(authorization)
    images = _validate_chat_images(payload.images)
    user_id = str(claims["uid"])
    _normalize_provider(payload.provider)
    selected_model = (payload.model or "").strip()
    if not selected_model:
        raise HTTPException(status_code=400, detail="Select an available model")
    saved = _saved_openrouter_credentials(user_id)
    if saved is None:
        raise HTTPException(status_code=403, detail="An OpenRouter API key is not configured for this account")
    api_key, _ = saved
    internal_key = os.getenv("AGENT_INTERNAL_API_KEY", "").strip()
    runtime_url = os.getenv("AGENT_RUNTIME_URL", "").strip().rstrip("/")
    if not runtime_url or not internal_key:
        raise HTTPException(status_code=503, detail="Agent runtime is not configured")
    owner_uid = os.getenv("AGENT_OWNER_UID", "").strip()

    upstream_body = {
        "prompt": payload.prompt,
        "model": selected_model,
        "provider_api_key": api_key,
        "user_uid": user_id,
        "web_search": payload.web_search,
        "search_query": payload.search_query,
        "timezone": payload.timezone,
        "history": payload.history,
        "images": [image.model_dump() for image in payload.images],
    }
    headers = {
        "Content-Type": "application/json",
        "X-Agent-Internal-Key": internal_key,
        "X-Agent-User-UID": user_id,
        "X-Agent-User-Role": "owner" if owner_uid and user_id == owner_uid else "user",
        "X-Agent-Owner-UID": owner_uid,
    }

    async def relay():
        timeout = aiohttp.ClientTimeout(total=float(os.getenv("AGENT_TIMEOUT", "210")), sock_read=120)
        try:
            async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as client:
                async with client.post(f"{runtime_url}/v1/agent/run/stream", headers=headers, json=upstream_body) as upstream:
                    if upstream.status >= 400:
                        body = await upstream.text()
                        yield f"data: {json.dumps({'type': 'error', 'status': upstream.status, 'message': body[:400]})}\n\n"
                        yield "data: [DONE]\n\n"
                        return
                    async for raw in upstream.content:
                        if raw:
                            yield raw.decode("utf-8", errors="ignore")
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            yield f"data: {json.dumps({'type': 'error', 'status': 502, 'message': 'Agent runtime is unavailable'})}\n\n"
            yield "data: [DONE]\n\n"

    return StreamingResponse(relay(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
