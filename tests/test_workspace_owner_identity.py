import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from modules import workspace


OWNER_EMAIL = "warthadev@gmail.com"
OWNER_UID = "y0XK4xxWRPWYckpe9eLHIptjJHu2"


class _RuntimeResponse:
    status_code = 200

    @staticmethod
    def json():
        return {"success": True}


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


class WorkspaceOwnerIdentityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        workspace._verified_owner_uid.cache_clear()
        _RuntimeClient.requests = []

    def tearDown(self):
        workspace._verified_owner_uid.cache_clear()

    @staticmethod
    def _owner_record(*, provider_id="google.com", provider_email=OWNER_EMAIL, uid=OWNER_UID, disabled=False):
        provider = SimpleNamespace(provider_id=provider_id, email=provider_email)
        return SimpleNamespace(
            uid=uid,
            email=None,
            email_verified=False,
            disabled=disabled,
            provider_data=[provider],
        )

    def test_pinned_google_uid_is_owner_when_top_level_email_is_absent(self):
        record = self._owner_record()
        with patch.dict(os.environ, {"AGENT_OWNER_UID": OWNER_UID}), \
                patch.object(workspace, "init_firestore"), \
                patch.object(workspace.auth, "get_user", return_value=record) as get_user, \
                patch.object(workspace.auth, "get_user_by_email") as get_by_email:
            claims = {"uid": OWNER_UID, "email": None, "email_verified": False}
            self.assertEqual(workspace._verified_owner_uid(), OWNER_UID)
            self.assertTrue(workspace.is_owner_claims(claims))
            self.assertEqual(workspace._identity_headers(claims, "internal-test-key")["X-Agent-User-Role"], "owner")
            with patch.object(workspace, "_claims", return_value=claims):
                self.assertEqual(workspace.workspace_info("Bearer firebase-token"), {
                    "uid": OWNER_UID,
                    "owner": True,
                    "storage": "per-user",
                })
        get_user.assert_called_once_with(OWNER_UID)
        get_by_email.assert_not_called()

    def test_same_email_provider_accounts_with_other_uids_are_not_owner(self):
        record = self._owner_record()
        claims = {
            "uid": "different-provider-uid",
            "email": OWNER_EMAIL,
            "email_verified": True,
            "role": "owner",
            "owner_uid": OWNER_UID,
        }
        with patch.dict(os.environ, {"AGENT_OWNER_UID": OWNER_UID}), \
                patch.object(workspace, "init_firestore"), \
                patch.object(workspace.auth, "get_user", return_value=record):
            self.assertFalse(workspace.is_owner_claims(claims))

    def test_missing_owner_uid_fails_closed_without_email_fallback(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AGENT_OWNER_UID", None)
            with patch.object(workspace, "init_firestore"), \
                    patch.object(workspace.auth, "get_user") as get_user, \
                    patch.object(workspace.auth, "get_user_by_email") as get_by_email:
                self.assertEqual(workspace._verified_owner_uid(), "")
                self.assertFalse(workspace.is_owner_claims({"uid": OWNER_UID}))
        get_user.assert_not_called()
        get_by_email.assert_not_called()

    def test_invalid_uid_or_non_google_identity_fails_closed(self):
        mismatch = self._owner_record(uid="unexpected-uid")
        wrong_provider = self._owner_record(provider_id="github.com")
        with patch.dict(os.environ, {"AGENT_OWNER_UID": OWNER_UID}), \
                patch.object(workspace, "init_firestore"), \
                patch.object(workspace.auth, "get_user", return_value=mismatch):
            self.assertEqual(workspace._verified_owner_uid(), "")
        workspace._verified_owner_uid.cache_clear()
        with patch.dict(os.environ, {"AGENT_OWNER_UID": OWNER_UID}), \
                patch.object(workspace, "init_firestore"), \
                patch.object(workspace.auth, "get_user", return_value=wrong_provider):
            self.assertEqual(workspace._verified_owner_uid(), "")
        workspace._verified_owner_uid.cache_clear()
        with patch.dict(os.environ, {"AGENT_OWNER_UID": OWNER_UID}), \
                patch.object(workspace, "init_firestore"), \
                patch.object(workspace.auth, "get_user", side_effect=RuntimeError("not found")):
            self.assertEqual(workspace._verified_owner_uid(), "")

    async def test_spoofed_body_identity_fields_cannot_change_trusted_role_or_uid(self):
        record = self._owner_record()
        claims = {"uid": "regular-user-uid", "email": OWNER_EMAIL, "email_verified": True}
        spoofed_payload = {
            "path": ".",
            "uid": OWNER_UID,
            "user_uid": OWNER_UID,
            "role": "owner",
            "owner_uid": OWNER_UID,
            "is_owner": True,
        }
        with patch.dict(os.environ, {"AGENT_OWNER_UID": OWNER_UID}), \
                patch.object(workspace, "init_firestore"), \
                patch.object(workspace.auth, "get_user", return_value=record), \
                patch.object(workspace, "_runtime_config", return_value=("https://runtime.example", "internal-test-key")), \
                patch.object(workspace.httpx, "AsyncClient", _RuntimeClient):
            await workspace.runtime_request("/v1/workspace/list", claims, payload=spoofed_payload)

        request = _RuntimeClient.requests[0]
        self.assertEqual(request["headers"]["X-Agent-User-UID"], claims["uid"])
        self.assertEqual(request["headers"]["X-Agent-User-Role"], "user")
        self.assertEqual(request["headers"]["X-Agent-Owner-UID"], OWNER_UID)
        self.assertEqual(request["json"], {"uid": claims["uid"], "path": "."})


if __name__ == "__main__":
    unittest.main()
