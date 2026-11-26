# modules/agent_memory.py
"""Per-user AI memory + instructions, stored in Firestore under users/{uid}.

Data model (same users/{uid} document that already holds the Kaggle
credentials and the OpenRouter key records):
- ``aiInstructions``: one free-text field the user writes themself in
  Settings (e.g. "always answer in Indonesian", "I am a programmer").
- ``aiMemories`` subcollection: one document per saved fact, each
  ``{text, source, createdAt}`` where source is "ai" (saved by the agent
  through the ``remember`` tool) or "user" (typed in Settings).

Caps keep the injected context small: at most 50 entries, 500 chars each,
4000 chars total injected into one chat; instructions at most 2000 chars.
Reads never fail a chat — any Firestore error returns empty memory.

The frontend never talks to Firestore directly for this: it calls the
/api endpoints in modules/agent.py, which verify the Firebase ID token
first (``_claims``) and only ever touch that user's own document.
Deleting a memory deletes its document for real (not just from display).
"""
from __future__ import annotations

from typing import Any

from firebase_admin import firestore

from .firestore_storage import init_firestore

MEMORY_MAX_ENTRIES = 50
MEMORY_MAX_ENTRY_CHARS = 500
MEMORY_INJECT_MAX_CHARS = 4000
INSTRUCTIONS_MAX_CHARS = 2000

_MEMORIES = "aiMemories"


def _user_doc(user_id: str):
    return init_firestore().collection("users").document(user_id)


def _memories_ref(user_id: str):
    return _user_doc(user_id).collection(_MEMORIES)


def _clean_text(text: Any, limit: int) -> str:
    cleaned = str(text or "").strip()
    return cleaned[:limit] if len(cleaned) > limit else cleaned


def _entry_to_dict(doc) -> dict[str, Any]:
    data = doc.to_dict() or {}
    created = data.get("createdAt")
    if hasattr(created, "isoformat"):
        created = created.isoformat()
    elif created is not None:
        created = str(created)
    return {
        "id": doc.id,
        "text": str(data.get("text") or ""),
        "source": str(data.get("source") or "user"),
        "createdAt": created,
    }


def load_memory_context(user_id: str) -> dict[str, Any]:
    """What goes into the per-chat context note: instructions + memories,
    newest first, capped so one chat never carries more than
    MEMORY_INJECT_MAX_CHARS of memory. Returns {} on any error."""
    try:
        snapshot = _user_doc(user_id).get()
        data = snapshot.to_dict() if snapshot.exists else {}
        data = data or {}
        instructions = _clean_text(data.get("aiInstructions"), INSTRUCTIONS_MAX_CHARS)
        memories: list[str] = []
        total = 0
        docs = list(
            _memories_ref(user_id)
            .order_by("createdAt", direction=firestore.Query.DESCENDING)
            .limit(MEMORY_MAX_ENTRIES)
            .stream()
        )
        for doc in docs:
            text = _clean_text((doc.to_dict() or {}).get("text"), MEMORY_MAX_ENTRY_CHARS)
            if not text or text in memories:
                continue
            if total + len(text) > MEMORY_INJECT_MAX_CHARS:
                break
            memories.append(text)
            total += len(text)
        out: dict[str, Any] = {}
        if instructions:
            out["ai_instructions"] = instructions
        if memories:
            out["ai_memory"] = memories
        return out
    except Exception:
        return {}


def get_all(user_id: str) -> dict[str, Any]:
    """Full memory state for the Settings page: instructions + every entry,
    newest first."""
    snapshot = _user_doc(user_id).get()
    data = snapshot.to_dict() if snapshot.exists else {}
    data = data or {}
    instructions = _clean_text(data.get("aiInstructions"), INSTRUCTIONS_MAX_CHARS)
    memories = [
        _entry_to_dict(doc)
        for doc in _memories_ref(user_id)
        .order_by("createdAt", direction=firestore.Query.DESCENDING)
        .limit(MEMORY_MAX_ENTRIES)
        .stream()
    ]
    return {"instructions": instructions, "memories": memories}


def save_instructions(user_id: str, text: Any) -> str:
    """Store the user's own instructions (empty clears them)."""
    cleaned = _clean_text(text, INSTRUCTIONS_MAX_CHARS)
    _user_doc(user_id).set({"aiInstructions": cleaned}, merge=True)
    return cleaned


def save_memory(user_id: str, text: Any, source: str = "ai") -> dict[str, Any] | None:
    """Save one memory entry. Returns the saved entry dict, or None for empty
    text. When already at MEMORY_MAX_ENTRIES, the oldest entry is deleted
    first so the list never grows past the cap."""
    cleaned = _clean_text(text, MEMORY_MAX_ENTRY_CHARS)
    if not cleaned:
        return None
    ref = _memories_ref(user_id)
    existing = list(
        ref.order_by("createdAt", direction=firestore.Query.ASCENDING)
        .limit(MEMORY_MAX_ENTRIES)
        .stream()
    )
    if len(existing) >= MEMORY_MAX_ENTRIES:
        for old in existing[: len(existing) - MEMORY_MAX_ENTRIES + 1]:
            old.reference.delete()
    doc_ref = ref.document()
    doc_ref.set(
        {"text": cleaned, "source": source, "createdAt": firestore.SERVER_TIMESTAMP}
    )
    return {"id": doc_ref.id, "text": cleaned, "source": source, "createdAt": None}


def delete_memory(user_id: str, memory_id: str) -> bool:
    """Delete one memory document for real. True when it existed."""
    doc_ref = _memories_ref(user_id).document(memory_id)
    if not doc_ref.get().exists:
        return False
    doc_ref.delete()
    return True
