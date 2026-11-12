import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from modules import agent


class _RuntimeResponse:
    status_code = 200
    headers = {}

    @staticmethod
    def json():
        return {"answer": "ok"}


class _RuntimeClient:
    requests: list[dict] = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def post(self, url, *, headers, json):
        self.requests.append({"url": url, "headers": headers, "json": json})
        return _RuntimeResponse()


class AgentModelAccessTests(unittest.IsolatedAsyncioTestCase):
    claims = {"uid": "firebase-user-123", "email": "user@example.com", "email_verified": True}

    def setUp(self):
        _RuntimeClient.requests = []

    def _payload(self, model: str):
        return SimpleNamespace(
            prompt="hello",
            provider="9router",
            model=model,
            web_search=False,
            search_query="",
            timezone="Asia/Jakarta",
            history=[],
            images=[],
        )

    async def _patch_runtime(self):
        stack = patch.object(agent.httpx, "AsyncClient", _RuntimeClient)
        stack.start()
        self.addCleanup(stack.stop)
        config_patch = patch.object(agent, "_agent_runtime_config", return_value=("https://runtime.example", "internal-test-key"))
        config_patch.start()
        self.addCleanup(config_patch.stop)
        owner_patch = patch.object(agent, "_verified_owner_uid", return_value="verified-owner-uid")
        owner_patch.start()
        self.addCleanup(owner_patch.stop)
        claims_patch = patch.object(agent, "is_owner_claims", return_value=False)
        claims_patch.start()
        self.addCleanup(claims_patch.stop)
        auth_patch = patch.object(agent, "_claims", return_value=self.claims)
        auth_patch.start()
        self.addCleanup(auth_patch.stop)
        images_patch = patch.object(agent, "_validate_chat_images", return_value=[])
        images_patch.start()
        self.addCleanup(images_patch.stop)

    async def test_models_returns_local_inventory_without_saved_router_key(self):
        local_patch = patch.object(agent, "_local_model_ids", new=AsyncMock(return_value=["mimo-v2.6:latest"]))
        key_patch = patch.object(agent, "_saved_key", return_value=None)
        catalog_patch = patch.object(agent, "_fetch_router_models", new=AsyncMock())
        with patch.object(agent, "_claims", return_value=self.claims), local_patch, key_patch, catalog_patch as fetch_catalog:
            response = await agent.agent_models("Bearer firebase-token")

        self.assertFalse(response["configured"])
        self.assertEqual(response["models"], [{"id": "mimo-v2.6:latest", "capabilities": {"local": True}}])
        fetch_catalog.assert_not_awaited()

    async def test_models_combines_account_catalog_and_local_inventory(self):
        local_patch = patch.object(agent, "_local_model_ids", new=AsyncMock(return_value=["mimo-v2.6:latest"]))
        key_patch = patch.object(agent, "_saved_key", return_value=("user-router-key", {}))
        catalog_patch = patch.object(agent, "_fetch_router_models", new=AsyncMock(return_value=[{"id": "cloud/model-a"}]))
        with patch.object(agent, "_claims", return_value=self.claims), local_patch, key_patch, catalog_patch:
            response = await agent.agent_models("Bearer firebase-token")

        self.assertTrue(response["configured"])
        self.assertEqual({model["id"] for model in response["models"]}, {"cloud/model-a", "mimo-v2.6:latest"})

    async def test_registered_local_chat_needs_no_router_key(self):
        await self._patch_runtime()
        local_patch = patch.object(agent, "_local_model_ids", new=AsyncMock(return_value=["mimo-v2.6:latest"]))
        key_patch = patch.object(agent, "_saved_key", side_effect=AssertionError("local path must not load a router key"))
        catalog_patch = patch.object(agent, "_fetch_router_models", new=AsyncMock(side_effect=AssertionError("local path must not query cloud catalog")))
        with local_patch, key_patch, catalog_patch:
            response = await agent.agent_chat(self._payload("mimo-v2.6:latest"), "Bearer firebase-token")

        self.assertTrue(response["success"])
        self.assertEqual(_RuntimeClient.requests[0]["json"]["provider_api_key"], "")
        self.assertEqual(_RuntimeClient.requests[0]["json"]["user_uid"], self.claims["uid"])

    async def test_unregistered_local_named_model_is_not_a_keyless_bypass(self):
        local_patch = patch.object(agent, "_local_model_ids", new=AsyncMock(return_value=[]))
        key_patch = patch.object(agent, "_saved_key", return_value=None)
        with patch.object(agent, "_claims", return_value=self.claims), local_patch, key_patch:
            with self.assertRaises(HTTPException) as raised:
                await agent.agent_chat(self._payload("mimo-v2.6:latest"), "Bearer firebase-token")

        self.assertEqual(raised.exception.status_code, 403)

    async def test_cloud_chat_uses_this_users_key_and_requires_catalog_membership(self):
        await self._patch_runtime()
        local_patch = patch.object(agent, "_local_model_ids", new=AsyncMock(return_value=[]))
        key_patch = patch.object(agent, "_saved_key", return_value=("this-users-key", {"model": "cloud/model-a"}))
        catalog_patch = patch.object(agent, "_fetch_router_models", new=AsyncMock(return_value=[{"id": "cloud/model-a"}]))
        with local_patch, key_patch as load_key, catalog_patch as fetch_catalog:
            response = await agent.agent_chat(self._payload("cloud/model-a"), "Bearer firebase-token")

        self.assertTrue(response["success"])
        load_key.assert_called_once_with(self.claims["uid"], "9router")
        fetch_catalog.assert_awaited_once_with("this-users-key")
        self.assertEqual(_RuntimeClient.requests[0]["json"]["provider_api_key"], "this-users-key")

    async def test_cloud_chat_without_key_or_outside_catalog_is_rejected(self):
        await self._patch_runtime()
        local_patch = patch.object(agent, "_local_model_ids", new=AsyncMock(return_value=[]))
        key_patch = patch.object(agent, "_saved_key", return_value=None)
        fetch_catalog = patch.object(agent, "_fetch_router_models", new=AsyncMock())
        with local_patch, key_patch, fetch_catalog as fetch:
            with self.assertRaises(HTTPException) as raised:
                await agent.agent_chat(self._payload("cloud/model-a"), "Bearer firebase-token")
        self.assertEqual(raised.exception.status_code, 403)
        fetch.assert_not_awaited()

        key_patch = patch.object(agent, "_saved_key", return_value=("this-users-key", {}))
        catalog_patch = patch.object(agent, "_fetch_router_models", new=AsyncMock(return_value=[{"id": "cloud/model-b"}]))
        with local_patch, key_patch, catalog_patch:
            with self.assertRaises(HTTPException) as raised:
                await agent.agent_chat(self._payload("cloud/model-a"), "Bearer firebase-token")
        self.assertEqual(raised.exception.status_code, 403)
        self.assertEqual(_RuntimeClient.requests, [])


if __name__ == "__main__":
    unittest.main()
