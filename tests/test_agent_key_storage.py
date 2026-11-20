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
        return FakeSnapshot(self._value())

    def set(self, value, merge=False):
        parent = self._parent(create=True)
        document_id = self.path[-1]
        existing = parent.get(document_id)
        if not merge or not isinstance(existing, dict):
            parent[document_id] = copy.deepcopy(value)
            return
        self._merge(existing, value)

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

    async def test_openrouter_key_is_encrypted_and_keeps_legacy_settings_until_verified(self):
        api_key = "key-that-must-stay-encrypted"
        old_router_key = "legacy-router-test-key"
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
        other_user_key = "other-user-router-test-key"
        agent._set_key_record("user-b", "9router", {
            "encryptedKey": agent._fernet().encrypt(other_user_key.encode()).decode(),
            "baseUrl": "https://other-router.example/v1",
            "enabled": True,
        })
        payload = agent.AgentKeyPayload(provider="openrouter", api_key=api_key)

        with patch.object(agent, "_claims", return_value={"uid": "user-a"}):
            response = await agent.save_agent_key(payload, "Bearer firebase-token")

        user_data = self._user_data("user-a")
        saved = user_data["agentKeys"]["openrouter"]
        self.assertEqual(response["provider"], "openrouter")
        self.assertEqual(saved["fingerprint"], agent._fingerprint(api_key))
        self.assertEqual(agent._fernet().decrypt(saved["encryptedKey"].encode()).decode(), api_key)
        self.assertIn("9router", user_data["agentKeys"])
        self.assertEqual(user_data["nineRouterApiKey"], old_router_key)
        self.assertNotIn(api_key, str(user_data))

        with patch.object(agent, "_claims", return_value={"uid": "user-a"}), patch.object(
            agent, "_fetch_openrouter_models", side_effect=HTTPException(status_code=401, detail="invalid")
        ):
            with self.assertRaises(HTTPException):
                await agent.verify_openrouter_key("Bearer firebase-token")
        self.assertIn("9router", self._user_data("user-a")["agentKeys"])
        self.assertEqual(self._user_data("user-a")["nineRouterApiKey"], old_router_key)

        with patch.object(agent, "_claims", return_value={"uid": "user-a"}), patch.object(
            agent, "_fetch_openrouter_models", return_value=[{"id": "free-model:free"}]
        ):
            verified = await agent.verify_openrouter_key("Bearer firebase-token")
        self.assertEqual(verified["freeModelCount"], 1)
        user_data = self._user_data("user-a")
        self.assertNotIn("9router", user_data["agentKeys"])
        self.assertNotIn("nineRouterApiKey", user_data)
        self.assertNotIn("nineRouterModel", user_data)
        self.assertEqual(user_data["displayName"], "kept")
        self.assertFalse(agent._legacy_key_doc("user-a", "9router").get().exists)
        self.assertIn("9router", self._user_data("user-b")["agentKeys"])


if __name__ == "__main__":
    unittest.main()
