# modules/agent_local.py
# "AI Local": bridges aiagent.tsx <-> a single per-user GPU Kaggle worker
# (workerlocal.py). Kaggle kernels have no inbound port, so everything is a
# job the worker polls for and reports back -- same idea as the Downloader's
# session polling in modules/universal.py, but modelling a long-lived chat
# connection (one session per user) instead of one-off download jobs.
from __future__ import annotations

import asyncio
import base64
import binascii
import hmac
import logging
import os
import re
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
UPLOAD_JOB_TIMEOUT_SECONDS = int(os.environ.get("AGENT_LOCAL_UPLOAD_TIMEOUT", "120"))
UPLOAD_LIMIT_BYTES = int(os.environ.get("AGENT_LOCAL_UPLOAD_LIMIT_BYTES", str(20 * 1024 * 1024)))
UPLOAD_DIR = "/kaggle/working/uploads"

_sessions: dict[str, dict[str, Any]] = {}
_sessions_lock = asyncio.Lock()


class ChatImagePayload(BaseModel):
    mime_type: str = Field(min_length=1, max_length=32)
    data: str = Field(min_length=1, max_length=12_000_000)


class LocalChatPayload(BaseModel):
    prompt: str = Field(min_length=1)
    images: list[ChatImagePayload] = Field(default_factory=list, max_length=8)


class LocalUploadPayload(BaseModel):
    name: str = Field(min_length=1, max_length=240)
    content_base64: str = Field(min_length=1, max_length=28_000_000)
    mime_type: str = Field(default="application/octet-stream", max_length=160)


def _safe_upload_name(name: str) -> str:
    base = os.path.basename(name.replace("\\", "/")).strip()
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._") or "file"
    return base[:160]


class WorkspaceJobPayload(BaseModel):
    op: str = Field(pattern="^(list|read|write|move|delete)$")
    op_payload: dict[str, Any] = Field(default_factory=dict)


class WorkerStatusPayload(BaseModel):
    status: str = Field(pattern="^(booting|ready|error|offline)$")
    model_id: str | None = None
    error: str | None = None
    # Boot sub-phase reported by the worker while status == "booting".
    phase: str | None = Field(default=None, pattern="^(worker|packages|model)$")
    # Live activity line while a chat/tool job is running (shown in the UI as streaming status).
    activity: str | None = Field(default=None, max_length=240)


class WorkerActivityPayload(BaseModel):
    activity: str | None = Field(default=None, max_length=240)


class ConnectPayload(BaseModel):
    # force=True is the manual "Restart Kaggle" button: drop the current session and push a fresh kernel run.
    force: bool = False


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
    """Sesi TERBARU milik uid. (Sebelumnya mengembalikan sesi pertama/lama, sehingga
    setelah reconnect pesan masuk ke sesi lama yang tidak di-poll worker baru.)"""
    now = time.time()
    async with _sessions_lock:
        mine = [s for s in _sessions.values() if s.get("owner_uid") == uid and s.get("expires_at", 0) > now]
    return max(mine, key=lambda s: s.get("created_at", 0)) if mine else None


async def _drop_sessions_for_uid(uid: str) -> None:
    async with _sessions_lock:
        for sid in [sid for sid, s in _sessions.items() if s.get("owner_uid") == uid]:
            _sessions.pop(sid, None)


async def _get_session(session_id: str) -> dict[str, Any] | None:
    async with _sessions_lock:
        sess = _sessions.get(session_id)
        if sess and sess.get("expires_at", 0) > time.time():
            return sess
    return None


@router.post("/connect")
async def connect(request: Request, payload: ConnectPayload | None = None, authorization: str | None = Header(default=None)):
    """Called by "Start on Kaggle" / "Restart Kaggle".
    Reuses a worker that is still alive (unless force=True); if the worker is gone (Kaggle slept,
    went idle or was stopped) the old session is dropped and the Kaggle kernel is pushed again.
    The kernel is created automatically on the first run, so a new user needs no notebook of their own."""
    force = bool(payload and payload.force)
    uid = authenticate_user(authorization)
    credentials = get_user_credentials(uid)
    if not credentials:
        raise HTTPException(403, "Save this account's Kaggle username and API token first.")
    if not kaggle_local_runner.kaggle_enabled():
        raise HTTPException(503, "The Kaggle CLI or workerlocal.py is not available on the backend.")

    dataset = await kaggle_local_runner.dataset_access(uid, credentials, LOCAL_MODEL_DATASET)
    if dataset.get("state") in ("forbidden", "not_found"):
        raise HTTPException(409, _dataset_blocker_message(credentials["username"]))

    existing = await _get_session_for_uid(uid)
    snapshot = kaggle_local_runner.worker_snapshot(uid)
    if not force and existing and snapshot.get("alive") and snapshot.get("status") in ("booting", "ready"):
        return {"ok": True, "session_id": existing["session_id"], "status": snapshot.get("status"),
                "model_id": snapshot.get("model_id"), "reused": True}

    # Worker is gone (or a restart was forced) -> drop every old session so none are orphaned.
    await _drop_sessions_for_uid(uid)
    kaggle_local_runner.set_worker_status(uid, status="offline")

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

    result = await kaggle_local_runner.start_worker(
        sid, kaggle_local_runner.resolve_backend_url(str(request.base_url)),
        uid, worker_token, credentials, LOCAL_MODEL_DATASET,
    )
    if not result.get("ok"):
        async with _sessions_lock:
            _sessions.pop(sid, None)
        raise HTTPException(502, result.get("error") or "Failed to start the Kaggle worker.")

    return {"ok": True, "session_id": sid, "status": "booting", "model_id": None, "reused": False}


def _dataset_blocker_message(username: str) -> str:
    return (
        f"The Kaggle account \"{username}\" cannot access the model dataset ({LOCAL_MODEL_DATASET}). "
        "Ask the dataset owner to share it with this Kaggle username, then check again."
    )


@router.get("/preflight")
async def preflight(authorization: str | None = Header(default=None)):
    """Everything that must be true BEFORE a worker can start, so the UI can explain what is
    missing instead of failing after minutes of booting. A missing kernel is NOT a blocker:
    the first "Start" creates it."""
    uid = authenticate_user(authorization)
    credentials = get_user_credentials(uid)
    cli_ok = kaggle_local_runner.kaggle_enabled()
    checks: list[dict[str, Any]] = [
        {"id": "credentials", "label": "Kaggle account connected", "ok": bool(credentials)},
        {"id": "backend", "label": "Backend can control Kaggle", "ok": cli_ok},
    ]
    blocker: str | None = None
    message: str | None = None
    kernel_state: str | None = None
    if not credentials:
        blocker, message = "no-credentials", "Connect your Kaggle account first."
    elif not cli_ok:
        blocker, message = "no-backend", "The backend cannot control Kaggle right now. Please try again later."
    else:
        dataset, kernel = await asyncio.gather(
            kaggle_local_runner.dataset_access(uid, credentials, LOCAL_MODEL_DATASET),
            kaggle_local_runner.kernel_status(uid, credentials),
        )
        dataset_state = dataset.get("state")
        kernel_state = kernel.get("state")
        checks.append({
            "id": "dataset",
            "label": "Model dataset accessible",
            "ok": dataset_state not in ("forbidden", "not_found"),
            "detail": None if dataset_state == "ready" else dataset_state,
        })
        checks.append({
            "id": "kernel",
            "label": "Worker notebook on Kaggle",
            "ok": kernel_state not in ("not_found",),
            "detail": "It will be created automatically on the first start." if kernel_state == "not_found" else kernel_state,
        })
        if dataset_state in ("forbidden", "not_found"):
            blocker, message = "no-dataset", _dataset_blocker_message(credentials["username"])
    return {
        "ok": True,
        "can_start": blocker is None,
        "blocker": blocker,
        "message": message,
        "kernel_exists": kernel_state not in (None, "not_found"),
        "checks": checks,
    }


# Ordered boot pipeline shown in the UI. Each step is derived from real signals
# (push time, Kaggle kernel state, worker heartbeat, worker-reported phase).
_BOOT_STEPS: list[tuple[str, str, str]] = [
    ("push", "Sending the worker to Kaggle", "Uploading the notebook through the Kaggle API."),
    ("gpu", "Waiting for a Kaggle GPU", "Kaggle is queuing the notebook and assigning a GPU. This can take a few minutes when it is busy."),
    ("worker", "Worker connected", "The notebook is running and is about to check in with the backend."),
    ("packages", "Preparing packages", "Installing document and media tools inside the worker."),
    ("model", "Loading the model onto the GPU", "Copying the model and starting the inference server. This is usually the longest step."),
    ("ready", "Model ready", "You can start chatting."),
]
_PHASE_DONE_INDEX = {"worker": 2, "packages": 2, "model": 3}


def _build_boot_steps(state: str, snapshot: dict[str, Any], kaggle_state: str | None) -> list[dict[str, Any]]:
    if state == "ready":
        done_upto = len(_BOOT_STEPS) - 1
    else:
        done_upto = -1
        if (snapshot.get("last_push") or 0) > 0:
            done_upto = 0
        if kaggle_state == "running":
            done_upto = max(done_upto, 1)
        if (snapshot.get("last_heartbeat") or 0) > 0:
            done_upto = max(done_upto, 2)
        phase = snapshot.get("phase")
        if phase in _PHASE_DONE_INDEX:
            done_upto = max(done_upto, _PHASE_DONE_INDEX[phase])
    steps: list[dict[str, Any]] = []
    for idx, (sid, label, detail) in enumerate(_BOOT_STEPS):
        if idx <= done_upto:
            step_state = "done"
        elif idx == done_upto + 1:
            step_state = "error" if state == "error" else "active"
        else:
            step_state = "pending"
        step: dict[str, Any] = {"id": sid, "label": label, "state": step_state}
        if step_state in ("active", "error"):
            step["detail"] = detail
            if sid == "gpu" and kaggle_state == "queued":
                step["detail"] = "Queued on Kaggle. Waiting for a free GPU."
        steps.append(step)
    return steps


@router.get("/status")
async def status(deep: bool = Query(default=False), authorization: str | None = Header(default=None)):
    """Status worker yang jujur. `deep=1` juga menanyakan status asli kernel ke Kaggle
    (dipakai untuk mendeteksi kernel yang crash / kuota GPU habis saat booting)."""
    uid = authenticate_user(authorization)
    sess = await _get_session_for_uid(uid)
    snapshot = kaggle_local_runner.worker_snapshot(uid)
    state = snapshot.get("status") or "offline"
    error = snapshot.get("error")

    # Worker masih "hidup" tapi sesi backend hilang (mis. backend Render restart): yatim -> anggap mati.
    if not sess and state in ("booting", "ready"):
        state, error = "offline", "The backend session was lost (server restart). Restart the worker."

    kaggle_state = None
    if deep and state in ("booting", "offline", "error"):
        credentials = get_user_credentials(uid)
        if credentials:
            info = await kaggle_local_runner.kernel_status(uid, credentials)
            kaggle_state = info.get("state")
            booting_for = time.time() - float(snapshot.get("last_push") or 0)
            if state == "booting" and kaggle_state in ("error", "complete", "cancelled") and booting_for > 45:
                detail = info.get("detail")
                state = "error"
                error = ("The Kaggle kernel stopped while booting"
                         + (f" ({detail})" if detail else f" (status: {kaggle_state})")
                         + ". Common causes: the weekly GPU quota is used up, the account is not phone-verified "
                         "(required for GPU and internet), or the model dataset is not accessible.")
                kaggle_local_runner.set_worker_status(uid, status="error", error=error)

    steps = _build_boot_steps(state, snapshot, kaggle_state) if state in ("booting", "ready") else []
    last_push = float(snapshot.get("last_push") or 0)
    return {
        "ok": True,
        "steps": steps,
        "boot_elapsed": int(time.time() - last_push) if state == "booting" and last_push else None,
        "phase": snapshot.get("phase"),
        "connected": bool(sess) and state in ("booting", "ready"),
        "session_id": sess["session_id"] if sess else None,
        "status": state,
        "model_id": snapshot.get("model_id") if state == "ready" else None,
        "error": error,
        "alive": bool(snapshot.get("alive")),
        "kaggle_state": kaggle_state,
        "activity": snapshot.get("activity") if state == "ready" else None,
        "resumable": state in ("offline", "error"),
    }


async def _enqueue_and_wait(uid: str, job_type: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    sess = await _get_session_for_uid(uid)
    if not sess:
        raise HTTPException(409, "AI Local is not connected yet. Start it from the Local tab first.")
    snapshot = kaggle_local_runner.worker_snapshot(uid)
    if snapshot.get("status") != "ready":
        if snapshot.get("status") in ("offline", "error"):
            raise HTTPException(409, "The AI Local worker is down (Kaggle slept or stopped). Restart it from the Local tab.")
        raise HTTPException(409, "AI Local is still loading the GPU and model and cannot take messages yet.")

    job_id = uuid.uuid4().hex[:12]
    async with _sessions_lock:
        sess["pending_job"] = {"job_id": job_id, "type": job_type, "payload": payload}

    deadline = time.time() + timeout
    while time.time() < deadline:
        async with _sessions_lock:
            result = sess["job_results"].pop(job_id, None)
        if result is not None:
            return result
        if kaggle_local_runner.worker_snapshot(uid).get("status") != "ready":
            async with _sessions_lock:
                if sess.get("pending_job", {}).get("job_id") == job_id:
                    sess["pending_job"] = None
            raise HTTPException(503, "The AI Local worker stopped mid-request (Kaggle slept or stopped). Restart it from the Local tab.")
        await asyncio.sleep(0.5)

    async with _sessions_lock:
        if sess.get("pending_job", {}).get("job_id") == job_id:
            sess["pending_job"] = None
    raise HTTPException(504, "AI Local did not reply in time.")


@router.post("/chat")
async def local_chat(payload: LocalChatPayload, authorization: str | None = Header(default=None)):
    uid = authenticate_user(authorization)
    job_payload = {"prompt": payload.prompt, "images": [img.model_dump() for img in payload.images]}
    result = await _enqueue_and_wait(uid, "chat", job_payload, CHAT_JOB_TIMEOUT_SECONDS)
    if not result.get("ok"):
        raise HTTPException(502, result.get("error") or "AI Local failed to process the message.")
    inner = result.get("result") or {}
    content = inner.get("content") or ""
    attachments = inner.get("attachments") if isinstance(inner.get("attachments"), list) else []
    payload_out = {"content": content, "model": "ai-local", "usage": {}, "sources": []}
    if attachments:
        payload_out["attachments"] = attachments
    return {"success": True, "data": {"result": payload_out}}


@router.post("/upload")
async def local_upload(payload: LocalUploadPayload, authorization: str | None = Header(default=None)):
    """Tulis file asli ke /kaggle/working/uploads lewat operasi workspace yang sudah ada.

    Satu job pada satu waktu (antrean yang sama dengan chat). Isi file tidak
    dimasukkan ke prompt; pemanggil hanya menerima path-nya.
    """
    uid = authenticate_user(authorization)
    try:
        raw = base64.b64decode(payload.content_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(400, "Invalid file.") from exc
    if not raw:
        raise HTTPException(400, "The file is empty.")
    if len(raw) > UPLOAD_LIMIT_BYTES:
        raise HTTPException(413, "The file exceeds the 20 MB limit.")
    filename = f"{uuid.uuid4().hex[:8]}_{_safe_upload_name(payload.name)}"
    path = f"{UPLOAD_DIR}/{filename}"
    result = await _enqueue_and_wait(
        uid,
        "workspace",
        {"op": "write", "op_payload": {"path": path, "content_base64": payload.content_base64}},
        UPLOAD_JOB_TIMEOUT_SECONDS,
    )
    if not result.get("ok"):
        raise HTTPException(502, result.get("error") or "Failed to write the file on Kaggle.")
    inner = result.get("result") or {}
    if isinstance(inner, dict) and inner.get("ok") is False:
        raise HTTPException(502, inner.get("error") or "Failed to write the file on Kaggle.")
    return {"success": True, "data": {"path": path, "size": len(raw), "name": filename, "mime_type": payload.mime_type}}


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
    kaggle_local_runner.set_worker_status(
        sess["owner_uid"],
        status=payload.status,
        model_id=payload.model_id,
        error=payload.error,
        phase=payload.phase,
        activity=payload.activity,
    )
    kaggle_local_runner.touch_worker(sess["owner_uid"], session_id)
    return {"ok": True}


@router.post("/kaggle-activity")
async def kaggle_activity(payload: WorkerActivityPayload, session_id: str = Query(...),
                          worker_token: str | None = Header(default=None, alias="X-Kaggle-Worker-Token")):
    """Lightweight live status from the worker while a chat job is running."""
    sess = await _get_session(session_id)
    if not sess:
        raise HTTPException(410, "session not found or expired")
    _require_worker_token(sess, worker_token)
    kaggle_local_runner.set_worker_activity(sess["owner_uid"], payload.activity)
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
