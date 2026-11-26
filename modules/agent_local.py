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
import json
import logging
import os
import re
import time
import uuid
import secrets
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from . import agent_memory
from . import kaggle_local_runner
from .kaggle_settings import authenticate_user, get_user_credentials

logger = logging.getLogger("agent_local")
router = APIRouter(prefix="/agent/local", tags=["agent-local"])

# Ganti via env var kalau slug dataset model lokal lu beda.
LOCAL_MODEL_DATASET = os.environ.get("KAGGLE_LOCAL_MODEL_DATASET", "herbanuwarthakusuma/media-ai-local")

# Sesi bersifat "sliding": diperpanjang tiap heartbeat worker, jadi tidak putus di tengah idle 3-6 jam.
SESSION_TTL_SECONDS = int(os.environ.get("AGENT_LOCAL_SESSION_TTL", str(12 * 60 * 60)))
# Sesi disimpan ke disk supaya restart/tidur backend (mis. Render) TIDAK membuat worker Kaggle dianggap hilang.
_SESSIONS_FILE = os.environ.get("AGENT_LOCAL_SESSIONS_FILE", "/tmp/wartha-agent-local-sessions.json")
_PERSIST_EVERY_SECONDS = 60
STOP_WAIT_SECONDS = 20
CHAT_JOB_TIMEOUT_SECONDS = int(os.environ.get("AGENT_LOCAL_CHAT_TIMEOUT", "900"))
UPLOAD_JOB_TIMEOUT_SECONDS = int(os.environ.get("AGENT_LOCAL_UPLOAD_TIMEOUT", "120"))
UPLOAD_LIMIT_BYTES = int(os.environ.get("AGENT_LOCAL_UPLOAD_LIMIT_BYTES", str(20 * 1024 * 1024)))
UPLOAD_DIR = "/kaggle/working/uploads"

_sessions: dict[str, dict[str, Any]] = {}
_sessions_lock = asyncio.Lock()
_PERSIST_FIELDS = ("session_id", "owner_uid", "worker_token", "created_at", "expires_at", "control")
_last_persist_at = 0.0


def _persist_sessions_unlocked() -> None:
    """Tulis sesi aktif ke disk (panggil saat memegang _sessions_lock). Hanya field kecil; job tidak disimpan."""
    global _last_persist_at
    try:
        snapshot = {sid: {k: s.get(k) for k in _PERSIST_FIELDS} for sid, s in _sessions.items()}
        tmp = _SESSIONS_FILE + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # berisi worker_token -> hanya owner proses
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(snapshot, handle)
        os.replace(tmp, _SESSIONS_FILE)
        _last_persist_at = time.time()
    except Exception as exc:
        logger.warning("Failed to persist AI Local sessions: %s", exc)


def _load_sessions() -> None:
    if not os.path.exists(_SESSIONS_FILE):
        return
    try:
        with open(_SESSIONS_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        now = time.time()
        for sid, saved in (data or {}).items():
            if not isinstance(saved, dict) or float(saved.get("expires_at") or 0) <= now:
                continue
            _sessions[sid] = {**{k: saved.get(k) for k in _PERSIST_FIELDS}, "pending_job": None, "job_results": {}, "job_streams": {}}
        logger.info("Restored %d AI Local session(s) from disk", len(_sessions))
    except Exception as exc:
        logger.warning("Failed to load AI Local sessions: %s", exc)


def _extend_session_unlocked(sess: dict[str, Any]) -> None:
    sess["expires_at"] = time.time() + SESSION_TTL_SECONDS
    if time.time() - _last_persist_at > _PERSIST_EVERY_SECONDS:
        _persist_sessions_unlocked()


_load_sessions()


class ChatImagePayload(BaseModel):
    mime_type: str = Field(min_length=1, max_length=32)
    data: str = Field(min_length=1, max_length=12_000_000)


class LocalChatPayload(BaseModel):
    prompt: str = Field(min_length=1)
    images: list[ChatImagePayload] = Field(default_factory=list, max_length=8)
    # Bahasa tampilan yang dipakai user (id/en), diteruskan ke worker supaya
    # label tool dan judul langkah mengikuti bahasa user.
    language: str | None = Field(default=None, max_length=8)
    # Konteks waktu & wilayah user (zona waktu, waktu lokal, locale) dari browser.
    context: dict[str, Any] | None = None
    # Riwayat bersama (cloud + lokal) dari frontend, supaya worker paham
    # pembahasan yang terjadi di luar gilirannya sendiri.
    history: list[dict[str, Any]] = Field(default_factory=list, max_length=40)


class LocalUploadPayload(BaseModel):
    name: str = Field(min_length=1, max_length=240)
    content_base64: str = Field(min_length=1, max_length=28_000_000)
    mime_type: str = Field(default="application/octet-stream", max_length=160)


def _safe_upload_name(name: str) -> str:
    base = os.path.basename(name.replace("\\", "/")).strip()
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._") or "file"
    return base[:160]


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
    # Satu langkah proses (berpikir / tool) untuk log yang bisa dibuka-tutup di chat.
    step: dict[str, Any] | None = None
    reset: bool = False
    # Sapaan pembuka dari worker: ditampilkan sebagai pesan chat sebelum proses jalan.
    opening: str | None = Field(default=None, max_length=600)


class ConnectPayload(BaseModel):
    # Dipertahankan agar klien lama tidak error. Sekarang diabaikan: "connect" TIDAK pernah membuat worker
    # kedua; restart/stop punya endpoint sendiri (/restart, /stop).
    force: bool = False


class WorkerPollPayload(BaseModel):
    closing: bool = False
    heartbeat_only: bool = False
    reason: str | None = Field(default=None, max_length=40)


class WorkerStreamPayload(BaseModel):
    # Satu potongan teks live dari worker: segmen = satu panggilan model
    # (sapaan, jawaban akhir, ...); delta = teks baru sejak potongan sebelumnya.
    job_id: str
    segment: int = 0
    delta: str = Field(default="", max_length=5000)


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
        _persist_sessions_unlocked()


async def _get_session(session_id: str) -> dict[str, Any] | None:
    async with _sessions_lock:
        sess = _sessions.get(session_id)
        if sess and sess.get("expires_at", 0) > time.time():
            return sess
    return None


async def _start_or_reuse(request: Request, uid: str, credentials: dict[str, str]) -> dict[str, Any]:
    """Satu-satunya jalur untuk menyalakan worker. Urutan:
    1. Worker milik user masih hidup di backend -> pakai lagi.
    2. Tanya Kaggle langsung (tanpa cache): kernel masih running/queued?
         - sesi masih dikenal  -> adopsi (tunggu heartbeat), JANGAN push baru.
         - sesi tidak dikenal  -> tolak; worker yatim itu mati sendiri < 1 menit, user bisa Run lagi.
    3. Tidak ada worker sama sekali -> baru push kernel baru."""
    existing = await _get_session_for_uid(uid)
    snapshot = kaggle_local_runner.worker_snapshot(uid)
    state = snapshot.get("status")

    if existing and state == "stopping" and snapshot.get("alive"):
        raise HTTPException(409, "The worker is still shutting down. Wait a few seconds, then press Run again.")
    if existing and snapshot.get("alive") and state in ("booting", "ready"):
        return {"ok": True, "session_id": existing["session_id"], "status": state,
                "model_id": snapshot.get("model_id"), "reused": True}

    kernel = await kaggle_local_runner.kernel_status(uid, credentials, fresh=True)
    if kernel.get("state") in ("running", "queued"):
        if existing:
            kaggle_local_runner.adopt_running_worker(uid, existing["session_id"])
            logger.info("Adopted running Kaggle worker uid_slug=%s", kaggle_local_runner.local_kernel_slug(uid))
            snap = kaggle_local_runner.worker_snapshot(uid)
            return {"ok": True, "session_id": existing["session_id"], "status": snap.get("status") or "booting",
                    "model_id": snap.get("model_id"), "reused": True, "adopted": True}
        raise HTTPException(
            409,
            "A worker is still running on your Kaggle account but is no longer linked to this app. "
            "It shuts itself down within about a minute. Press Run again after that — no second worker was created.",
        )

    # Tidak ada worker di Kaggle -> aman membuat yang baru.
    await _drop_sessions_for_uid(uid)
    kaggle_local_runner.set_worker_status(uid, status="offline")

    worker_token = secrets.token_urlsafe(32)
    sid = uuid.uuid4().hex[:16]
    now = time.time()
    sess = {
        "session_id": sid, "owner_uid": uid, "worker_token": worker_token,
        "created_at": now, "expires_at": now + SESSION_TTL_SECONDS, "control": None,
        "pending_job": None, "job_results": {}, "job_streams": {},
    }
    async with _sessions_lock:
        _sessions[sid] = sess
        _persist_sessions_unlocked()

    result = await kaggle_local_runner.start_worker(
        sid, kaggle_local_runner.resolve_backend_url(str(request.base_url)),
        uid, worker_token, credentials, LOCAL_MODEL_DATASET,
    )
    if not result.get("ok"):
        async with _sessions_lock:
            _sessions.pop(sid, None)
            _persist_sessions_unlocked()
        raise HTTPException(502, result.get("error") or "Failed to start the Kaggle worker.")

    return {"ok": True, "session_id": sid, "status": "booting", "model_id": None, "reused": False}


async def _prepare_start(authorization: str | None) -> tuple[str, dict[str, str]]:
    uid = authenticate_user(authorization)
    credentials = get_user_credentials(uid)
    if not credentials:
        raise HTTPException(403, "Save this account's Kaggle username and API token first.")
    if not kaggle_local_runner.kaggle_enabled():
        raise HTTPException(503, "The Kaggle CLI or workerlocal.py is not available on the backend.")
    dataset = await kaggle_local_runner.dataset_access(uid, credentials, LOCAL_MODEL_DATASET)
    if dataset.get("state") in ("forbidden", "not_found"):
        raise HTTPException(409, _dataset_blocker_message(credentials["username"]))
    return uid, credentials


@router.post("/connect")
async def connect(request: Request, payload: ConnectPayload | None = None, authorization: str | None = Header(default=None)):
    """Tombol "Run". Dipanggil HANYA oleh klik user (tidak pernah otomatis saat F5/buka tab).
    Selalu cek dulu apakah worker sudah ada (di backend dan di Kaggle) sebelum membuat yang baru."""
    uid, credentials = await _prepare_start(authorization)
    return await _start_or_reuse(request, uid, credentials)


@router.post("/restart")
async def restart(request: Request, authorization: str | None = Header(default=None)):
    """Tombol "Restart".
    - Worker hidup & siap  -> muat ulang model di worker yang SAMA (tanpa kernel Kaggle baru, tanpa antre GPU).
    - Worker sedang boot   -> tidak melakukan apa-apa (jangan menumpuk worker).
    - Worker tidak ada     -> sama seperti Run (cek Kaggle dulu)."""
    uid, credentials = await _prepare_start(authorization)
    existing = await _get_session_for_uid(uid)
    snapshot = kaggle_local_runner.worker_snapshot(uid)
    if existing and snapshot.get("alive") and snapshot.get("status") == "ready":
        async with _sessions_lock:
            existing["control"] = "restart"
            existing["pending_job"] = None
            existing["job_results"].clear()
            _persist_sessions_unlocked()
        kaggle_local_runner.mark_restarting_model(uid)
        kaggle_local_runner.reset_activity_log(uid)
        return {"ok": True, "session_id": existing["session_id"], "status": "booting", "model_id": None,
                "reused": True, "in_place": True}
    return await _start_or_reuse(request, uid, credentials)


@router.post("/cancel")
async def cancel_chat(authorization: str | None = Header(default=None)):
    """Tombol "Berhenti": batalkan jawaban yang sedang berjalan, worker tetap hidup.

    Berbeda dengan /stop yang mematikan sesi worker Kaggle: cancel hanya menghentikan
    satu jawaban ini. Worker tetap ready untuk chat berikutnya.
    """
    uid = authenticate_user(authorization)
    sess = await _get_session_for_uid(uid)
    if not sess:
        raise HTTPException(409, "AI Local is not connected yet. Start it from the Local tab first.")
    async with _sessions_lock:
        pending = sess.get("pending_job") or {}
        lang = _normalize_agent_language((pending.get("payload") or {}).get("language"))
        cancelled_text = "Dibatalkan oleh user." if lang == "id" else "Cancelled by user."
        if pending:
            job_id = pending.get("job_id")
            sess["pending_job"] = None
            sess["job_results"][job_id] = {"ok": True, "result": {"content": cancelled_text, "cancelled": True}}
        sess["control"] = "cancel"
        _persist_sessions_unlocked()
    return {"ok": True, "cancelled": True}


@router.post("/stop")
async def stop(authorization: str | None = Header(default=None)):
    """Tombol "Stop": hentikan SESI worker Kaggle milik user.
    - Worker yang sudah terhubung diberi perintah stop lewat `control`, lalu endpoint ini
      menunggu sampai worker menutup sesinya sendiri (kernel Kaggle selesai, GPU dilepas).
    - Apa pun yang terjadi, sesi SELALU dihapus oleh endpoint ini sebelum kembali: kalau
      worker lambat mati, sesinya tetap dihapus, jadi worker itu mati sendiri begitu
      check-in berikutnya dijawab 410. Stop tidak pernah menyisakan sesi "stopping".
    - Worker yang belum sempat terhubung (masih antre di Kaggle) langsung kehilangan
      sesinya, jadi mati sendiri begitu mulai jalan.
    Kaggle sendiri tidak punya API untuk mematikan kernel, jadi kernel selalu dimatikan
    dari dalam worker, bukan dari backend."""
    uid = authenticate_user(authorization)
    sess = await _get_session_for_uid(uid)
    snapshot = kaggle_local_runner.worker_snapshot(uid)
    connected = bool(sess and float(snapshot.get("last_heartbeat") or 0) > 0 and snapshot.get("alive"))

    if not connected:
        await _drop_sessions_for_uid(uid)
        kaggle_local_runner.set_worker_status(uid, status="offline")
        kaggle_local_runner.invalidate_kernel_status_cache(uid)
        return {"ok": True, "status": "offline", "stopped": True}

    async with _sessions_lock:
        sess["control"] = "stop"
        sess["pending_job"] = None
        _persist_sessions_unlocked()
    kaggle_local_runner.mark_stopping(uid)

    deadline = time.time() + STOP_WAIT_SECONDS
    while time.time() < deadline:
        if kaggle_local_runner.worker_snapshot(uid).get("status") == "offline":
            return {"ok": True, "status": "offline", "stopped": True}
        await asyncio.sleep(0.5)
    # Worker lambat mati: hapus sesi di sini juga. Sesi sudah berakhir, dan worker
    # yang masih hidup akan mati sendiri saat check-in berikutnya dijawab 410.
    await _drop_sessions_for_uid(uid)
    kaggle_local_runner.set_worker_status(uid, status="offline")
    kaggle_local_runner.invalidate_kernel_status_cache(uid)
    return {"ok": True, "status": "offline", "stopped": True}


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
    if not sess and state in ("booting", "ready", "stopping"):
        state, error = "offline", "The backend lost track of this worker. Press Run — the app checks Kaggle first and never starts a duplicate."

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
        "connected": bool(sess) and state in ("booting", "ready", "stopping"),
        "session_id": sess["session_id"] if sess else None,
        "status": state,
        "model_id": snapshot.get("model_id") if state == "ready" else None,
        "error": error,
        "alive": bool(snapshot.get("alive")),
        "kaggle_state": kaggle_state,
        "activity": snapshot.get("activity") if state == "ready" else None,
        "resumable": state in ("offline", "error"),
    }


async def _enqueue_job(uid: str, job_type: str, payload: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Periksa worker siap, lalu antrekan satu job. Dipakai /chat dan /chat-stream."""
    sess = await _get_session_for_uid(uid)
    if not sess:
        raise HTTPException(409, "AI Local is not connected yet. Start it from the Local tab first.")
    snapshot = kaggle_local_runner.worker_snapshot(uid)
    if snapshot.get("status") != "ready":
        if snapshot.get("status") == "stopping":
            raise HTTPException(409, "AI Local is shutting down. Press Run to start it again.")
        if snapshot.get("status") in ("offline", "error"):
            raise HTTPException(409, "The AI Local worker is down (Kaggle slept or stopped). Restart it from the Local tab.")
        raise HTTPException(409, "AI Local is still loading the GPU and model and cannot take messages yet.")

    job_id = uuid.uuid4().hex[:12]
    async with _sessions_lock:
        sess["pending_job"] = {"job_id": job_id, "type": job_type, "payload": payload}
        streams = sess.setdefault("job_streams", {})
        streams[job_id] = {"events": []}
        # Simpan stream maksimal 3 job terakhir supaya memori tidak tumbuh tanpa batas.
        if len(streams) > 3:
            for old_id in list(streams)[: len(streams) - 3]:
                streams.pop(old_id, None)
    return sess, job_id


async def _enqueue_and_wait(uid: str, job_type: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    sess, job_id = await _enqueue_job(uid, job_type, payload)

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
            snap = kaggle_local_runner.worker_snapshot(uid)
            msg = "The AI Local worker stopped mid-request (Kaggle slept or stopped). Restart it from the Local tab."
            if snap.get("raw_status") == "error" and snap.get("error"):
                msg += f" System log: {snap['error']}"
            raise HTTPException(503, msg)
        await asyncio.sleep(0.5)

    async with _sessions_lock:
        if sess.get("pending_job", {}).get("job_id") == job_id:
            sess["pending_job"] = None
    raise HTTPException(504, "AI Local did not reply in time.")


@router.post("/chat")
def _normalize_agent_language(value: Any) -> str:
    lang = str(value or "").strip().lower()
    if lang.startswith("id"):
        return "id"
    if lang.startswith("en"):
        return "en"
    return "id"


async def local_tools_available(uid: str) -> bool:
    """True kalau sesi lokal user sedang ready — syarat cloud boleh menawarkan tool Kaggle."""
    sess = await _get_session_for_uid(uid)
    if not sess:
        return False
    return kaggle_local_runner.worker_snapshot(uid).get("status") == "ready"


async def run_kaggle_tool(uid: str, name: str, args: dict[str, Any], timeout: float = 300.0) -> str | dict[str, Any]:
    """Jalankan SATU tool di worker Kaggle user dan kembalikan hasil teksnya.
    Error (worker mati / timeout) dilempar sebagai exception supaya pemanggil
    bisa memberikannya ke model sebagai hasil tool yang gagal, bukan mematikan turn.
    Satu pengecualian: inspect_image mengembalikan dict {"content": ..., "image": {...}}
    supaya gambarnya bisa diteruskan ke model sebagai pesan visual, bukan sebagai
    teks mentah — sebelumnya cloud hanya menerima repr dict-nya dan gambar tidak
    pernah sampai ke model. Pengecualian kedua: request_access mengembalikan
    {"content": ..., "pending_request": {...}} supaya permintaan ke user tidak
    hilang di jalan."""
    result = await _enqueue_and_wait(uid, "tool", {"name": name, "args": args}, timeout)
    if isinstance(result, dict) and result.get("ok"):
        inner = result.get("result") or {}
        if isinstance(inner, dict):
            if isinstance(inner.get("image"), dict):
                return {"content": str(inner.get("content") or ""), "image": inner["image"]}
            if isinstance(inner.get("pending_request"), dict):
                return {"content": str(inner.get("content") or ""), "pending_request": inner["pending_request"]}
            return str(inner.get("content") or "")
        return str(inner)
    err = result.get("error") if isinstance(result, dict) else None
    raise RuntimeError(str(err or "Tool failed on the Kaggle worker"))


async def local_chat(payload: LocalChatPayload, authorization: str | None = Header(default=None)):
    uid = authenticate_user(authorization)
    base_context = payload.context if isinstance(payload.context, dict) else {}
    job_payload = {"prompt": payload.prompt, "images": [img.model_dump() for img in payload.images],
                   "language": _normalize_agent_language(payload.language),
                   "context": {**base_context, **agent_memory.load_memory_context(uid)},
                   "history": payload.history if isinstance(payload.history, list) else []}
    kaggle_local_runner.reset_activity_log(uid)
    result = await _enqueue_and_wait(uid, "chat", job_payload, CHAT_JOB_TIMEOUT_SECONDS)
    if not result.get("ok"):
        raise HTTPException(502, result.get("error") or "AI Local failed to process the message.")
    inner = result.get("result") or {}
    # Memories the worker collected via the remember tool: persist them now
    # (the worker has no Firestore access). Best-effort — never fail the chat.
    for mem in (inner.get("memories_saved") or []):
        try:
            agent_memory.save_memory(uid, str(mem), source="ai")
        except Exception:
            pass
    # Plans the worker saved via the update_plan tool: persist them now
    # (the worker has no Firestore access). Best-effort — never fail the chat.
    if isinstance(inner.get("plans_saved"), list):
        try:
            agent_memory.save_plan(uid, inner["plans_saved"])
        except Exception:
            pass
    content = inner.get("content") or ""
    attachments = inner.get("attachments") if isinstance(inner.get("attachments"), list) else []
    payload_out = {"content": content, "model": "ai-local", "usage": {}, "sources": []}
    if attachments:
        payload_out["attachments"] = attachments
    if isinstance(inner.get("search_images"), list) and inner["search_images"]:
        payload_out["search_images"] = inner["search_images"]
    if isinstance(inner.get("pending_request"), dict) and inner["pending_request"]:
        payload_out["pending_request"] = inner["pending_request"]
    return {"success": True, "data": {"result": payload_out}}


@router.post("/chat-stream")
async def local_chat_stream(payload: LocalChatPayload, authorization: str | None = Header(default=None)):
    """Chat AI Local sebagai SSE live: token mengalir selagi model menulis,
    lalu satu event final berisi jawaban lengkap + lampiran. Sama seperti /chat,
    hanya saja jawabannya tidak menunggu sampai selesai."""
    uid = authenticate_user(authorization)
    base_context = payload.context if isinstance(payload.context, dict) else {}
    job_payload = {"prompt": payload.prompt, "images": [img.model_dump() for img in payload.images],
                   "language": _normalize_agent_language(payload.language),
                   "context": {**base_context, **agent_memory.load_memory_context(uid)},
                   "history": payload.history if isinstance(payload.history, list) else []}
    kaggle_local_runner.reset_activity_log(uid)
    sess, job_id = await _enqueue_job(uid, "chat", job_payload)

    async def _events():
        idx = 0
        deadline = time.time() + CHAT_JOB_TIMEOUT_SECONDS
        try:
            while True:
                async with _sessions_lock:
                    stream = (sess.get("job_streams") or {}).get(job_id) or {"events": []}
                    events = stream["events"]
                    result = sess["job_results"].get(job_id)
                while idx < len(events):
                    ev = events[idx]
                    idx += 1
                    yield ("data: " + json.dumps({"type": "token", "segment": ev.get("segment", 0),
                                                  "delta": ev.get("delta", "")}, ensure_ascii=False) + "\n\n")
                if result is not None:
                    async with _sessions_lock:
                        sess["job_results"].pop(job_id, None)
                    if not result.get("ok"):
                        yield ("data: " + json.dumps({"type": "error",
                                                      "message": result.get("error") or "AI Local failed to process the message."},
                                                     ensure_ascii=False) + "\n\n")
                    else:
                        inner = result.get("result") or {}
                        out: dict[str, Any] = {"content": inner.get("content") or "", "model": "ai-local",
                                               "usage": {}, "sources": []}
                        if isinstance(inner.get("attachments"), list) and inner["attachments"]:
                            out["attachments"] = inner["attachments"]
                        if isinstance(inner.get("search_images"), list) and inner["search_images"]:
                            out["search_images"] = inner["search_images"]
                        if isinstance(inner.get("pending_request"), dict) and inner["pending_request"]:
                            out["pending_request"] = inner["pending_request"]
                        # Memories the worker collected via remember: persist
                        # them now (best-effort, never fail the chat).
                        for mem in (inner.get("memories_saved") or []):
                            try:
                                agent_memory.save_memory(uid, str(mem), source="ai")
                            except Exception:
                                pass
                        # Plans the worker saved via update_plan: same.
                        if isinstance(inner.get("plans_saved"), list):
                            try:
                                agent_memory.save_plan(uid, inner["plans_saved"])
                            except Exception:
                                pass
                        yield ("data: " + json.dumps({"type": "final", "result": out}, ensure_ascii=False) + "\n\n")
                    break
                snap = kaggle_local_runner.worker_snapshot(uid)
                if snap.get("status") != "ready":
                    err_event: dict[str, Any] = {"type": "error",
                                                 "message": "The AI Local worker stopped mid-request (Kaggle slept or stopped). Restart it from the Local tab."}
                    # Worker sempat melaporkan penyebab matinya (FATAL / ekor log
                    # llama-server): sertakan supaya aplikasi bisa menampilkannya
                    # sebagai System log, bukan cuma pesan generik. Kalau kernel
                    # mati diam-diam tanpa laporan, tidak ada detail yang bisa
                    # ditampilkan — pesannya tetap generik seperti sebelumnya.
                    if snap.get("raw_status") == "error" and snap.get("error"):
                        err_event["detail"] = str(snap["error"])
                    yield ("data: " + json.dumps(err_event, ensure_ascii=False) + "\n\n")
                    break
                if time.time() >= deadline:
                    yield ("data: " + json.dumps({"type": "error", "message": "AI Local did not reply in time."},
                                                 ensure_ascii=False) + "\n\n")
                    break
                await asyncio.sleep(0.1)
        finally:
            async with _sessions_lock:
                if sess.get("pending_job", {}).get("job_id") == job_id:
                    sess["pending_job"] = None

    return StreamingResponse(_events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/kaggle-stream")
async def kaggle_stream(payload: WorkerStreamPayload, session_id: str = Query(...),
                        worker_token: str | None = Header(default=None, alias="X-Kaggle-Worker-Token")):
    """Satu potongan teks live dari worker untuk job yang sedang berjalan."""
    sess = await _get_session(session_id)
    if not sess:
        raise HTTPException(410, "session not found or expired")
    _require_worker_token(sess, worker_token)
    async with _sessions_lock:
        streams = sess.setdefault("job_streams", {})
        entry = streams.setdefault(payload.job_id, {"events": []})
        events = entry["events"]
        events.append({"segment": payload.segment, "delta": payload.delta})
        # Batasi memori: buang potongan paling lama kalau sudah sangat panjang.
        if len(events) > 5000:
            del events[: len(events) - 5000]
    return {"ok": True}


@router.get("/activity")
async def local_activity(since: int = Query(default=0, ge=0), authorization: str | None = Header(default=None)):
    """Log langkah proses (berpikir / tool) milik chat yang sedang berjalan. Murah, aman di-poll tiap ~1 detik."""
    uid = authenticate_user(authorization)
    snapshot = kaggle_local_runner.worker_snapshot(uid)
    log = kaggle_local_runner.get_activity_log(uid, since)
    return {"ok": True, "activity": snapshot.get("activity"), "seq": log["seq"], "steps": log["steps"],
            "opening": kaggle_local_runner.get_worker_opening(uid)}


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
    async with _sessions_lock:
        _extend_session_unlocked(sess)
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
    uid = sess["owner_uid"]
    if payload.reset:
        kaggle_local_runner.reset_activity_log(uid)
    if payload.step:
        kaggle_local_runner.upsert_activity_step(uid, payload.step)
    if payload.opening:
        kaggle_local_runner.set_worker_opening(uid, payload.opening)
    kaggle_local_runner.set_worker_activity(uid, payload.activity)
    kaggle_local_runner.touch_worker(uid, session_id)
    async with _sessions_lock:
        _extend_session_unlocked(sess)
    return {"ok": True}


@router.post("/kaggle-poll")
async def kaggle_poll(payload: WorkerPollPayload, session_id: str = Query(...),
                       worker_token: str | None = Header(default=None, alias="X-Kaggle-Worker-Token")):
    sess = await _get_session(session_id)
    if not sess:
        raise HTTPException(410, "session not found or expired")
    _require_worker_token(sess, worker_token)
    uid = sess["owner_uid"]
    kaggle_local_runner.touch_worker(uid, session_id)
    async with _sessions_lock:
        _extend_session_unlocked(sess)
        control = sess.get("control")
    if payload.closing:
        # Worker berhenti (stop dari user / idle 3-6 jam / error): sesi selesai, status offline.
        kaggle_local_runner.set_worker_status(uid, status="offline")
        async with _sessions_lock:
            _sessions.pop(session_id, None)
            _persist_sessions_unlocked()
        return {"ok": True, "job": None, "control": None}
    if payload.heartbeat_only:
        # Heartbeat berjalan di thread terpisah (juga saat boot/muat model), jadi hanya "stop"
        # dan "cancel" yang dikirim lewat sini. Cancel sekali pakai: langsung dihapus setelah disampaikan.
        if control == "cancel":
            async with _sessions_lock:
                sess["control"] = None
                _persist_sessions_unlocked()
        return {"ok": True, "job": None,
                "control": "stop" if control == "stop" else ("cancel" if control == "cancel" else None)}
    async with _sessions_lock:
        job = sess.get("pending_job")
        if control in ("restart", "cancel"):
            sess["control"] = None  # sekali pakai
            _persist_sessions_unlocked()
    return {"ok": True, "job": job, "control": control}


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
