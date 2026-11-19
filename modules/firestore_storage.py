"""Minimal Firestore initialization for authenticated chat credential storage."""
from __future__ import annotations

import json
import os
import threading

import firebase_admin
from firebase_admin import credentials, firestore


_db = None
_init_lock = threading.Lock()


def _load_credentials() -> credentials.Certificate:
    raw_credentials = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON")
    if not raw_credentials:
        raise RuntimeError("FIREBASE_SERVICE_ACCOUNT_JSON env var not set")
    try:
        credential_data = json.loads(raw_credentials)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"FIREBASE_SERVICE_ACCOUNT_JSON is not valid JSON: {exc}") from exc
    return credentials.Certificate(credential_data)


def init_firestore():
    """Return the shared Firestore client, initializing Firebase once if needed.

    This module intentionally performs no reads, writes, or deletes by itself. Existing
    collections and documents—including legacy chat credentials—are left untouched.
    """
    global _db
    if _db is not None:
        return _db

    with _init_lock:
        if _db is not None:
            return _db
        if not firebase_admin._apps:
            firebase_admin.initialize_app(_load_credentials())
        _db = firestore.client()
        return _db
