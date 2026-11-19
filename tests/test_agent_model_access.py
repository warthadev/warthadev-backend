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

    async def test_models_without_key_are_empty_and_never_read_local_inventory(self):
        key_patch = patch.object(agent, "_saved_key", return_value=None)
        catalog_patch = patch.object(agent, "_fetch_router_models", new=AsyncMock())
        inventory_patch = patch.object(agent, "runtime_request", new=AsyncMock())
        with patch.object(agent, "_claims", return_value=self.claims), key_patch, catalog_patch as fetch_catalog, inventory_patch as inventory:
            response = await agent.agent_models("Bearer firebase-token")

        self.assertFalse(response["configured"])
        self.assertEqual(response["models"], [])
        fetch_catalog.assert_not_awaited()
        inventory.assert_not_awaited()

    async def test_models_return_only_the_account_9router_catalog(self):
        key_patch = patch.object(agent, "_saved_key", return_value=("user-router-key", {}))
        catalog_patch = patch.object(agent, "_fetch_router_models", new=AsyncMock(return_value=[{"id": "cloud/model-a"}]))
        inventory_patch = patch.object(agent, "runtime_request", new=AsyncMock())
        with patch.object(agent, "_claims", return_value=self.claims), key_patch, catalog_patch, inventory_patch as inventory:
            response = await agent.agent_models("Bearer firebase-token")

        self.assertTrue(response["configured"])
        self.assertEqual(response["models"], [{"id": "cloud/model-a"}])
        inventory.assert_not_awaited()

    async def test_legacy_local_model_without_router_key_is_rejected(self):
        key_patch = patch.object(agent, "_saved_key", return_value=None)
        catalog_patch = patch.object(agent, "_fetch_router_models", new=AsyncMock())
        inventory_patch = patch.object(agent, "runtime_request", new=AsyncMock())
        with patch.object(agent, "_claims", return_value=self.claims), key_patch, catalog_patch as fetch_catalog, inventory_patch as inventory:
            with self.assertRaises(HTTPException) as raised:
                await agent.agent_chat(self._payload("mimo-v2.6:latest"), "Bearer firebase-token")

        self.assertEqual(raised.exception.status_code, 403)
        fetch_catalog.assert_not_awaited()
        inventory.assert_not_awaited()
        self.assertEqual(_RuntimeClient.requests, [])

    async def test_cloud_chat_uses_this_users_key_and_requires_catalog_membership(self):
        await self._patch_runtime()
        key_patch = patch.object(agent, "_saved_key", return_value=("this-users-key", {"model": "cloud/model-a"}))
        catalog_patch = patch.object(agent, "_fetch_router_models", new=AsyncMock(return_value=[{"id": "cloud/model-a"}]))
        with key_patch as load_key, catalog_patch as fetch_catalog:
            response = await agent.agent_chat(self._payload("cloud/model-a"), "Bearer firebase-token")

        self.assertTrue(response["success"])
        load_key.assert_called_once_with(self.claims["uid"], "9router")
        fetch_catalog.assert_awaited_once_with("this-users-key")
        self.assertEqual(_RuntimeClient.requests[0]["json"]["provider_api_key"], "this-users-key")

    async def test_cloud_chat_without_key_or_outside_catalog_is_rejected(self):
        await self._patch_runtime()
        key_patch = patch.object(agent, "_saved_key", return_value=None)
        catalog_patch = patch.object(agent, "_fetch_router_models", new=AsyncMock())
        with key_patch, catalog_patch as fetch:
            with self.assertRaises(HTTPException) as raised:
                await agent.agent_chat(self._payload("cloud/model-a"), "Bearer firebase-token")
        self.assertEqual(raised.exception.status_code, 403)
        fetch.assert_not_awaited()

        key_patch = patch.object(agent, "_saved_key", return_value=("this-users-key", {}))
        catalog_patch = patch.object(agent, "_fetch_router_models", new=AsyncMock(return_value=[{"id": "cloud/model-b"}]))
        with key_patch, catalog_patch:
            with self.assertRaises(HTTPException) as raised:
                await agent.agent_chat(self._payload("cloud/model-a"), "Bearer firebase-token")
        self.assertEqual(raised.exception.status_code, 403)
        self.assertEqual(_RuntimeClient.requests, [])


if __name__ == "__main__":
    unittest.main()
