import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from modules import agent


class _RouterResponse:
    status_code = 200
    headers = {}

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class _RouterClient:
    requests: list[dict] = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def request(self, method, url, *, headers, json=None):
        self.requests.append({"method": method, "url": url, "headers": headers, "json": json})
        key = headers["Authorization"].removeprefix("Bearer ")
        if method == "GET":
            model_id = {"key-a": "model-a", "key-b": "model-b"}.get(key, "model-default")
            return _RouterResponse({"data": [{"id": model_id}]})
        return _RouterResponse({
            "model": json["model"],
            "choices": [{"message": {"content": "Mock answer"}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        })


class AgentModelAccessTests(unittest.IsolatedAsyncioTestCase):
    claims = {"uid": "user-a", "email": "user@example.com", "email_verified": True}

    def setUp(self):
        _RouterClient.requests = []

    def _payload(self, model: str, provider: str = "9router"):
        return SimpleNamespace(
            prompt="hello",
            provider=provider,
            model=model,
            web_search=False,
            search_query="",
            timezone="Asia/Jakarta",
            history=[],
            images=[],
        )

    def _patch_router(self):
        stack = patch.object(agent.httpx, "AsyncClient", _RouterClient)
        stack.start()
        self.addCleanup(stack.stop)

    def _router_env(self):
        return patch.dict(os.environ, {"AGENT_9ROUTER_BASE_URL": "https://router.example/v1"})

    async def test_models_without_key_are_empty_and_never_call_router(self):
        self._patch_router()
        with patch.object(agent, "_claims", return_value=self.claims), patch.object(agent, "_saved_router_key", return_value=None):
            response = await agent.agent_models("Bearer firebase-token")

        self.assertFalse(response["configured"])
        self.assertEqual(response["models"], [])
        self.assertEqual(_RouterClient.requests, [])

    async def test_catalog_is_fetched_with_each_accounts_own_9router_key(self):
        self._patch_router()
        with self._router_env(), patch.object(agent, "_claims", return_value={"uid": "user-a"}), patch.object(agent, "_saved_router_key", return_value=("key-a", {})):
            response = await agent.agent_models("Bearer firebase-token")
        self.assertTrue(response["configured"])
        self.assertEqual(response["provider"], "9router")
        self.assertEqual(response["models"], [{"id": "model-a"}])
        self.assertEqual(_RouterClient.requests[-1]["headers"]["Authorization"], "Bearer key-a")

        with self._router_env(), patch.object(agent, "_claims", return_value={"uid": "user-b"}), patch.object(agent, "_saved_router_key", return_value=("key-b", {})):
            other_response = await agent.agent_models("Bearer firebase-token")
        self.assertEqual(other_response["models"], [{"id": "model-b"}])
        self.assertEqual(_RouterClient.requests[-1]["headers"]["Authorization"], "Bearer key-b")

    async def test_catalog_fails_closed_without_an_approved_endpoint(self):
        self._patch_router()
        with patch.dict(os.environ, {"AGENT_9ROUTER_BASE_URL": ""}), patch.object(agent, "_claims", return_value=self.claims), patch.object(agent, "_saved_router_key", return_value=("key-a", {})):
            with self.assertRaises(HTTPException) as raised:
                await agent.agent_models("Bearer firebase-token")

        self.assertEqual(raised.exception.status_code, 503)
        self.assertIn("No approved 9Router endpoint", raised.exception.detail)
        self.assertEqual(_RouterClient.requests, [])

    async def test_chat_without_key_is_rejected(self):
        self._patch_router()
        with patch.object(agent, "_claims", return_value=self.claims), patch.object(agent, "_saved_router_key", return_value=None):
            with self.assertRaises(HTTPException) as raised:
                await agent.agent_chat(self._payload("model-a"), "Bearer firebase-token")

        self.assertEqual(raised.exception.status_code, 403)
        self.assertEqual(_RouterClient.requests, [])

    async def test_chat_uses_account_key_and_only_chat_fields(self):
        self._patch_router()
        with self._router_env(), patch.object(agent, "_claims", return_value=self.claims), patch.object(agent, "_saved_router_key", return_value=("key-a", {})):
            response = await agent.agent_chat(self._payload("model-a"), "Bearer firebase-token")

        self.assertTrue(response["success"])
        self.assertEqual(response["userId"], "user-a")
        self.assertEqual(response["provider"], "9router")
        self.assertEqual(response["data"]["result"]["content"], "Mock answer")
        self.assertEqual(len(_RouterClient.requests), 2)
        chat_request = _RouterClient.requests[-1]
        self.assertEqual(chat_request["method"], "POST")
        self.assertEqual(chat_request["url"], "https://router.example/v1/chat/completions")
        self.assertEqual(chat_request["headers"]["Authorization"], "Bearer key-a")
        self.assertNotIn("key-a", str(chat_request["json"]))
        self.assertNotIn("tools", chat_request["json"])
        self.assertNotIn("web_search", chat_request["json"])

    async def test_chat_cannot_use_model_outside_this_keys_catalog(self):
        self._patch_router()
        with self._router_env(), patch.object(agent, "_claims", return_value={"uid": "user-b"}), patch.object(agent, "_saved_router_key", return_value=("key-b", {})):
            with self.assertRaises(HTTPException) as raised:
                await agent.agent_chat(self._payload("model-a"), "Bearer firebase-token")

        self.assertEqual(raised.exception.status_code, 403)
        self.assertEqual(len(_RouterClient.requests), 1)
        self.assertEqual(_RouterClient.requests[0]["method"], "GET")

    async def test_direct_provider_is_not_supported(self):
        with self.assertRaises(HTTPException) as raised:
            agent._normalize_provider("openai")
        self.assertEqual(raised.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
