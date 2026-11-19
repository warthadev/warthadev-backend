# modules/firestore_storage.py
import os
import json
import threading
import firebase_admin
from firebase_admin import credentials, firestore
from typing import List, Dict, Any

_db = None
_init_lock = threading.Lock()

BATCH_SIZE = 500


def _load_credentials() -> credentials.Certificate:
    cred_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON")
    if not cred_json:
        raise RuntimeError("FIREBASE_SERVICE_ACCOUNT_JSON env var not set")
    try:
        cred_dict = json.loads(cred_json)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"FIREBASE_SERVICE_ACCOUNT_JSON is not valid JSON: {e}")
    return credentials.Certificate(cred_dict)


def init_firestore():
    global _db
    if _db is not None:
        return _db

    with _init_lock:
        if _db is not None:
            return _db

        cred = _load_credentials()
        if not firebase_admin._apps:
            firebase_admin.initialize_app(cred)
        _db = firestore.client()
        print("Firestore initialized")
        return _db


def _ensure_db():
    if _db is None:
        init_firestore()
    return _db


def _is_sentinel(value) -> bool:
    return type(value).__name__ == 'Sentinel' or 'Sentinel' in str(type(value))


def _clean_value(value):
    if _is_sentinel(value):
        return None
    if hasattr(value, 'isoformat'):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode('utf-8', errors='ignore')
    if isinstance(value, dict):
        return {k: _clean_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clean_value(v) for v in value]
    if isinstance(value, (int, float, str, bool)):
        return value
    if value is None:
        return None
    return str(value)


def save_file(chat_id: int, message_id: int, file_data: Dict[str, Any]):
    db = _ensure_db()
    doc_ref = db.collection("telegram_files").document(f"{chat_id}_{message_id}")
    file_data["chatId"] = chat_id
    file_data["messageId"] = message_id
    doc_ref.set(file_data, merge=True)


def get_files(chat_id: int) -> List[Dict[str, Any]]:
    db = _ensure_db()
    docs = db.collection("telegram_files").where("chatId", "==", chat_id).stream()

    files = []
    for doc in docs:
        data = doc.to_dict()
        safe_data = {k: _clean_value(v) for k, v in data.items()}
        files.append(safe_data)

    files.sort(key=lambda x: x.get("messageId", 0))
    return files


def get_max_message_id(chat_id: int) -> int:
    db = _ensure_db()
    docs = (
        db.collection("telegram_files")
        .where("chatId", "==", chat_id)
        .stream()
    )
    max_id = 0
    for doc in docs:
        data = doc.to_dict()
        msg_id = data.get("messageId", 0)
        if msg_id > max_id:
            max_id = msg_id
    return max_id


def _chunked(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def delete_chat(chat_id: int):
    db = _ensure_db()
    docs = db.collection("telegram_files").where("chatId", "==", chat_id).stream()
    refs = [doc.reference for doc in docs]
    if not refs:
        return
    for chunk in _chunked(refs, BATCH_SIZE):
        batch = db.batch()
        for ref in chunk:
            batch.delete(ref)
        batch.commit()


def delete_file(chat_id: int, message_id: int):
    db = _ensure_db()
    doc_ref = db.collection("telegram_files").document(f"{chat_id}_{message_id}")
    try:
        doc_ref.delete()
        print(f"Deleted file {chat_id}_{message_id} from Firestore")
    except Exception as e:
        print(f"Error deleting file {chat_id}_{message_id}: {e}")


def get_all_chats_with_files() -> List[int]:
    db = _ensure_db()
    docs = db.collection("telegram_files").select(["chatId"]).stream()
    chat_ids = set()
    for doc in docs:
        data = doc.to_dict()
        chat_id = data.get("chatId")
        if chat_id is not None:
            chat_ids.add(chat_id)
    return list(chat_ids)