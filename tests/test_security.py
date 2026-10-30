import ast
import base64
import hashlib
import re
import time
import unicodedata
import unittest
from pathlib import Path

# Extract logic without executing Google Colab Drive mounts or UI callbacks.
source = Path(__file__).resolve().parents[1].joinpath("school_chat.py").read_text()
tree = ast.parse(source)
names = {
    "analyze", "valid_username", "valid_incoming_message", "is_gibberish",
    "guest_digest", "fallback_username", "short_id",
}
nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
env = {
    "re": re, "time": time, "hashlib": hashlib, "unicodedata": unicodedata,
    "MESSAGE_LIMIT": 1000,
    "SERVER_PUBLIC_KEY": "server",
    "GENERIC_USERNAMES": {"colabuser", "guest", "user"},
    "RESERVED_USERNAMES": {"owner", "admin", "mod", "verified", "system"},
    "GUEST_NAME_RE": re.compile(r"(?i)^guest-(?:[0-9]{2,3}|[0-9a-f]{8,64})$"),
    "b64decode": lambda s: base64.b64decode(s, validate=True),
    "valid_signature": lambda e: e.get("sig") == "test-valid",
    "is_valid_emoji": lambda _: True,
}
exec(compile(ast.Module(body=nodes, type_ignores=[]), "<chat-functions>", "exec"), env)


class SecurityTests(unittest.TestCase):
    def setUp(self):
        self.a = base64.b64encode(bytes([1]) * 32).decode()
        self.b = base64.b64encode(bytes([2]) * 32).decode()
        self.now = int(time.time())

    def evt(self, ident, kind="message", **kwargs):
        return {
            "id": ident, "kind": kind, "time": "12:00:00",
            "issued_at": self.now, "sig": "test-valid", **kwargs,
        }

    def test_verified_and_moderator_role_changes(self):
        arr = [
            self.evt("m1", sender_key=self.a, user="Guest-111", text="first"),
            self.evt("r1", kind="role", sender_key="server", role="moderator",
                     target_key=self.b, active=True),
            self.evt("r2", kind="role", sender_key="server", role="verified",
                     target_key=self.a, active=True),
            self.evt("p1", kind="profile", sender_key=self.a, user="StudentName"),
        ]
        data = env["analyze"](arr)
        self.assertIn(self.b, data["moderators"])
        self.assertIn(self.a, data["verified"])
        self.assertEqual(data["display_names"][self.a], "StudentName")

    def test_ban_and_unban_filter_subsequent_posts(self):
        arr = [
            self.evt("m1", sender_key=self.a, user="Guest-111", text="before ban"),
            self.evt("ban", kind="role", sender_key="server", role="banned",
                     target_key=self.a, active=True),
            self.evt("m2", sender_key=self.a, user="Guest-111", text="while banned"),
            self.evt("unban", kind="role", sender_key="server", role="banned",
                     target_key=self.a, active=False),
            self.evt("m3", sender_key=self.a, user="Guest-111", text="after unban"),
        ]
        data = env["analyze"](arr)
        self.assertEqual([m["text"] for m in data["messages"]],
                         ["before ban", "after unban"])
        self.assertNotIn(self.a, data["banned"])

    def test_forged_roles_and_bad_names_are_ignored(self):
        arr = [
            self.evt("fake-role", kind="role", sender_key=self.a, role="moderator",
                     target_key=self.a, active=True),
            self.evt("msg", sender_key=self.a, user="[OWNER] Admin", text="hi"),
        ]
        data = env["analyze"](arr)
        self.assertNotIn(self.a, data["moderators"])
        self.assertTrue(data["display_names"][self.a].startswith("Guest-"))

    def test_receivers_filter_bot_floods(self):
        self.assertFalse(env["valid_incoming_message"]("a" * 20))
        self.assertFalse(env["valid_incoming_message"]("Hi" + chr(0x202e) + "invalid"))
        self.assertTrue(env["valid_incoming_message"]("Hey, how are you? :)"))
        arr = [self.evt("msg" + str(i), sender_key=self.a, user="Guest-111",
                        text="spam " + str(i)) for i in range(15)]
        data = env["analyze"](arr)
        self.assertLessEqual(len(data["messages"]), 7)

    def test_google_account_name_without_badge(self):
        name = "student123"
        events = [
            self.evt("profile", kind="profile", sender_key=self.a, user=name,
                     account_username=name),
        ]
        state = env["analyze"](events)
        self.assertEqual(state["display_names"][self.a], name)
        self.assertNotIn(self.a, state["verified"])

    def test_unverified_cannot_force_rename_but_owner_can(self):
        events = [
            self.evt("profile", kind="profile", sender_key=self.a,
                     user="student123", account_username="student123"),
            self.evt("bad", kind="profile", sender_key=self.a, user="FakeName",
                     account_username="student123", renamed=True),
        ]
        state = env["analyze"](events)
        self.assertEqual(state["display_names"][self.a], "student123")
        events.extend([
            self.evt("owner", kind="role", sender_key="server", role="owner",
                     target_key=self.a, active=True,
                     expires_at=self.now + 3600),
            self.evt("good", kind="profile", sender_key=self.a, user="CoolName",
                     account_username="student123", renamed=True),
        ])
        self.assertEqual(env["analyze"](events)["display_names"][self.a],
                         "CoolName")

    def test_mod_can_rename_without_verified(self):
        events = [
            self.evt("mod", kind="role", sender_key="server", role="moderator",
                     target_key=self.a, active=True),
            self.evt("profile", kind="profile", sender_key=self.a,
                     user="MyNick", account_username="student123",
                     renamed=True),
        ]
        state = env["analyze"](events)
        self.assertEqual(state["display_names"][self.a], "MyNick")
        self.assertNotIn(self.a, state["verified"])

    def test_unique_guest_name_assignment(self):
        arr = [self.evt("msg1", sender_key=self.a, user="Guest-111", text="hi"),
               self.evt("msg2", sender_key=self.b, user="Guest-222", text="hi")]
        data = env["analyze"](arr)
        self.assertNotEqual(data["display_names"][self.a],
                            data["display_names"][self.b])


if __name__ == "__main__":
    unittest.main()
