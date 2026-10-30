"""Regression tests for accounts where Google does not allow email lookup."""
import ast
import unittest
from pathlib import Path
from unittest.mock import Mock

SOURCE = Path(__file__).resolve().parents[1].joinpath("messaging_app.py").read_text()
TREE = ast.parse(SOURCE)


def load_function(name, variables):
    target = next(node for node in TREE.body
                  if isinstance(node, ast.FunctionDef) and node.name == name)
    exec(compile(ast.Module(body=[target], type_ignores=[]),
                 "<chat-isolated>", "exec"), variables)
    return variables[name]


class RestrictedAccountTests(unittest.TestCase):
    def test_no_forced_google_oauth_login(self):
        # A school account restriction must never trigger a second sign-in.
        self.assertFalse(any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "authenticate_user"
            for node in ast.walk(TREE)
        ))
        self.assertIn("GOOGLE_ACCOUNT_NAME_AVAILABLE", SOURCE)
        self.assertIn("fallback_username(identity_public)", SOURCE)

    def test_existing_guest_identity_does_not_repost_profile(self):
        identity = "private-signed-identity"
        event = {"kind": "profile", "sender_key": identity, "sig": "valid"}
        save = Mock(return_value=True)
        context = {
            "identity_public": identity,
            "GOOGLE_ACCOUNT_NAME_AVAILABLE": False,
            "GOOGLE_ACCOUNT_USERNAME": "Guest-123",
            "read_messages": lambda: [event],
            "valid_signature": lambda evt: evt.get("sig") == "valid",
            "write_event": save,
            "event_now": Mock(return_value=event),
        }
        load_function("ensure_account_profile", context)()
        save.assert_not_called()

    def test_new_guest_profile_created_once(self):
        save = Mock(return_value=True)
        context = {
            "identity_public": "new-signed-identity",
            "GOOGLE_ACCOUNT_NAME_AVAILABLE": False,
            "GOOGLE_ACCOUNT_USERNAME": "Guest-578",
            "read_messages": lambda: [],
            "valid_signature": lambda evt: evt.get("sig") == "valid",
            "write_event": save,
            "event_now": Mock(return_value={"kind": "profile"}),
        }
        load_function("ensure_account_profile", context)()
        save.assert_called_once()


if __name__ == "__main__":
    unittest.main()
