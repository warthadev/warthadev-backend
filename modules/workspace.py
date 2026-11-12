"""Firebase-authenticated proxy for per-user agent workspaces."""
from __future__ import annotations

import functools
import logging
import os
from typing import Any

import httpx
from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from firebase_admin import auth
from pydantic import BaseModel, Field

from .firestore_storage import init_firestore

router = APIRouter(prefix="/agent/workspace", tags=["agent-workspace"])
logger = logging.getLogger(__name__)
OWNER_EMAIL = "warthadev@gmail.com"


class FileContentPayload(BaseModel):
    content: str


class MovePayload(BaseModel):
    source: str = Field(min_length=1, max_length=2048)
    destination: str = Field(min_length=1, max_length=2048)


class PackagePayload(BaseModel):
    package: str = Field(min_length=1, max_length=200)


class PackageReviewPayload(BaseModel):
    target_uid: str = Field(min_length=1, max_length=128)
    request_id: str = Field(min_length=1, max_length=64)
    decision: str = Field(pattern="^(approve|reject)$")


class LocalModelPayload(BaseModel):
    model: str = Field(min_length=1, max_length=200)
    action: str = Field(pattern="^(add|remove)$")


class HostActionConfirmationPayload(BaseModel):
    action_id: str = Field(min_length=20, max_length=128)


def _claims(authorization: str | None) -> dict[str, Any]:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authentication required")
    try:
        init_firestore()
        claims = auth.verify_id_token(authorization[7:].strip())
        if not claims.get("uid"):
            raise HTTPException(status_code=401, detail="Invalid authentication")
        return claims
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Invalid authentication") from exc


@functools.lru_cache(maxsize=1)
def _verified_owner_uid() -> str:
    """Resolve the fixed owner account to a Firebase UID, never from request data."""
    try:
        init_firestore()
        configured_uid = os.getenv("AGENT_OWNER_UID", "").strip()
        record = auth.get_user(configured_uid) if configured_uid else auth.get_user_by_email(OWNER_EMAIL)
        if (record.email or "").strip().lower() != OWNER_EMAIL or not record.email_verified:
            return ""
        return record.uid
    except Exception as exc:
        logger.warning("Owner UID is unavailable (%s)", type(exc).__name__)
        return ""


def is_owner_claims(claims: dict[str, Any]) -> bool:
    uid = str(claims.get("uid") or "")
    email = str(claims.get("email") or "").strip().lower()
    return bool(
        uid
        and uid == _verified_owner_uid()
        and email == OWNER_EMAIL
        and claims.get("email_verified") is True
    )


def _identity_headers(claims: dict[str, Any], internal_key: str) -> dict[str, str]:
    uid = str(claims["uid"])
    owner_uid = _verified_owner_uid()
    return {
        "X-Agent-Internal-Key": internal_key,
        "X-Agent-User-UID": uid,
        "X-Agent-User-Role": "owner" if is_owner_claims(claims) else "user",
        "X-Agent-Owner-UID": owner_uid,
    }


def _runtime_config() -> tuple[str, str]:
    runtime_url = os.getenv("AGENT_RUNTIME_URL", "").strip().rstrip("/")
    internal_key = os.getenv("AGENT_INTERNAL_API_KEY", "").strip()
    if not runtime_url or not internal_key:
        raise HTTPException(status_code=503, detail="Agent workspace runtime is not configured")
    try:
        parsed = httpx.URL(runtime_url)
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Agent runtime URL is invalid") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.host:
        raise HTTPException(status_code=500, detail="Agent runtime URL is invalid")
    return runtime_url, internal_key


async def runtime_request(
    path: str,
    claims: dict[str, Any],
    *,
    payload: dict[str, Any] | None = None,
    timeout: float = 60.0,
) -> dict[str, Any]:
    runtime_url, internal_key = _runtime_config()
    headers = _identity_headers(claims, internal_key)
    body = {"uid": str(claims["uid"]), **(payload or {})}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(f"{runtime_url}{path}", headers=headers, json=body)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Agent workspace runtime unavailable") from exc
    try:
        result = response.json()
    except ValueError:
        result = None
    if response.status_code >= 400:
        detail = result.get("detail") if isinstance(result, dict) else None
        if not isinstance(detail, (str, dict, list)):
            detail = "Agent workspace request failed"
        status = response.status_code if 400 <= response.status_code < 500 else 502
        raise HTTPException(status_code=status, detail=detail)
    if not isinstance(result, dict):
        raise HTTPException(status_code=502, detail="Agent workspace returned an invalid response")
    return result


def _require_owner(claims: dict[str, Any]) -> None:
    if not is_owner_claims(claims):
        raise HTTPException(status_code=403, detail="Owner access required")


@router.get("")
def workspace_info(authorization: str | None = Header(default=None)):
    claims = _claims(authorization)
    return {"uid": str(claims["uid"]), "owner": is_owner_claims(claims), "storage": "per-user"}


@router.get("/files")
async def list_workspace_files(path: str = Query(default=".", max_length=2048), authorization: str | None = Header(default=None)):
    claims = _claims(authorization)
    return await runtime_request("/v1/workspace/list", claims, payload={"path": path})


@router.get("/file")
async def read_workspace_file(path: str = Query(..., min_length=1, max_length=2048), authorization: str | None = Header(default=None)):
    claims = _claims(authorization)
    return await runtime_request("/v1/workspace/read", claims, payload={"path": path})


@router.put("/file")
async def write_workspace_file(payload: FileContentPayload, path: str = Query(..., min_length=1, max_length=2048), authorization: str | None = Header(default=None)):
    claims = _claims(authorization)
    return await runtime_request("/v1/workspace/write", claims, payload={"path": path, "content": payload.content})


@router.post("/move")
async def move_workspace_file(payload: MovePayload, authorization: str | None = Header(default=None)):
    claims = _claims(authorization)
    return await runtime_request("/v1/workspace/move", claims, payload=payload.model_dump())


@router.delete("/file")
async def delete_workspace_file(path: str = Query(..., min_length=1, max_length=2048), authorization: str | None = Header(default=None)):
    claims = _claims(authorization)
    return await runtime_request("/v1/workspace/delete", claims, payload={"path": path})


@router.get("/packages")
async def list_workspace_packages(authorization: str | None = Header(default=None)):
    claims = _claims(authorization)
    return await runtime_request("/v1/workspace/packages", claims)


@router.post("/packages")
async def request_workspace_package(payload: PackagePayload, authorization: str | None = Header(default=None)):
    claims = _claims(authorization)
    return await runtime_request("/v1/workspace/packages/request", claims, payload=payload.model_dump())


@router.get("/admin/packages")
async def admin_package_requests(authorization: str | None = Header(default=None)):
    claims = _claims(authorization)
    _require_owner(claims)
    return await runtime_request("/v1/workspace/admin/packages", claims)


@router.post("/admin/packages/review")
async def admin_review_package(payload: PackageReviewPayload, authorization: str | None = Header(default=None)):
    claims = _claims(authorization)
    _require_owner(claims)
    return await runtime_request("/v1/workspace/admin/packages/review", claims, payload=payload.model_dump())


@router.get("/admin/models/local")
async def admin_list_local_models(authorization: str | None = Header(default=None)):
    claims = _claims(authorization)
    _require_owner(claims)
    return await runtime_request("/v1/workspace/admin/models/local", claims, payload={"action": "list"})


@router.post("/admin/models/local")
async def admin_update_local_model(payload: LocalModelPayload, authorization: str | None = Header(default=None)):
    claims = _claims(authorization)
    _require_owner(claims)
    return await runtime_request("/v1/workspace/admin/models/local", claims, payload=payload.model_dump())


@router.post("/host-actions/confirm")
async def confirm_host_action(payload: HostActionConfirmationPayload, authorization: str | None = Header(default=None)):
    claims = _claims(authorization)
    _require_owner(claims)
    return await runtime_request("/v1/agent/host-actions/confirm", claims, payload=payload.model_dump())
