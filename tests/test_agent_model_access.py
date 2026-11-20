import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from modules import agent


class AgentModelAccessTests(unittest.IsolatedAsyncioTestCase):
    claims = {"uid": "user-a", "email": "user@example.com", "email_verified": True}
    credentials = {
        "user-a": ("key-a", {}),
        "user-b": ("key-b", {}),
    }
    allowed_models = {"key-a": "model-a:free", "key-b": "model-b:free"}

    def setUp(self):
        self.requests: list[dict] = []

    def _payload(self, model: str, provider: str = "openrouter"):
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
                model_id = self.allowed_models.get(api_key, "model-default:free")
                payload = {"data": [
                    {"id": model_id, "pricing": {"prompt": "0", "completion": "0"}, "reasoning": False},
                    {"id": "paid/model", "pricing": {"prompt": "0.0001", "completion": "0.0002"}},
                ]}
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

    async def test_models_without_key_are_empty_and_never_call_openrouter(self):
        self._patch_router()
        with patch.object(agent, "_claims", return_value=self.claims), patch.object(agent, "_saved_openrouter_credentials", return_value=None):
            response = await agent.agent_models("Bearer firebase-token")

        self.assertFalse(response["configured"])
        self.assertEqual(response["provider"], "openrouter")
        self.assertEqual(response["models"], [])
        self.assertEqual(self.requests, [])

    async def test_catalog_is_fetched_with_each_accounts_own_key_and_only_free_models(self):
        self._patch_router()
        for user_id in ("user-a", "user-b"):
            with patch.object(agent, "_claims", return_value={"uid": user_id}), patch.object(agent, "_saved_openrouter_credentials", side_effect=self._saved_for_user):
                response = await agent.agent_models("Bearer firebase-token")
            self.assertTrue(response["configured"])
            self.assertEqual(response["provider"], "openrouter")
            self.assertEqual(response["models"], [{"id": f"model-{user_id[-1]}:free", "capabilities": {"reasoning": False}}])

        self.assertEqual(
            [(request["base_url"], request["api_key"]) for request in self.requests],
            [(agent.OPENROUTER_API_BASE_URL, "key-a"), (agent.OPENROUTER_API_BASE_URL, "key-b")],
        )

    async def test_chat_uses_same_account_key_and_selected_openrouter_model(self):
        self._patch_router()
        with patch.object(agent, "_claims", return_value={"uid": "user-a"}), patch.object(agent, "_saved_openrouter_credentials", side_effect=self._saved_for_user):
            response = await agent.agent_chat(self._payload("model-a:free"), "Bearer firebase-token")

        self.assertTrue(response["success"])
        self.assertEqual(response["userId"], "user-a")
        self.assertEqual(response["provider"], "openrouter")
        self.assertEqual(response["data"]["result"]["content"], "Mock answer")
        self.assertEqual(len(self.requests), 2)
        catalog_request, chat_request = self.requests
        self.assertEqual(catalog_request["endpoint"], "models")
        self.assertEqual(chat_request["endpoint"], "chat/completions")
        self.assertEqual(catalog_request["base_url"], agent.OPENROUTER_API_BASE_URL)
        self.assertEqual(chat_request["base_url"], catalog_request["base_url"])
        self.assertEqual(chat_request["api_key"], catalog_request["api_key"])
        self.assertEqual(chat_request["api_key"], "key-a")
        self.assertEqual(chat_request["json"]["model"], "model-a:free")
        self.assertNotIn("key-a", str(chat_request["json"]))
        self.assertNotIn("tools", chat_request["json"])
        self.assertNotIn("web_search", chat_request["json"])

    async def test_chat_cannot_use_another_accounts_model(self):
        self._patch_router()
        with patch.object(agent, "_claims", return_value={"uid": "user-b"}), patch.object(agent, "_saved_openrouter_credentials", side_effect=self._saved_for_user):
            with self.assertRaises(HTTPException) as raised:
                await agent.agent_chat(self._payload("model-a:free"), "Bearer firebase-token")

        self.assertEqual(raised.exception.status_code, 403)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0]["api_key"], "key-b")

    async def test_chat_rejects_model_missing_from_current_free_catalog(self):
        self._patch_router()
        with patch.object(agent, "_claims", return_value={"uid": "user-a"}), patch.object(agent, "_saved_openrouter_credentials", side_effect=self._saved_for_user):
            with self.assertRaises(HTTPException) as raised:
                await agent.agent_chat(self._payload("paid/model"), "Bearer firebase-token")
        self.assertEqual(raised.exception.status_code, 403)
        self.assertEqual(len(self.requests), 1)

    async def test_free_catalog_requires_zero_input_and_output_price_or_free_variant(self):
        payload = {"data": [
            {"id": "zero-price", "pricing": {"prompt": "0", "completion": "0"}},
            {"id": "official:free", "pricing": {"prompt": "1", "completion": "2"}},
            {"id": "paid-output", "pricing": {"prompt": "0", "completion": "0.1"}},
            {"id": "paid-input", "pricing": {"prompt": "0.1", "completion": "0"}},
            {"id": "missing-price", "pricing": {"prompt": "0"}},
        ]}
        self.assertEqual(
            agent._extract_free_openrouter_models(payload),
            [{"id": "zero-price"}, {"id": "official:free"}],
        )

    async def test_redirect_status_is_rejected(self):
        with self.assertRaises(HTTPException) as raised:
            agent._raise_router_http_error(agent.RouterHTTPResponse(status_code=302, body=b""), "chat")
        self.assertEqual(raised.exception.status_code, 502)
        self.assertIn("redirects are not allowed", raised.exception.detail)

    async def test_response_reader_joins_stream_chunks_and_enforces_limit(self):
        class Content:
            def __init__(self, chunks):
                self.chunks = chunks

            async def iter_chunked(self, _chunk_size):
                for chunk in self.chunks:
                    yield chunk

        result = await agent._read_router_response(Content([b'{"da', b'ta":', b'[]}']))
        self.assertEqual(json.loads(result), {"data": []})
        with patch.object(agent, "MAX_ROUTER_RESPONSE_BYTES", 4), self.assertRaises(HTTPException) as raised:
            await agent._read_router_response(Content([b"123", b"45"]))
        self.assertEqual(raised.exception.status_code, 502)

    async def test_key_validation_uses_authenticated_endpoint_and_checks_response(self):
        with patch.object(agent, "_router_request", return_value=agent.RouterHTTPResponse(
            status_code=200, body=b'{"data":{"label":"valid"}}'
        )) as request:
            await agent._verify_openrouter_authentication("key-a")
        request.assert_awaited_once_with("GET", agent.OPENROUTER_API_BASE_URL, "key", "key-a")

        for response in (
            agent.RouterHTTPResponse(status_code=401, body=b'{}'),
            agent.RouterHTTPResponse(status_code=200, body=b'{}'),
            agent.RouterHTTPResponse(status_code=200, body=b'not-json'),
        ):
            with self.subTest(status=response.status_code, body=response.body), patch.object(
                agent, "_router_request", return_value=response
            ), self.assertRaises(HTTPException):
                await agent._verify_openrouter_authentication("invalid-key")

    async def test_only_openrouter_provider_is_supported(self):
        for provider in ("openai", "9router"):
            with self.subTest(provider=provider), self.assertRaises(HTTPException) as raised:
                agent._normalize_provider(provider)
            self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(agent._normalize_provider(" OpenRouter "), "openrouter")


if __name__ == "__main__":
    unittest.main()
