"""Per-user Kaggle credentials for Downloader workers."""
from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Header, HTTPException
from firebase_admin import auth
from pydantic import BaseModel, Field

from .firestore_storage import init_firestore

router = APIRouter(prefix="/kaggle/settings", tags=["kaggle-settings"])


class KaggleCredentialsPayload(BaseModel):
    username: str = Field(min_length=1, max_length=50)
    api_token: str = Field(min_length=1, max_length=500)


def authenticate_user(authorization: str | None) -> str:
    """Verify a Firebase ID token and return its UID."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authentication required")
    try:
        init_firestore()
        claims = auth.verify_id_token(authorization[7:].strip())
        uid = str(claims.get("uid") or "").strip()
        if not uid:
            raise HTTPException(status_code=401, detail="Invalid authentication")
        return uid
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Invalid authentication") from exc


def get_user_credentials(uid: str) -> dict[str, str] | None:
    """Read only this user's saved Kaggle credentials from their Firestore document."""
    snapshot = init_firestore().collection("users").document(uid).get()
    data = snapshot.to_dict() if snapshot.exists else {}
    saved = (data or {}).get("kaggleCredentials")
    if not isinstance(saved, dict):
        return None
    username = saved.get("username")
    api_token = saved.get("apiToken")
    if not isinstance(username, str) or not username.strip() or not isinstance(api_token, str) or not api_token.strip():
        return None
    return {"username": username.strip(), "api_token": api_token.strip()}


@router.get("/credentials")
def read_credentials(authorization: str | None = Header(default=None)):
    uid = authenticate_user(authorization)
    credentials = get_user_credentials(uid)
    if not credentials:
        return {"configured": False, "credentials": None}
    # Plaintext readback is intentional: the signed-in owner can view/manage their own token.
    return {
        "configured": True,
        "credentials": {"username": credentials["username"], "apiToken": credentials["api_token"]},
    }


@router.put("/credentials")
def save_credentials(payload: KaggleCredentialsPayload, authorization: str | None = Header(default=None)):
    uid = authenticate_user(authorization)
    username = payload.username.strip()
    api_token = payload.api_token.strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,50}", username):
        raise HTTPException(status_code=400, detail="Kaggle username may contain only letters, numbers, underscores, and hyphens")
    if not api_token:
        raise HTTPException(status_code=400, detail="Kaggle API token is required")

    user_ref = init_firestore().collection("users").document(uid)
    user_ref.set({"kaggleCredentials": {"username": username, "apiToken": api_token}}, merge=True)
    snapshot = user_ref.get()
    data: dict[str, Any] = snapshot.to_dict() if snapshot.exists else {}
    saved = (data or {}).get("kaggleCredentials")
    if not isinstance(saved, dict) or saved.get("username") != username or saved.get("apiToken") != api_token:
        raise HTTPException(status_code=500, detail="Kaggle credentials could not be verified after saving")
    return {"success": True, "configured": True, "credentials": {"username": username, "apiToken": api_token}}


@router.delete("/credentials")
def delete_credentials(authorization: str | None = Header(default=None)):
    uid = authenticate_user(authorization)
    from firebase_admin import firestore

    user_ref = init_firestore().collection("users").document(uid)
    if user_ref.get().exists:
        user_ref.update({"kaggleCredentials": firestore.DELETE_FIELD})
    return {"success": True, "configured": False}
