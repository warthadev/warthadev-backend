import copy
import os
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet
from fastapi import HTTPException
from firebase_admin import firestore

from modules import agent
from modules.agent_ssrf import RouterTarget


class FakeSnapshot:
    def __init__(self, value):
        self.exists = value is not None
        self._value = copy.deepcopy(value)

    def to_dict(self):
        return copy.deepcopy(self._value)


class FakeDocument:
    def __init__(self, database, path):
        self.database = database
        self.path = path

    def _parent(self, create=False):
        node = self.database.data
        for part in self.path[:-1]:
            if create:
                node = node.setdefault(part, {})
            else:
                node = node.get(part, {})
                if not isinstance(node, dict):
                    return None
        return node

    def _value(self):
        parent = self._parent()
        return parent.get(self.path[-1]) if isinstance(parent, dict) else None

    def get(self):
        value = self._value()
        if self.database.corrupt_pending_readback and self.path == ("users", "user-a"):
            self.database.corrupt_pending_readback = False
            value = copy.deepcopy(value)
            value["agentKeys"]["openrouter"]["pendingApiKey"] = "readback-mismatch"
        return FakeSnapshot(value)

    def set(self, value, merge=False):
        parent = self._parent(create=True)
        document_id = self.path[-1]
        existing = parent.get(document_id)
        if not merge or not isinstance(existing, dict):
            parent[document_id] = copy.deepcopy(value)
        else:
            self._merge(existing, value)
        openrouter = ((value.get("agentKeys") or {}).get("openrouter") or {})
        if (
            self.database.corrupt_pending_on_stage
            and isinstance(openrouter, dict)
            and "pendingApiKey" in openrouter
        ):
            self.database.corrupt_pending_on_stage = False
            self.database.corrupt_pending_readback = True

    @classmethod
    def _merge(cls, destination, source):
        for key, value in source.items():
            if isinstance(value, dict) and isinstance(destination.get(key), dict):
                cls._merge(destination[key], value)
            elif isinstance(value, dict):
                destination[key] = copy.deepcopy(value)
            else:
                destination[key] = copy.deepcopy(value)

    def update(self, updates):
        parent = self._parent()
        if not isinstance(parent, dict) or self.path[-1] not in parent:
            raise KeyError("document does not exist")
        document = parent[self.path[-1]]
        for field_path, value in updates.items():
            parts = field_path.split(".")
            target = document
            for part in parts[:-1]:
                target = target.setdefault(part, {})
            if value is firestore.DELETE_FIELD:
                target.pop(parts[-1], None)
            else:
                target[parts[-1]] = copy.deepcopy(value)

    def delete(self):
        parent = self._parent()
        if isinstance(parent, dict):
            parent.pop(self.path[-1], None)

    def collection(self, name):
        return FakeCollection(self.database, (*self.path, name))


class FakeCollection:
    def __init__(self, database, path):
        self.database = database
        self.path = path

    def document(self, document_id):
        return FakeDocument(self.database, (*self.path, document_id))


class FakeFirestore:
    def __init__(self):
        self.data = {}
        self.corrupt_pending_readback = False
        self.corrupt_pending_on_stage = False

    def collection(self, name):
        return FakeCollection(self, (name,))


class AgentKeyStorageTests(unittest.TestCase):
    def setUp(self):
        self.database = FakeFirestore()
        self.encryption_key = Fernet.generate_key().decode()
        self.firestore_patch = patch.object(agent, "init_firestore", return_value=self.database)
        self.firestore_patch.start()
        self.env_patch = patch.dict(os.environ, {"AGENT_ENCRYPTION_KEY": self.encryption_key})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.addCleanup(self.firestore_patch.stop)

    def _user_data(self, user_id):
        return self.database.data.get("users", {}).get(user_id, {})

    def _record(self, api_key, base_url):
        return {
            "encryptedKey": agent._fernet().encrypt(api_key.encode()).decode(),
            "baseUrl": base_url,
            "model": "",
            "fingerprint": agent._fingerprint(api_key),
            "enabled": True,
        }

    def test_keys_and_urls_are_per_user_and_key_is_encrypted(self):
        api_key_a = "router-secret-user-a"
        api_key_b = "router-secret-user-b"
        agent._set_key_record("user-a", "9router", self._record(api_key_a, "https://router-a.example/v1"))
        agent._set_key_record("user-b", "9router", self._record(api_key_b, "https://router-b.example/v1"))

        stored_a = self._user_data("user-a")["agentKeys"]["9router"]
        stored_b = self._user_data("user-b")["agentKeys"]["9router"]
        self.assertNotIn(api_key_a, str(self._user_data("user-a")))
        self.assertNotIn(api_key_b, str(self._user_data("user-b")))
        self.assertEqual(agent._fernet().decrypt(stored_a["encryptedKey"].encode()).decode(), api_key_a)
        self.assertEqual(agent._fernet().decrypt(stored_b["encryptedKey"].encode()).decode(), api_key_b)
        self.assertEqual(stored_a["baseUrl"], "https://router-a.example/v1")
        self.assertEqual(stored_b["baseUrl"], "https://router-b.example/v1")
        self.assertNotIn("agent_users", self.database.data)

    def test_legacy_record_can_still_be_read_for_safe_migration(self):
        api_key = "9router-secret-for-user-a"
        agent._set_key_record("user-a", "9router", self._record(api_key, "https://router-a.example/v1"))

        saved = agent._get_key_record("user-a", "9router")
        data = self._user_data("user-a")
        self.assertEqual(saved["baseUrl"], "https://router-a.example/v1")
        self.assertEqual(agent._fernet().decrypt(saved["encryptedKey"].encode()).decode(), api_key)
        self.assertNotIn(api_key, str(data))
        self.assertIn("9router", data["agentKeys"])
        self.assertNotIn("gateway", data["agentKeys"])
        self.assertNotIn("agent_users", self.database.data)

    def test_legacy_plaintext_field_is_encrypted_and_cleared_on_access(self):
        legacy_key = "legacy-router-secret"
        self.database.data["users"] = {
            "user-a": {
                "nineRouterApiKey": legacy_key,
                "nineRouterModel": "openrouter/example-model",
                "displayName": "kept",
            }
        }

        record = agent._get_key_record("user-a", "9router")
        stored_user = self._user_data("user-a")

        self.assertEqual(agent._fernet().decrypt(record["encryptedKey"].encode()).decode(), legacy_key)
        self.assertEqual(record["model"], "openrouter/example-model")
        self.assertEqual(record["baseUrl"], "")
        self.assertIsNone(stored_user["nineRouterApiKey"])
        self.assertIsNone(stored_user["nineRouterModel"])
        self.assertEqual(stored_user["displayName"], "kept")

    def test_legacy_encrypted_subcollection_is_moved_to_user_document(self):
        legacy_key = "already-encrypted-secret"
        legacy_ciphertext = agent._fernet().encrypt(legacy_key.encode()).decode()
        legacy_doc = (
            self.database.collection("agent_users")
            .document("user-a")
            .collection("keys")
            .document("9router")
        )
        legacy_doc.set({
            "encryptedKey": legacy_ciphertext,
            "baseUrl": "https://router-a.example/v1",
            "model": "cloud/model-a",
            "fingerprint": "fingerprint",
            "enabled": True,
        })

        record = agent._get_key_record("user-a", "9router")

        self.assertEqual(record["encryptedKey"], legacy_ciphertext)
        self.assertEqual(record["baseUrl"], "https://router-a.example/v1")
        self.assertEqual(self._user_data("user-a")["agentKeys"]["9router"], record)
        self.assertFalse(legacy_doc.get().exists)
        self.assertEqual(agent._fernet().decrypt(record["encryptedKey"].encode()).decode(), legacy_key)

    def test_deleting_one_provider_preserves_other_user_fields_and_keys(self):
        agent._set_key_record("user-a", "9router", {"encryptedKey": "cipher-a", "baseUrl": "https://router.example", "enabled": True})
        agent._set_key_record("user-a", "gemini", {"encryptedKey": "cipher-b", "enabled": True})
        self.database.collection("users").document("user-a").set({"displayName": "kept"}, merge=True)

        agent._delete_key_record("user-a", "9router")

        data = self._user_data("user-a")
        self.assertNotIn("9router", data["agentKeys"])
        self.assertEqual(data["agentKeys"]["gemini"]["encryptedKey"], "cipher-b")
        self.assertEqual(data["displayName"], "kept")


class AgentKeyUpdateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.database = FakeFirestore()
        self.encryption_key = Fernet.generate_key().decode()
        self.firestore_patch = patch.object(agent, "init_firestore", return_value=self.database)
        self.firestore_patch.start()
        self.env_patch = patch.dict(os.environ, {"AGENT_ENCRYPTION_KEY": self.encryption_key})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.addCleanup(self.firestore_patch.stop)

    def _user_data(self, user_id):
        return self.database.data.get("users", {}).get(user_id, {})

    def _set_old_openrouter_key(self, user_id, api_key):
        agent._set_key_record(user_id, "openrouter", {
            "encryptedKey": agent._fernet().encrypt(api_key.encode()).decode(),
            "fingerprint": agent._fingerprint(api_key),
            "enabled": True,
        })

    async def test_new_key_is_plaintext_only_after_auth_catalog_and_readback_and_scope_is_per_uid(self):
        api_key = "verified-openrouter-key-for-user-a"
        other_user_key = "other-user-router-test-key"
        old_router_key = "legacy-router-test-key"
        self._set_old_openrouter_key("user-a", "old-encrypted-openrouter-key")
        agent._set_key_record("user-a", "9router", {
            "encryptedKey": agent._fernet().encrypt(old_router_key.encode()).decode(),
            "baseUrl": "https://old-router.example/v1",
            "fingerprint": agent._fingerprint(old_router_key),
            "enabled": True,
        })
        self.database.collection("users").document("user-a").set({
            "nineRouterApiKey": old_router_key,
            "nineRouterModel": "old-model",
            "displayName": "kept",
        }, merge=True)
        agent._legacy_key_doc("user-a", "9router").set({"encryptedKey": "legacy-cipher"})
        agent._set_key_record("user-b", "9router", {
            "encryptedKey": agent._fernet().encrypt(other_user_key.encode()).decode(),
            "baseUrl": "https://other-router.example/v1",
            "enabled": True,
        })
        order = []

        async def verify(_api_key):
            order.append("auth")

        async def fetch(_api_key):
            order.append("catalog")
            return [{"id": "free-model:free"}]

        payload = agent.AgentKeyPayload(provider="openrouter", api_key=api_key)
        with patch.object(agent, "_claims", return_value={"uid": "user-a"}), patch.object(
            agent, "_verify_openrouter_authentication", side_effect=verify
        ), patch.object(agent, "_fetch_openrouter_models", side_effect=fetch):
            response = await agent.save_agent_key(payload, "Bearer firebase-token")

        user_data = self._user_data("user-a")
        saved = user_data["agentKeys"]["openrouter"]
        self.assertEqual(order, ["auth", "catalog"])
        self.assertEqual(response["provider"], "openrouter")
        self.assertEqual(response["apiKey"], api_key)
        self.assertEqual(response["freeModelCount"], 1)
        self.assertEqual(saved["apiKey"], api_key)
        self.assertNotIn("encryptedKey", saved)
        self.assertNotIn("pendingApiKey", saved)
        self.assertNotIn("fingerprint", saved)
        self.assertNotIn("9router", user_data["agentKeys"])
        self.assertNotIn("nineRouterApiKey", user_data)
        self.assertNotIn("nineRouterModel", user_data)
        self.assertEqual(user_data["displayName"], "kept")
        self.assertFalse(agent._legacy_key_doc("user-a", "9router").get().exists)
        self.assertIn("9router", self._user_data("user-b")["agentKeys"])

        with patch.object(agent, "_claims", return_value={"uid": "user-a"}):
            owner_response = agent.list_agent_keys("Bearer owner-token")
        self.assertEqual(owner_response["keys"][0]["apiKey"], api_key)
        self.assertNotIn(other_user_key, str(owner_response))
        with patch.object(agent, "_claims", return_value={"uid": "user-b"}):
            other_response = agent.list_agent_keys("Bearer other-token")
        self.assertNotIn(api_key, str(other_response))
        self.assertTrue(all(item["provider"] != "openrouter" for item in other_response["keys"]))

    async def test_invalid_key_retains_ciphertext_and_legacy_settings(self):
        self._set_old_openrouter_key("user-a", "old-encrypted-key")
        old_ciphertext = self._user_data("user-a")["agentKeys"]["openrouter"]["encryptedKey"]
        agent._set_key_record("user-a", "9router", {"encryptedKey": "legacy-cipher", "enabled": True})
        payload = agent.AgentKeyPayload(provider="openrouter", api_key="invalid-key")

        with patch.object(agent, "_claims", return_value={"uid": "user-a"}), patch.object(
            agent, "_verify_openrouter_authentication", side_effect=HTTPException(status_code=401, detail="invalid")
        ), patch.object(agent, "_fetch_openrouter_models") as fetch_models:
            with self.assertRaises(HTTPException):
                await agent.save_agent_key(payload, "Bearer firebase-token")
            fetch_models.assert_not_awaited()

        saved = self._user_data("user-a")["agentKeys"]["openrouter"]
        self.assertEqual(saved["encryptedKey"], old_ciphertext)
        self.assertNotIn("apiKey", saved)
        self.assertIn("9router", self._user_data("user-a")["agentKeys"])

    async def test_key_return_rejects_unauthenticated_request(self):
        with patch.object(agent, "_claims", side_effect=HTTPException(status_code=401, detail="Authentication required")):
            with self.assertRaises(HTTPException) as raised:
                agent.list_agent_keys(None)
        self.assertEqual(raised.exception.status_code, 401)

    async def test_catalog_failure_retains_ciphertext_and_legacy_settings(self):
        self._set_old_openrouter_key("user-a", "old-encrypted-key")
        old_ciphertext = self._user_data("user-a")["agentKeys"]["openrouter"]["encryptedKey"]
        agent._set_key_record("user-a", "9router", {"encryptedKey": "legacy-cipher", "enabled": True})
        payload = agent.AgentKeyPayload(provider="openrouter", api_key="verified-but-catalog-failed")

        with patch.object(agent, "_claims", return_value={"uid": "user-a"}), patch.object(
            agent, "_verify_openrouter_authentication", return_value=None
        ), patch.object(agent, "_fetch_openrouter_models", side_effect=HTTPException(status_code=503, detail="unavailable")):
            with self.assertRaises(HTTPException):
                await agent.save_agent_key(payload, "Bearer firebase-token")

        saved = self._user_data("user-a")["agentKeys"]["openrouter"]
        self.assertEqual(saved["encryptedKey"], old_ciphertext)
        self.assertNotIn("apiKey", saved)
        self.assertIn("9router", self._user_data("user-a")["agentKeys"])

    async def test_failed_plaintext_readback_retains_ciphertext_and_old_key_remains_active(self):
        self._set_old_openrouter_key("user-a", "old-encrypted-key")
        self.database.corrupt_pending_on_stage = True
        old_ciphertext = self._user_data("user-a")["agentKeys"]["openrouter"]["encryptedKey"]
        agent._set_key_record("user-a", "9router", {"encryptedKey": "legacy-cipher", "enabled": True})
        payload = agent.AgentKeyPayload(provider="openrouter", api_key="candidate-key")

        with patch.object(agent, "_claims", return_value={"uid": "user-a"}), patch.object(
            agent, "_verify_openrouter_authentication", return_value=None
        ), patch.object(agent, "_fetch_openrouter_models", return_value=[{"id": "free-model:free"}]):
            with self.assertRaises(HTTPException) as raised:
                await agent.save_agent_key(payload, "Bearer firebase-token")

        self.assertEqual(raised.exception.status_code, 500)
        saved = self._user_data("user-a")["agentKeys"]["openrouter"]
        self.assertEqual(saved["encryptedKey"], old_ciphertext)
        self.assertNotIn("apiKey", saved)
        self.assertEqual(saved["pendingApiKey"], "candidate-key")
        self.assertIn("9router", self._user_data("user-a")["agentKeys"])
        active_key, _ = agent._saved_openrouter_credentials("user-a")
        self.assertEqual(active_key, "old-encrypted-key")

    async def test_verifying_existing_ciphertext_migrates_to_plaintext_after_catalog_success(self):
        old_key = "owner-encrypted-openrouter-key"
        self._set_old_openrouter_key("user-a", old_key)
        with patch.object(agent, "_claims", return_value={"uid": "user-a"}):
            before = agent.list_agent_keys("Bearer firebase-token")
        self.assertEqual(before["keys"][0]["apiKey"], old_key)
        self.assertTrue(before["keys"][0]["migrationRequired"])

        with patch.object(agent, "_claims", return_value={"uid": "user-a"}), patch.object(
            agent, "_verify_openrouter_authentication", return_value=None
        ), patch.object(agent, "_fetch_openrouter_models", return_value=[{"id": "free-model:free"}]):
            response = await agent.save_agent_key(
                agent.AgentKeyPayload(provider="openrouter", api_key=""),
                "Bearer firebase-token",
            )

        saved = self._user_data("user-a")["agentKeys"]["openrouter"]
        self.assertEqual(response["apiKey"], old_key)
        self.assertEqual(response["freeModelCount"], 1)
        self.assertEqual(saved["apiKey"], old_key)
        self.assertNotIn("encryptedKey", saved)
        with patch.object(agent, "_claims", return_value={"uid": "user-a"}):
            after = agent.list_agent_keys("Bearer firebase-token")
        self.assertEqual(after["keys"][0]["apiKey"], old_key)
        self.assertFalse(after["keys"][0]["migrationRequired"])


if __name__ == "__main__":
    unittest.main()
