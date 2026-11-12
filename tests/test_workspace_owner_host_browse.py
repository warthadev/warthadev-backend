import sys
import types
import unittest
from unittest.mock import AsyncMock, patch

try:
    import httpx  # noqa: F401
except ModuleNotFoundError:
    httpx_stub = types.ModuleType("httpx")

    class UnusedAsyncClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("No HTTP client should be created in these tests")

    httpx_stub.AsyncClient = UnusedAsyncClient
    httpx_stub.HTTPError = type("HTTPError", (Exception,), {})
    sys.modules["httpx"] = httpx_stub

try:
    from firebase_admin import auth as _firebase_auth  # noqa: F401
except ModuleNotFoundError:
    firebase_admin_stub = types.ModuleType("firebase_admin")
    firebase_auth_stub = types.ModuleType("firebase_admin.auth")
    firebase_auth_stub.get_user = lambda _uid: None
    firebase_auth_stub.get_user_by_email = lambda _email: None
    firebase_auth_stub.verify_id_token = lambda _token: {}
    firebase_admin_stub.auth = firebase_auth_stub
    sys.modules["firebase_admin"] = firebase_admin_stub
    sys.modules["firebase_admin.auth"] = firebase_auth_stub

firestore_storage_stub = types.ModuleType("modules.firestore_storage")
firestore_storage_stub.init_firestore = lambda: None
sys.modules.setdefault("modules.firestore_storage", firestore_storage_stub)

from fastapi import HTTPException

from modules import workspace


OWNER_UID = "y0XK4xxWRPWYckpe9eLHIptjJHu2"


class WorkspaceOwnerHostBrowseTests(unittest.IsolatedAsyncioTestCase):
    async def test_owner_lists_host_directory_using_verified_claims(self):
        claims = {"uid": OWNER_UID}
        result = {"path": "/", "entries": []}
        with (
            patch.object(workspace, "_claims", return_value=claims) as verify_claims,
            patch.object(workspace, "_verified_owner_uid", return_value=OWNER_UID),
            patch.object(workspace, "runtime_request", new_callable=AsyncMock, return_value=result) as runtime_request,
        ):
            response = await workspace.list_owner_host_files(path="/", authorization="Bearer owner-token")

        self.assertEqual(response, result)
        verify_claims.assert_called_once_with("Bearer owner-token")
        runtime_request.assert_awaited_once_with(
            "/v1/workspace/owner/list",
            claims,
            payload={"path": "/"},
        )

    async def test_owner_reads_host_file_only_through_explicit_path_request(self):
        claims = {"uid": OWNER_UID}
        result = {"path": "/etc/hostname", "text": "sandbox", "isText": True, "size": 7}
        with (
            patch.object(workspace, "_claims", return_value=claims),
            patch.object(workspace, "_verified_owner_uid", return_value=OWNER_UID),
            patch.object(workspace, "runtime_request", new_callable=AsyncMock, return_value=result) as runtime_request,
        ):
            response = await workspace.read_owner_host_file(path="/etc/hostname", authorization="Bearer owner-token")

        self.assertEqual(response, result)
        runtime_request.assert_awaited_once_with(
            "/v1/workspace/owner/read",
            claims,
            payload={"path": "/etc/hostname"},
        )

    async def test_non_owner_cannot_list_or_read_host_files(self):
        claims = {"uid": "regular-user-uid"}
        with (
            patch.object(workspace, "_claims", return_value=claims),
            patch.object(workspace, "_verified_owner_uid", return_value=OWNER_UID),
            patch.object(workspace, "runtime_request", new_callable=AsyncMock) as runtime_request,
        ):
            for route, kwargs in (
                (workspace.list_owner_host_files, {"path": "/"}),
                (workspace.read_owner_host_file, {"path": "/etc/hostname"}),
            ):
                with self.subTest(route=route.__name__):
                    with self.assertRaises(HTTPException) as raised:
                        await route(**kwargs, authorization="Bearer regular-user-token")
                    self.assertEqual(raised.exception.status_code, 403)

        runtime_request.assert_not_awaited()

    async def test_missing_bearer_is_rejected_before_any_host_operation(self):
        with patch.object(workspace, "runtime_request", new_callable=AsyncMock) as runtime_request:
            with self.assertRaises(HTTPException) as raised:
                await workspace.list_owner_host_files(path="/", authorization=None)

        self.assertEqual(raised.exception.status_code, 401)
        runtime_request.assert_not_awaited()

    def test_routes_accept_no_body_uid_or_role_fields(self):
        self.assertEqual(set(workspace.list_owner_host_files.__annotations__), {"path", "authorization"})
        self.assertEqual(set(workspace.read_owner_host_file.__annotations__), {"path", "authorization"})


if __name__ == "__main__":
    unittest.main()
