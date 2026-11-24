# modules/agent_local.py
# "AI Local": bridges aiagent.tsx <-> a single per-user GPU Kaggle worker
# (workerlocal.py). Kaggle kernels have no inbound port, so everything is a
# job the worker polls for and reports back -- same idea as the Downloader's
# session polling in modules/universal.py, but modelling a long-lived chat
# connection (one session per user) instead of one-off download jobs.
from __future__ import annotations

import asyncio
import hmac
import logging
import os
import time
import uuid
import secrets
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field

from . import kaggle_local_runner
from .kaggle_settings import authenticate_user, get_user_credentials

logger = logging.getLogger("agent_local")
router = APIRouter(prefix="/agent/local", tags=["agent-local"])

# Ganti via env var kalau slug dataset model lokal lu beda.
LOCAL_MODEL_DATASET = os.environ.get("KAGGLE_LOCAL_MODEL_DATASET", "herbanuwarthakusuma/media-ai-local")

SESSION_TTL_SECONDS = int(os.environ.get("AGENT_LOCAL_SESSION_TTL", str(6 * 60 * 60)))
CHAT_JOB_TIMEOUT_SECONDS = int(os.environ.get("AGENT_LOCAL_CHAT_TIMEOUT", "240"))
WORKSPACE_JOB_TIMEOUT_SECONDS = int(os.environ.get("AGENT_LOCAL_WORKSPACE_TIMEOUT", "60"))

_sessions: dict[str, dict[str, Any]] = {}
_sessions_lock = asyncio.Lock()


class ChatImagePayload(BaseModel):
    mime_type: str = Field(min_length=1, max_length=32)
    data: str = Field(min_length=1, max_length=12_000_000)


class LocalChatPayload(BaseModel):
    prompt: str = Field(min_length=1)
    images: list[ChatImagePayload] = Field(default_factory=list, max_length=8)


class WorkspaceJobPayload(BaseModel):
    op: str = Field(pattern="^(list|read|write|move|delete)$")
    op_payload: dict[str, Any] = Field(default_factory=dict)


class WorkerStatusPayload(BaseModel):
    status: str = Field(pattern="^(booting|ready|error|offline)$")
    model_id: str | None = None
    error: str | None = None


class WorkerPollPayload(BaseModel):
    closing: bool = False
    heartbeat_only: bool = False


class WorkerResultPayload(BaseModel):
    job_id: str
    ok: bool
    result: dict[str, Any] | None = None
    error: str | None = None


def _require_worker_token(session: dict[str, Any], supplied: str | None) -> None:
    expected = str(session.get("worker_token") or "")
    if not expected or not supplied or not hmac.compare_digest(expected.encode("utf-8"), supplied.encode("utf-8")):
        raise HTTPException(401, "Worker authentication required")


async def _get_session_for_uid(uid: str) -> dict[str, Any] | None:
    async with _sessions_lock:
        for sess in _sessions.values():
            if sess.get("owner_uid") == uid and sess.get("expires_at", 0) > time.time():
                return sess
    return None


async def _get_session(session_id: str) -> dict[str, Any] | None:
    async with _sessions_lock:
        sess = _sessions.get(session_id)
        if sess and sess.get("expires_at", 0) > time.time():
            return sess
    return None


@router.post("/connect")
async def connect(request: Request, authorization: str | None = Header(default=None)):
    """Called when the user opens the 'Local' tab / taps connect. Reuses an
    already-booted worker for this account if one is still alive."""
    uid = authenticate_user(authorization)
    credentials = get_user_credentials(uid)
    if not credentials:
        raise HTTPException(403, "Save this account's Kaggle username and API token first")

    existing = await _get_session_for_uid(uid)
    snapshot = kaggle_local_runner.worker_snapshot(uid)
    if existing and snapshot.get("alive"):
        return {"ok": True, "session_id": existing["session_id"], "status": snapshot.get("status"),
                "model_id": snapshot.get("model_id"), "reused": True}

    worker_token = secrets.token_urlsafe(32)
    sid = uuid.uuid4().hex[:16]
    now = time.time()
    sess = {
        "session_id": sid, "owner_uid": uid, "worker_token": worker_token,
        "created_at": now, "expires_at": now + SESSION_TTL_SECONDS,
        "pending_job": None, "job_results": {},
    }
    async with _sessions_lock:
        _sessions[sid] = sess

    if not kaggle_local_runner.kaggle_enabled():
        raise HTTPException(503, "Kaggle CLI atau workerlocal.py tidak tersedia di backend")

    result = await kaggle_local_runner.start_worker(
        sid, kaggle_local_runner.resolve_backend_url(str(request.base_url)),
        uid, worker_token, credentials, LOCAL_MODEL_DATASET,
    )
    if not result.get("ok"):
        async with _sessions_lock:
            _sessions.pop(sid, None)
        raise HTTPException(502, result.get("error") or "Gagal menjalankan Kaggle worker")

    return {"ok": True, "session_id": sid, "status": "booting", "model_id": None, "reused": False}


@router.get("/status")
async def status(authorization: str | None = Header(default=None)):
    uid = authenticate_user(authorization)
    sess = await _get_session_for_uid(uid)
    if not sess:
        return {"ok": True, "connected": False, "status": "offline", "model_id": None}
    snapshot = kaggle_local_runner.worker_snapshot(uid)
    return {
        "ok": True, "connected": True, "session_id": sess["session_id"],
        "status": snapshot.get("status"), "model_id": snapshot.get("model_id"),
        "error": snapshot.get("error"), "alive": snapshot.get("alive"),
    }


async def _enqueue_and_wait(uid: str, job_type: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    sess = await _get_session_for_uid(uid)
    if not sess:
        raise HTTPException(409, "AI Local belum terhubung. Sambungkan dulu lewat tab Local.")
    snapshot = kaggle_local_runner.worker_snapshot(uid)
    if snapshot.get("status") != "ready":
        raise HTTPException(409, f"AI Local sedang '{snapshot.get('status')}', belum siap menerima pesan")

    job_id = uuid.uuid4().hex[:12]
    async with _sessions_lock:
        sess["pending_job"] = {"job_id": job_id, "type": job_type, "payload": payload}

    deadline = time.time() + timeout
    while time.time() < deadline:
        async with _sessions_lock:
            result = sess["job_results"].pop(job_id, None)
        if result is not None:
            return result
        await asyncio.sleep(0.5)

    async with _sessions_lock:
        if sess.get("pending_job", {}).get("job_id") == job_id:
            sess["pending_job"] = None
    raise HTTPException(504, "AI Local tidak membalas tepat waktu")


@router.post("/chat")
async def local_chat(payload: LocalChatPayload, authorization: str | None = Header(default=None)):
    uid = authenticate_user(authorization)
    job_payload = {"prompt": payload.prompt, "images": [img.model_dump() for img in payload.images]}
    result = await _enqueue_and_wait(uid, "chat", job_payload, CHAT_JOB_TIMEOUT_SECONDS)
    if not result.get("ok"):
        raise HTTPException(502, result.get("error") or "AI Local gagal memproses pesan")
    content = (result.get("result") or {}).get("content") or ""
    return {"success": True, "data": {"result": {"content": content, "model": "ai-local", "usage": {}, "sources": []}}}


async def run_workspace_job(uid: str, op: str, op_payload: dict[str, Any]) -> dict[str, Any]:
    """Called directly (same process) by modules/workspace.py when this user's
    active workspace is the Kaggle worker rather than the Sandbox runtime."""
    result = await _enqueue_and_wait(uid, "workspace", {"op": op, "op_payload": op_payload}, WORKSPACE_JOB_TIMEOUT_SECONDS)
    if not result.get("ok"):
        raise HTTPException(502, (result.get("result") or {}).get("error") or result.get("error") or "Workspace job failed")
    return (result.get("result") or {}).get("result")


def has_active_worker(uid: str) -> bool:
    snapshot = kaggle_local_runner.worker_snapshot(uid)
    return bool(snapshot.get("alive") and snapshot.get("status") == "ready")


# --------------------------- worker-facing endpoints ---------------------------

@router.get("/kaggle-config")
async def kaggle_config(session_id: str = Query(...), worker_token: str | None = Header(default=None, alias="X-Kaggle-Worker-Token")):
    sess = await _get_session(session_id)
    if not sess:
        raise HTTPException(410, "session not found or expired")
    _require_worker_token(sess, worker_token)
    return {"ok": True, "session_id": session_id}


@router.post("/kaggle-status")
async def kaggle_status(payload: WorkerStatusPayload, session_id: str = Query(...),
                         worker_token: str | None = Header(default=None, alias="X-Kaggle-Worker-Token")):
    sess = await _get_session(session_id)
    if not sess:
        raise HTTPException(410, "session not found or expired")
    _require_worker_token(sess, worker_token)
    kaggle_local_runner.set_worker_status(sess["owner_uid"], status=payload.status, model_id=payload.model_id, error=payload.error)
    kaggle_local_runner.touch_worker(sess["owner_uid"], session_id)
    return {"ok": True}


@router.post("/kaggle-poll")
async def kaggle_poll(payload: WorkerPollPayload, session_id: str = Query(...),
                       worker_token: str | None = Header(default=None, alias="X-Kaggle-Worker-Token")):
    sess = await _get_session(session_id)
    if not sess:
        raise HTTPException(410, "session not found or expired")
    _require_worker_token(sess, worker_token)
    kaggle_local_runner.touch_worker(sess["owner_uid"], session_id)
    if payload.heartbeat_only:
        return {"ok": True, "job": None}
    if payload.closing:
        kaggle_local_runner.set_worker_status(sess["owner_uid"], status="offline")
        return {"ok": True, "job": None}
    async with _sessions_lock:
        job = sess.get("pending_job")
    return {"ok": True, "job": job}


@router.post("/kaggle-result")
async def kaggle_result(payload: WorkerResultPayload, session_id: str = Query(...),
                         worker_token: str | None = Header(default=None, alias="X-Kaggle-Worker-Token")):
    sess = await _get_session(session_id)
    if not sess:
        raise HTTPException(410, "session not found or expired")
    _require_worker_token(sess, worker_token)
    async with _sessions_lock:
        if sess.get("pending_job", {}).get("job_id") == payload.job_id:
            sess["pending_job"] = None
        sess["job_results"][payload.job_id] = {"ok": payload.ok, "result": payload.result, "error": payload.error}
    return {"ok": True}
