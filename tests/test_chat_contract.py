import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from main import app
from modules import chat


client = TestClient(app)


def test_public_route_set_is_chat_only():
    paths = {route.path for route in app.routes if getattr(route, "path", None)}
    paths.update(route.path for route in chat.router.routes)
    assert paths == {
        "/", "/health", "/config",
        "/chat/credentials", "/chat/models", "/chat/completions",
    }


def test_credentials_requires_firebase_bearer():
    response = client.get("/chat/credentials")
    assert response.status_code == 401


def test_credentials_get_returns_account_metadata():
    saved = ("secret-key", {"model": "cloud/model", "fingerprint": "abc123"})
    with patch.object(chat, "_claims", return_value={"uid": "user-1"}), patch.object(chat, "_saved_key", return_value=saved):
        response = client.get("/chat/credentials", headers={"Authorization": "Bearer firebase-token"})
    assert response.status_code == 200
    assert response.json() == {"configured": True, "model": "cloud/model", "fingerprint": "abc123"}


def test_put_credentials_encrypts_and_returns_fingerprint():
    key_doc = SimpleNamespace(set=AsyncMock())
    key_doc.set = lambda data, merge=False: setattr(key_doc, "written", data)
    with patch.object(chat, "_claims", return_value={"uid": "user-1"}), patch.object(chat, "_key_doc", return_value=key_doc), patch.object(chat, "_fernet") as fernet:
        fernet.return_value.encrypt.return_value = b"encrypted"
        response = client.put("/chat/credentials", headers={"Authorization": "Bearer token"}, json={"api_key": "secret-key", "model": "cloud/model"})
    assert response.status_code == 200
    assert response.json() == {"success": True, "model": "cloud/model", "fingerprint": hashlib.sha256(b"secret-key").hexdigest()[:12]}
    assert key_doc.written["encryptedKey"] == "encrypted"


def test_completion_forwards_only_safe_text_payload():
    upstream_response = SimpleNamespace(status_code=200, content=b'{"id":"chatcmpl-test"}', headers={"content-type": "application/json"})

    class FakeClient:
        requests = []
        def __init__(self, *args, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False
        async def post(self, url, **kwargs):
            self.requests.append((url, kwargs))
            return upstream_response

    with patch.object(chat, "_claims", return_value={"uid": "user-1"}), patch.object(chat, "_saved_key", return_value=("secret-key", {})), patch.object(chat.httpx, "AsyncClient", FakeClient):
        response = client.post("/chat/completions", headers={"Authorization": "Bearer token"}, json={"model": "cloud/model", "messages": [{"role": "user", "content": "hello"}], "stream": False})
    assert response.status_code == 200
    assert response.json() == {"id": "chatcmpl-test"}
    assert FakeClient.requests[0][0].endswith("/chat/completions")
    assert FakeClient.requests[0][1]["headers"]["Authorization"] == "Bearer secret-key"
    assert FakeClient.requests[0][1]["json"]["stream"] is False


def test_completion_rejects_stream_tools_and_multimodal_messages():
    headers = {"Authorization": "Bearer token"}
    with patch.object(chat, "_claims", return_value={"uid": "user-1"}), patch.object(chat, "_saved_key", return_value=("secret-key", {})):
        stream = client.post("/chat/completions", headers=headers, json={"model": "m", "messages": [{"role": "user", "content": "hi"}], "stream": True})
        tools = client.post("/chat/completions", headers=headers, json={"model": "m", "messages": [{"role": "user", "content": "hi"}], "tools": []})
        image = client.post("/chat/completions", headers=headers, json={"model": "m", "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]})
    assert stream.status_code == tools.status_code == image.status_code == 400
