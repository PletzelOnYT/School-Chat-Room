"""Role ledger integration, version-gate, and signature filtering tests."""
import ast
import io
import json
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1].joinpath("school_chat.py").read_text()
tree = ast.parse(SOURCE)
node = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
            and n.name == "read_remote_role_events")


class RemoteRolesTests(unittest.TestCase):
    def context(self, data):
        env = {
            "time": time, "urllib": urllib, "json": json,
            "OWNER_AUTH_URL": "https://example.invalid/api/owner",
            "REMOTE_ROLE_REFRESH_SECONDS": 2.0,
            "CLIENT_VERSION": "5.16",
            "last_remote_role_check": 0.0,
            "remote_role_events": [],
            "remote_role_status": "unavailable",
            "minimum_client_version": "",
            "update_required": False,
            "trusted_server_event": lambda e: e.get("sig") == "trusted",
        }
        exec(compile(ast.Module(body=[node], type_ignores=[]),
                     "<role-ledger>", "exec"), env)
        url_body = json.dumps(data).encode("utf-8")
        return env, url_body

    def test_signed_durable_grants_are_loaded(self):
        data = {
            "status": "online", "role_storage": "durable",
            "min_client_version": "5.16",
            "role_events": [
                {"id": "A", "kind": "role", "role": "moderator",
                 "target_key": "identity", "sig": "trusted"},
                {"id": "B", "kind": "role", "role": "owner",
                 "target_key": "identity", "sig": "trusted"},
                {"id": "C", "kind": "role", "role": "verified",
                 "target_key": "identity", "sig": "untrusted"},
            ],
        }
        env, raw = self.context(data)
        with patch("urllib.request.urlopen", return_value=io.BytesIO(raw)) as urlopen:
            roles = env["read_remote_role_events"]()
            self.assertEqual(len(roles), 1)
            self.assertEqual(roles[0]["id"], "A")
            self.assertEqual(env["remote_role_status"], "durable")
            self.assertFalse(env["update_required"])
            self.assertEqual(env["read_remote_role_events"](), roles)
            urlopen.assert_called_once()

    def test_newer_server_version_requires_restart(self):
        env, raw = self.context({
            "status": "online", "role_storage": "not_configured",
            "min_client_version": "5.17", "role_events": [],
        })
        with patch("urllib.request.urlopen", return_value=io.BytesIO(raw)):
            self.assertEqual(env["read_remote_role_events"](), [])
        self.assertTrue(env["update_required"])
        self.assertEqual(env["minimum_client_version"], "5.17")

    def test_cached_signed_roles_survive_temporary_outage(self):
        env, _ = self.context({})
        signed = {"id": "A", "kind": "role", "role": "banned", "sig": "trusted"}
        env["remote_role_events"] = [signed]
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("offline")):
            self.assertEqual(env["read_remote_role_events"](), [signed])
        self.assertEqual(env["remote_role_status"], "unavailable")


if __name__ == "__main__":
    unittest.main()
