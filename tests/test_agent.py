import asyncio
import json
import os
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet
from fastapi import HTTPException

from modules import agent


class FakeSnapshot:
    def __init__(self, data=None):
        self._data = data
        self.exists = data is not None

    def to_dict(self):
        return self._data


class FakeDocument:
    def __init__(self, data=None):
        self.data = data
        self.deleted = False

    def get(self):
        return FakeSnapshot(self.data if not self.deleted else None)

    def set(self, data, merge=False):
        self.data = {**(self.data or {}), **data} if merge else data
        self.deleted = False

    def delete(self):
        self.deleted = True
        self.data = None


class FakeUsersCollection:
    def __init__(self, document):
        self._document = document

    def document(self, _uid):
        return self._document


class FakeDatabase:
    def __init__(self, legacy_document):
        self._legacy_document = legacy_document

    def collection(self, name):
        if name != "users":
            raise AssertionError("unexpected collection")
        return FakeUsersCollection(self._legacy_document)


class FakeResponse:
    def __init__(self, payload, status_code=200, headers=None, text=""):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text

    def json(self):
        return self._payload


class StubAsyncClient:
    calls = []
    get_handler = None
    post_handler = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def get(self, url, headers=None):
        self.calls.append(("GET", url, headers or {}, None))
        if type(self).get_handler:
            return type(self).get_handler(url, headers or {})
        return FakeResponse({"data": []})

    async def post(self, url, headers=None, json=None):
        self.calls.append(("POST", url, headers or {}, json))
        if type(self).post_handler:
            return type(self).post_handler(url, headers or {}, json or {})
        return FakeResponse({"success": True, "result": {"content": "ok"}})


class AgentKeyIsolationTests(unittest.TestCase):
    def setUp(self):
        self.fernet_key = Fernet.generate_key().decode()
        self.fernet = Fernet(self.fernet_key.encode())
        self.keys_by_uid = {
            "uid-a": "private-key-for-a",
            "uid-b": "private-key-for-b",
        }
        self.docs_by_uid = {
            uid: FakeDocument({
                "encryptedKey": self.fernet.encrypt(key.encode()).decode(),
                "model": "openrouter/openrouter/free",
                "fingerprint": agent._fingerprint(key),
                "enabled": True,
            })
            for uid, key in self.keys_by_uid.items()
        }
        StubAsyncClient.calls = []
        StubAsyncClient.get_handler = None
        StubAsyncClient.post_handler = None
        self.env_patch = patch.dict(os.environ, {"AGENT_ENCRYPTION_KEY": self.fernet_key, "AGENT_INTERNAL_API_KEY": "internal-test-key", "AGENT_RUNTIME_URL": "https://runtime.example.test"})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def _claims_for_token(self, authorization):
        uid = (authorization or "").removeprefix("Bearer ")
        return {"uid": uid, "email": f"{uid}@example.test"}

    def test_saved_keys_are_isolated_by_uid_and_never_returned_in_catalog(self):
        def respond(_url, headers):
            api_key = headers["Authorization"].removeprefix("Bearer ")
            owner = "uid-a" if api_key == self.keys_by_uid["uid-a"] else "uid-b"
            return FakeResponse({"data": [{"id": f"{owner}/model"}]})

        StubAsyncClient.get_handler = respond
        with patch.object(agent, "_claims", side_effect=self._claims_for_token), \
                patch.object(agent, "_key_doc", side_effect=lambda uid, _provider: self.docs_by_uid[uid]), \
                patch.object(agent.httpx, "AsyncClient", StubAsyncClient):
            result_a = asyncio.run(agent.agent_models("Bearer uid-a"))
            result_b = asyncio.run(agent.agent_models("Bearer uid-b"))

        self.assertEqual(result_a, {"configured": True, "models": ["uid-a/model"]})
        self.assertEqual(result_b, {"configured": True, "models": ["uid-b/model"]})
        self.assertNotIn(self.keys_by_uid["uid-a"], json.dumps(result_a))
        self.assertNotIn(self.keys_by_uid["uid-b"], json.dumps(result_b))
        sent_keys = [call[2]["Authorization"] for call in StubAsyncClient.calls]
        self.assertEqual(sent_keys, [f"Bearer {self.keys_by_uid['uid-a']}", f"Bearer {self.keys_by_uid['uid-b']}"])

    def test_unconfigured_account_gets_an_honest_empty_catalog(self):
        with patch.object(agent, "_claims", return_value={"uid": "uid-new"}), \
                patch.object(agent, "_saved_key", return_value=None), \
                patch.object(agent, "_fetch_router_models") as fetch_models:
            result = asyncio.run(agent.agent_models("Bearer uid-new"))
        self.assertEqual(result, {"configured": False, "models": []})
        fetch_models.assert_not_called()

    def test_configured_account_can_have_a_valid_but_empty_catalog(self):
        with patch.object(agent, "_claims", return_value={"uid": "uid-a"}), \
                patch.object(agent, "_saved_key", return_value=("private-key-for-a", {})), \
                patch.object(agent, "_fetch_router_models", return_value=[]):
            result = asyncio.run(agent.agent_models("Bearer uid-a"))
        self.assertEqual(result, {"configured": True, "models": []})

    def test_catalog_failure_is_reported_without_exposing_upstream_body(self):
        with patch.object(agent, "_claims", return_value={"uid": "uid-a"}), \
                patch.object(agent, "_saved_key", return_value=("private-key-for-a", {})), \
                patch.object(agent, "_fetch_router_models", side_effect=HTTPException(status_code=502, detail="9Router model catalog is unavailable")):
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(agent.agent_models("Bearer uid-a"))
        self.assertEqual(raised.exception.status_code, 502)
        self.assertNotIn("private-key-for-a", raised.exception.detail)

    def test_save_encrypts_key_and_returns_only_safe_metadata_for_current_uid(self):
        saved = {}

        def key_document(uid, provider):
            return saved.setdefault((uid, provider), FakeDocument())

        payload = agent.AgentKeyPayload(provider="9Router", api_key="a-new-secret", model="selected-model")
        with patch.object(agent, "_claims", return_value={"uid": "uid-a"}), \
                patch.object(agent, "_get_key_snapshot", return_value=FakeSnapshot()), \
                patch.object(agent, "_key_doc", side_effect=key_document):
            result = agent.save_agent_key(payload, "Bearer uid-a")

        stored = saved[("uid-a", "9router")].data
        self.assertNotEqual(stored["encryptedKey"], "a-new-secret")
        self.assertEqual(self.fernet.decrypt(stored["encryptedKey"].encode()).decode(), "a-new-secret")
        self.assertNotIn("a-new-secret", json.dumps(result))
        self.assertNotIn("encryptedKey", result)
        self.assertEqual(result["provider"], "9router")

    def test_legacy_plaintext_key_is_encrypted_and_cleared_server_side(self):
        old_key = "legacy-secret-key"
        legacy_document = FakeDocument({"nineRouterApiKey": old_key, "nineRouterModel": "legacy-model"})
        key_document = FakeDocument()
        database = FakeDatabase(legacy_document)
        with patch.object(agent, "_key_doc", return_value=key_document), \
                patch.object(agent, "init_firestore", return_value=database):
            snapshot = agent._get_key_snapshot("uid-a", "9router")
        self.assertTrue(snapshot.exists)
        self.assertEqual(self.fernet.decrypt(key_document.data["encryptedKey"].encode()).decode(), old_key)
        self.assertIsNone(legacy_document.data["nineRouterApiKey"])
        self.assertNotEqual(key_document.data["encryptedKey"], old_key)

    def test_delete_removes_only_the_authenticated_users_key(self):
        with patch.object(agent, "_claims", return_value={"uid": "uid-a"}), \
                patch.object(agent, "_key_doc", side_effect=lambda uid, _provider: self.docs_by_uid[uid]):
            result = agent.delete_agent_key("9Router", "Bearer uid-a")
        self.assertTrue(result["success"])
        self.assertTrue(self.docs_by_uid["uid-a"].deleted)
        self.assertFalse(self.docs_by_uid["uid-b"].deleted)

    def test_router_key_rejection_is_sanitized(self):
        StubAsyncClient.get_handler = lambda _url, _headers: FakeResponse({"detail": "private-key-for-a rejected"}, 401)
        with patch.object(agent.httpx, "AsyncClient", StubAsyncClient):
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(agent._fetch_router_models("private-key-for-a"))
        self.assertEqual(raised.exception.status_code, 401)
        self.assertNotIn("private-key-for-a", raised.exception.detail)

    def test_router_cooldown_has_a_specific_user_facing_message(self):
        StubAsyncClient.get_handler = lambda _url, _headers: FakeResponse({"error": {"code": "machine_cooldown"}}, 409)
        with patch.object(agent.httpx, "AsyncClient", StubAsyncClient):
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(agent._fetch_router_models("private-key-for-a"))
        self.assertEqual(raised.exception.status_code, 503)
        self.assertIn("sedang cooldown", raised.exception.detail)
        self.assertNotIn("private-key-for-a", raised.exception.detail)

    def test_chat_rejects_a_model_not_in_the_signed_in_users_catalog(self):
        payload = agent.AgentChatPayload(prompt="hello", provider="9router", model="uid-b/private-model")
        with patch.object(agent, "_claims", return_value={"uid": "uid-a", "email": "uid-a@example.test"}), \
                patch.object(agent, "_saved_key", return_value=("private-key-for-a", {})), \
                patch.object(agent, "_fetch_router_models", return_value=["uid-a/allowed-model"]), \
                patch.object(agent.httpx, "AsyncClient", StubAsyncClient):
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(agent.agent_chat(payload, "Bearer uid-a"))
        self.assertEqual(raised.exception.status_code, 403)
        self.assertIn("not available for this account", raised.exception.detail)
        self.assertEqual(StubAsyncClient.calls, [])

    def test_chat_requires_runtime_url_before_fetching_models(self):
        payload = agent.AgentChatPayload(prompt="hello", provider="9router", model="uid-a/allowed-model")
        with patch.dict(os.environ, {"AGENT_RUNTIME_URL": ""}), \
                patch.object(agent, "_claims", return_value={"uid": "uid-a", "email": "uid-a@example.test"}), \
                patch.object(agent, "_saved_key", return_value=("private-key-for-a", {})), \
                patch.object(agent, "_fetch_router_models") as fetch_models:
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(agent.agent_chat(payload, "Bearer uid-a"))
        self.assertEqual(raised.exception.status_code, 500)
        self.assertEqual(raised.exception.detail, "Agent runtime URL is not configured")
        fetch_models.assert_not_called()
        self.assertEqual(StubAsyncClient.calls, [])

    def test_chat_requires_internal_runtime_key(self):
        payload = agent.AgentChatPayload(prompt="hello", provider="9router", model="uid-a/allowed-model")
        with patch.dict(os.environ, {"AGENT_INTERNAL_API_KEY": ""}), \
                patch.object(agent, "_claims", return_value={"uid": "uid-a", "email": "uid-a@example.test"}), \
                patch.object(agent, "_saved_key", return_value=("private-key-for-a", {})):
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(agent.agent_chat(payload, "Bearer uid-a"))
        self.assertEqual(raised.exception.status_code, 500)
        self.assertEqual(raised.exception.detail, "Agent runtime internal API key is not configured")
        self.assertEqual(StubAsyncClient.calls, [])

    def test_chat_passes_only_the_authenticated_users_key_after_model_validation(self):
        payload = agent.AgentChatPayload(prompt="hello", provider="9router", model="uid-a/allowed-model")
        StubAsyncClient.post_handler = lambda _url, _headers, _body: FakeResponse({"success": True, "result": {"content": "ok"}})
        with patch.object(agent, "_claims", return_value={"uid": "uid-a", "email": "uid-a@example.test"}), \
                patch.object(agent, "_saved_key", return_value=("private-key-for-a", {})), \
                patch.object(agent, "_fetch_router_models", return_value=["uid-a/allowed-model"]), \
                patch.object(agent.httpx, "AsyncClient", StubAsyncClient):
            result = asyncio.run(agent.agent_chat(payload, "Bearer uid-a"))
        self.assertTrue(result["success"])
        runtime_call = next(call for call in StubAsyncClient.calls if call[0] == "POST")
        self.assertEqual(runtime_call[1], "https://runtime.example.test/v1/agent/run")
        self.assertEqual(runtime_call[2]["X-Agent-Internal-Key"], "internal-test-key")
        self.assertEqual(runtime_call[3]["model"], "uid-a/allowed-model")
        self.assertEqual(runtime_call[3]["provider_api_key"], "private-key-for-a")
        self.assertNotIn("private-key-for-a", json.dumps(result))

    def test_mimo_catalog_id_and_authenticated_users_key_reach_runtime_unchanged(self):
        mimo_model_id = "openai-compatible-chat-test/mimo-v2.6:latest"
        payload = agent.AgentChatPayload(prompt="mock-only", provider="9router", model=mimo_model_id)
        StubAsyncClient.post_handler = lambda _url, _headers, _body: FakeResponse({"success": True, "result": {"content": "ok"}})
        with patch.object(agent, "_claims", return_value={"uid": "uid-a", "email": "uid-a@example.test"}), \
                patch.object(agent, "_saved_key", return_value=("private-key-for-a", {})), \
                patch.object(agent, "_fetch_router_models", return_value=[mimo_model_id]), \
                patch.object(agent.httpx, "AsyncClient", StubAsyncClient):
            asyncio.run(agent.agent_chat(payload, "Bearer uid-a"))
        runtime_call = next(call for call in StubAsyncClient.calls if call[0] == "POST")
        self.assertEqual(runtime_call[1], "https://runtime.example.test/v1/agent/run")
        self.assertEqual(runtime_call[3]["model"], mimo_model_id)
        self.assertEqual(runtime_call[3]["provider_api_key"], "private-key-for-a")

    def test_runtime_http_error_logs_only_safe_response_metadata(self):
        sensitive_prompt = "private-prompt-marker"
        sensitive_key = "private-provider-key-marker"
        sensitive_email = "private-user@example.test"
        sensitive_body = "private-upstream-body-marker"
        payload = agent.AgentChatPayload(prompt=sensitive_prompt, provider="9router", model="uid-a/allowed-model")
        StubAsyncClient.post_handler = lambda _url, _headers, _body: FakeResponse(
            {"error": sensitive_body},
            502,
            headers={
                "content-type": "application/json",
                "x-request-id": "request-123",
                "cf-ray": "ray-456",
                "via": "edge-proxy",
                "server": "runtime-proxy",
                "authorization": "private-authorization-marker",
                "x-api-key": "private-header-key-marker",
                "x-user-email": sensitive_email,
            },
            text=sensitive_body,
        )
        with patch.object(agent, "_claims", return_value={"uid": "uid-a", "email": sensitive_email}), \
                patch.object(agent, "_saved_key", return_value=(sensitive_key, {})), \
                patch.object(agent, "_fetch_router_models", return_value=["uid-a/allowed-model"]), \
                patch.object(agent.httpx, "AsyncClient", StubAsyncClient):
            with self.assertLogs(agent.logger, level="WARNING") as captured:
                with self.assertRaises(HTTPException) as raised:
                    asyncio.run(agent.agent_chat(payload, "Bearer uid-a"))

        self.assertEqual(raised.exception.status_code, 502)
        self.assertEqual(raised.exception.detail, "Agent runtime request failed")
        log_text = "\n".join(captured.output)
        self.assertIn("status=502", log_text)
        self.assertRegex(log_text, r"elapsed_ms=\d+")
        self.assertIn("application/json", log_text)
        for safe_identifier in ("request-123", "ray-456", "edge-proxy", "runtime-proxy"):
            self.assertIn(safe_identifier, log_text)
        for sensitive_value in (
            sensitive_prompt,
            sensitive_key,
            sensitive_email,
            sensitive_body,
            "private-authorization-marker",
            "private-header-key-marker",
            "internal-test-key",
        ):
            self.assertNotIn(sensitive_value, log_text)
        self.assertNotIn(sensitive_body, raised.exception.detail)

    def test_chat_routes_local_and_cloud_catalog_models_with_isolated_account_keys(self):
        selected_models = {
            "uid-a": "openai-compatible-chat-test/mimo-v2.6:latest",
            "uid-b": "google/gemini-test",
        }
        account_keys = {
            "uid-a": "private-key-for-a",
            "uid-b": "private-key-for-b",
        }

        def claims_for_token(authorization):
            uid = (authorization or "").removeprefix("Bearer ")
            return {"uid": uid, "email": f"{uid}@example.test"}

        def models_for_key(api_key):
            uid = "uid-a" if api_key == account_keys["uid-a"] else "uid-b"
            return [selected_models[uid]]

        def key_for_uid(uid, provider):
            self.assertEqual(provider, "9router")
            return account_keys[uid], {}

        with patch.object(agent, "_claims", side_effect=claims_for_token), \
                patch.object(agent, "_saved_key", side_effect=key_for_uid), \
                patch.object(agent, "_fetch_router_models", side_effect=models_for_key), \
                patch.object(agent.httpx, "AsyncClient", StubAsyncClient):
            for uid, model in selected_models.items():
                payload = agent.AgentChatPayload(prompt="mock-only", provider="9router", model=model)
                asyncio.run(agent.agent_chat(payload, f"Bearer {uid}"))

        runtime_calls = [call for call in StubAsyncClient.calls if call[0] == "POST"]
        self.assertEqual([call[3]["model"] for call in runtime_calls], list(selected_models.values()))
        self.assertEqual([call[3]["provider_api_key"] for call in runtime_calls], list(account_keys.values()))


if __name__ == "__main__":
    unittest.main()
