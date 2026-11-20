import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from modules import agent


class AgentModelAccessTests(unittest.IsolatedAsyncioTestCase):
    claims = {"uid": "user-a", "email": "user@example.com", "email_verified": True}
    credentials = {
        "user-a": ("https://router-a.example/v1", "key-a", {}),
        "user-b": ("https://router-b.example/v1", "key-b", {}),
    }
    allowed_models = {"key-a": "model-a", "key-b": "model-b"}

    def setUp(self):
        self.requests: list[dict] = []

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

    def _fake_router_request(self):
        async def request(method, base_url, endpoint, api_key, json_body=None):
            self.requests.append({
                "method": method,
                "base_url": base_url,
                "endpoint": endpoint,
                "api_key": api_key,
                "json": json_body,
            })
            if endpoint == "models":
                model_id = self.allowed_models.get(api_key, "model-default")
                payload = {"data": [{"id": model_id}]}
            else:
                payload = {
                    "model": json_body["model"],
                    "choices": [{"message": {"content": "Mock answer"}}],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
                }
            return agent.RouterHTTPResponse(status_code=200, body=json.dumps(payload).encode())
        return request

    def _patch_router(self):
        stack = patch.object(agent, "_router_request", side_effect=self._fake_router_request())
        stack.start()
        self.addCleanup(stack.stop)

    def _saved_for_user(self, user_id):
        return self.credentials.get(user_id)

    async def test_models_without_key_are_empty_and_never_call_router(self):
        self._patch_router()
        with patch.object(agent, "_claims", return_value=self.claims), patch.object(agent, "_saved_router_credentials", return_value=None):
            response = await agent.agent_models("Bearer firebase-token")

        self.assertFalse(response["configured"])
        self.assertEqual(response["models"], [])
        self.assertEqual(self.requests, [])

    async def test_catalog_uses_each_accounts_own_url_and_9router_key(self):
        self._patch_router()
        for user_id in ("user-a", "user-b"):
            with patch.object(agent, "_claims", return_value={"uid": user_id}), patch.object(agent, "_saved_router_credentials", side_effect=self._saved_for_user):
                response = await agent.agent_models("Bearer firebase-token")
            self.assertTrue(response["configured"])
            self.assertEqual(response["provider"], "9router")
            self.assertEqual(response["models"], [{"id": f"model-{user_id[-1]}"}])

        self.assertEqual(
            [(request["base_url"], request["api_key"]) for request in self.requests],
            [("https://router-a.example/v1", "key-a"), ("https://router-b.example/v1", "key-b")],
        )

    async def test_chat_uses_same_account_url_and_key_for_catalog_and_chat(self):
        self._patch_router()
        with patch.object(agent, "_claims", return_value={"uid": "user-a"}), patch.object(agent, "_saved_router_credentials", side_effect=self._saved_for_user):
            response = await agent.agent_chat(self._payload("model-a"), "Bearer firebase-token")

        self.assertTrue(response["success"])
        self.assertEqual(response["userId"], "user-a")
        self.assertEqual(response["provider"], "9router")
        self.assertEqual(response["data"]["result"]["content"], "Mock answer")
        self.assertEqual(len(self.requests), 2)
        catalog_request, chat_request = self.requests
        self.assertEqual(catalog_request["endpoint"], "models")
        self.assertEqual(chat_request["endpoint"], "chat/completions")
        self.assertEqual(catalog_request["base_url"], "https://router-a.example/v1")
        self.assertEqual(chat_request["base_url"], catalog_request["base_url"])
        self.assertEqual(chat_request["api_key"], catalog_request["api_key"])
        self.assertEqual(chat_request["api_key"], "key-a")
        self.assertEqual(chat_request["json"]["model"], "model-a")
        self.assertNotIn("key-a", str(chat_request["json"]))
        self.assertNotIn("tools", chat_request["json"])
        self.assertNotIn("web_search", chat_request["json"])

    async def test_chat_cannot_use_model_outside_this_keys_catalog(self):
        self._patch_router()
        with patch.object(agent, "_claims", return_value={"uid": "user-b"}), patch.object(agent, "_saved_router_credentials", side_effect=self._saved_for_user):
            with self.assertRaises(HTTPException) as raised:
                await agent.agent_chat(self._payload("model-a"), "Bearer firebase-token")

        self.assertEqual(raised.exception.status_code, 403)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0]["api_key"], "key-b")
        self.assertEqual(self.requests[0]["base_url"], "https://router-b.example/v1")

    async def test_missing_account_url_fails_closed_without_global_fallback(self):
        self._patch_router()
        with patch.dict(os.environ, {"AGENT_9ROUTER_BASE_URL": "https://legacy-global.example/v1"}), patch.object(agent, "_claims", return_value=self.claims), patch.object(agent, "_saved_router_credentials", return_value=("", "key-a", {})):
            with self.assertRaises(HTTPException) as raised:
                await agent.agent_models("Bearer firebase-token")

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("base URL", raised.exception.detail)
        self.assertEqual(self.requests, [])

    async def test_redirect_status_is_rejected(self):
        with self.assertRaises(HTTPException) as raised:
            agent._raise_router_http_error(agent.RouterHTTPResponse(status_code=302, body=b""), "chat")
        self.assertEqual(raised.exception.status_code, 502)
        self.assertIn("redirects are not allowed", raised.exception.detail)

    async def test_direct_provider_is_not_supported(self):
        with self.assertRaises(HTTPException) as raised:
            agent._normalize_provider("openai")
        self.assertEqual(raised.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
