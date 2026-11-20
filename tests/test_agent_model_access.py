import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from modules import agent


class _ProviderResponse:
    status_code = 200
    headers = {}

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class _ProviderClient:
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
            return _ProviderResponse({"data": [{"id": model_id}]})
        return _ProviderResponse({
            "model": json["model"],
            "choices": [{"message": {"content": "Mock answer"}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        })


class AgentModelAccessTests(unittest.IsolatedAsyncioTestCase):
    claims = {"uid": "user-a", "email": "user@example.com", "email_verified": True}

    def setUp(self):
        _ProviderClient.requests = []

    def _payload(self, model: str, provider: str = "openai"):
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

    def _patch_provider(self):
        stack = patch.object(agent.httpx, "AsyncClient", _ProviderClient)
        stack.start()
        self.addCleanup(stack.stop)

    async def test_models_without_key_are_empty_and_never_read_local_inventory(self):
        self._patch_provider()
        with patch.object(agent, "_claims", return_value=self.claims), patch.object(agent, "_saved_gateway", return_value=None):
            response = await agent.agent_models("Bearer firebase-token")

        self.assertFalse(response["configured"])
        self.assertEqual(response["models"], [])
        self.assertEqual(_ProviderClient.requests, [])

    async def test_catalog_is_fetched_with_each_accounts_own_key(self):
        self._patch_provider()
        with patch.object(agent, "_claims", return_value={"uid": "user-a"}), patch.object(agent, "_saved_gateway", return_value=("key-a", "openai", {})):
            response = await agent.agent_models("Bearer firebase-token")
        self.assertTrue(response["configured"])
        self.assertEqual(response["provider"], "openai")
        self.assertEqual(response["models"], [{"id": "model-a"}])
        self.assertEqual(_ProviderClient.requests[-1]["headers"]["Authorization"], "Bearer key-a")

        with patch.object(agent, "_claims", return_value={"uid": "user-b"}), patch.object(agent, "_saved_gateway", return_value=("key-b", "deepseek", {})):
            other_response = await agent.agent_models("Bearer firebase-token")
        self.assertEqual(other_response["provider"], "deepseek")
        self.assertEqual(other_response["models"], [{"id": "model-b"}])
        self.assertEqual(_ProviderClient.requests[-1]["headers"]["Authorization"], "Bearer key-b")

    async def test_chat_without_key_is_rejected(self):
        self._patch_provider()
        with patch.object(agent, "_claims", return_value=self.claims), patch.object(agent, "_saved_gateway", return_value=None):
            with self.assertRaises(HTTPException) as raised:
                await agent.agent_chat(self._payload("model-a"), "Bearer firebase-token")

        self.assertEqual(raised.exception.status_code, 403)
        self.assertEqual(_ProviderClient.requests, [])

    async def test_chat_uses_account_key_only_in_provider_auth_header(self):
        self._patch_provider()
        with patch.object(agent, "_claims", return_value=self.claims), patch.object(agent, "_saved_gateway", return_value=("key-a", "openai", {})):
            response = await agent.agent_chat(self._payload("model-a"), "Bearer firebase-token")

        self.assertTrue(response["success"])
        self.assertEqual(response["userId"], "user-a")
        self.assertEqual(response["data"]["result"]["content"], "Mock answer")
        self.assertEqual(len(_ProviderClient.requests), 2)
        chat_request = _ProviderClient.requests[-1]
        self.assertEqual(chat_request["method"], "POST")
        self.assertEqual(chat_request["url"], "https://api.openai.com/v1/chat/completions")
        self.assertEqual(chat_request["headers"]["Authorization"], "Bearer key-a")
        self.assertNotIn("key-a", str(chat_request["json"]))
        self.assertNotIn("tools", chat_request["json"])

    async def test_chat_cannot_use_model_outside_this_accounts_catalog(self):
        self._patch_provider()
        with patch.object(agent, "_claims", return_value={"uid": "user-b"}), patch.object(agent, "_saved_gateway", return_value=("key-b", "deepseek", {})):
            with self.assertRaises(HTTPException) as raised:
                await agent.agent_chat(self._payload("model-a", provider="deepseek"), "Bearer firebase-token")
        self.assertEqual(raised.exception.status_code, 403)
        self.assertEqual(len(_ProviderClient.requests), 1)
        self.assertEqual(_ProviderClient.requests[0]["method"], "GET")

    async def test_chat_rejects_a_different_provider_from_the_saved_connection(self):
        self._patch_provider()
        with patch.object(agent, "_claims", return_value=self.claims), patch.object(agent, "_saved_gateway", return_value=("key-a", "openai", {})):
            with self.assertRaises(HTTPException) as raised:
                await agent.agent_chat(self._payload("model-a", provider="openrouter"), "Bearer firebase-token")
        self.assertEqual(raised.exception.status_code, 403)
        self.assertEqual(_ProviderClient.requests, [])

    async def test_user_supplied_urls_are_not_valid_provider_ids(self):
        with self.assertRaises(HTTPException) as raised:
            agent._normalize_provider("https://127.0.0.1:8000")
        self.assertEqual(raised.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
