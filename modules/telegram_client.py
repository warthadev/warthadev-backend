# modules/telegram_client.py
import os
import time
import asyncio
from collections import OrderedDict
from typing import Dict
from telethon import TelegramClient
from telethon.sessions import StringSession

MAX_CLIENTS = 8
IDLE_DISCONNECT_SECONDS = 30 * 60

_clients: "OrderedDict[str, TelegramClient]" = OrderedDict()
_client_last_used: Dict[str, float] = {}
_client_locks: Dict[str, asyncio.Lock] = {}
_registry_lock: asyncio.Lock | None = None


def _get_registry_lock() -> asyncio.Lock:
    global _registry_lock
    if _registry_lock is None:
        _registry_lock = asyncio.Lock()
    return _registry_lock


def _get_api_creds():
    api_id_raw = os.environ.get("TELEGRAM_API_ID")
    api_hash = os.environ.get("TELEGRAM_API_HASH")
    if not api_id_raw or not api_hash:
        raise RuntimeError("TELEGRAM_API_ID and TELEGRAM_API_HASH must be set")
    try:
        api_id = int(api_id_raw)
    except ValueError:
        raise RuntimeError("TELEGRAM_API_ID must be an integer")
    return api_id, api_hash


async def _get_session_lock(session_string: str) -> asyncio.Lock:
    async with _get_registry_lock():
        lock = _client_locks.get(session_string)
        if lock is None:
            lock = asyncio.Lock()
            _client_locks[session_string] = lock
        return lock


def _touch(session_string: str) -> None:
    _client_last_used[session_string] = time.time()
    if session_string in _clients:
        _clients.move_to_end(session_string)


async def _evict_if_needed() -> None:
    while len(_clients) > MAX_CLIENTS:
        oldest_session, oldest_client = _clients.popitem(last=False)
        _client_last_used.pop(oldest_session, None)
        try:
            if oldest_client.is_connected():
                await oldest_client.disconnect()
        except Exception:
            pass


async def _evict_idle() -> None:
    now = time.time()
    stale_sessions = [
        s for s, t in _client_last_used.items()
        if now - t > IDLE_DISCONNECT_SECONDS and s in _clients
    ]
    for session_string in stale_sessions:
        client = _clients.pop(session_string, None)
        _client_last_used.pop(session_string, None)
        if client is None:
            continue
        try:
            if client.is_connected():
                await client.disconnect()
        except Exception:
            pass


async def get_telegram_client(session_string: str) -> TelegramClient:
    if not session_string:
        raise ValueError("Session string is required")

    lock = await _get_session_lock(session_string)
    async with lock:
        if session_string in _clients:
            client = _clients[session_string]
            if client.is_connected():
                _touch(session_string)
                return client
            _clients.pop(session_string, None)
            _client_last_used.pop(session_string, None)

        await _evict_idle()

        api_id, api_hash = _get_api_creds()
        client = TelegramClient(StringSession(session_string), api_id, api_hash)
        await client.start()
        _clients[session_string] = client
        _client_last_used[session_string] = time.time()
        await _evict_if_needed()
        return client


async def disconnect_client(session_string: str):
    if not session_string:
        return

    lock = await _get_session_lock(session_string)
    async with lock:
        client = _clients.pop(session_string, None)
        _client_last_used.pop(session_string, None)
        if client is not None:
            try:
                if client.is_connected():
                    await client.disconnect()
            except Exception:
                pass

        async with _get_registry_lock():
            _client_locks.pop(session_string, None)


async def disconnect_all():
    async with _get_registry_lock():
        sessions = list(_clients.keys())

    for session_string in sessions:
        await disconnect_client(session_string)