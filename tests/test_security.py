"""Regression tests for the single self-claimed Verified title."""
import ast
import base64
from collections import Counter
import hashlib
import re
import time
import unicodedata
import unittest
from pathlib import Path

source = Path(__file__).resolve().parents[1].joinpath("messaging_app.py").read_text()
tree = ast.parse(source)
names = {
    "analyze", "valid_username", "valid_incoming_message", "is_gibberish",
    "gibberish_reason",
    "guest_digest", "fallback_username", "short_id",
    "resolve_verified_target",
}
functions = [node for node in tree.body
             if isinstance(node, ast.FunctionDef) and node.name in names]
env = {
    "re": re, "time": time, "hashlib": hashlib, "unicodedata": unicodedata,
    "Counter": Counter,
    "MESSAGE_LIMIT": 1000,
    "GENERIC_USERNAMES": {"colabuser", "guest", "user"},
    "RESERVED_USERNAMES": {
        "owner", "mod", "moderator", "admin", "administrator",
        "verified", "system", "server", "staff", "school chat",
        "schoolchat", "security", "support",
    },
    "GUEST_NAME_RE": re.compile(r"(?i)^guest-(?:[0-9]{2,3}|[0-9a-f]{8,64})$"),
    "b64decode": lambda s: base64.b64decode(s, validate=True),
    "valid_signature": lambda e: e.get("sig") == "test-valid",
    "is_valid_emoji": lambda _: True,
}
exec(compile(ast.Module(body=functions, type_ignores=[]), "<chat>", "exec"), env)


class VerifiedOnlyTests(unittest.TestCase):
    def setUp(self):
        self.a = base64.b64encode(bytes([1])*32).decode()
        self.b = base64.b64encode(bytes([2])*32).decode()
        self.now = int(time.time())

    def evt(self, identity, kind="message", **kwargs):
        event = dict(id=identity, kind=kind, time="12:00:00",
                     issued_at=self.now, sig="test-valid")
        event.update(kwargs)
        return event

    def test_only_verified_self_claim_is_effective(self):
        entries = [
            self.evt("first", sender_key=self.a, user="Guest-101", text="hello"),
            self.evt("claim", kind="role", sender_key=self.a,
                     role="verified", target_key=self.a, active=True,
                     self_verified=True, verification_version=2),
            self.evt("second", sender_key=self.a, user="Guest-101", text="after"),
        ]
        state = env["analyze"](entries)
        self.assertEqual(len(state["messages"]), 2)
        self.assertTrue(all(row["verified"] for row in state["messages"]))
        self.assertEqual(state["verified"], {self.a})

    def test_old_privileged_grants_are_inert(self):
        entries = [
            self.evt("owner", kind="role", sender_key="server",
                     role="owner", target_key=self.a, active=True),
            self.evt("mod", kind="role", sender_key="server",
                     role="moderator", target_key=self.a, active=True),
            self.evt("old-verified", kind="role", sender_key="server",
                     role="verified", target_key=self.a, active=True),
            self.evt("banned", kind="role", sender_key="server",
                     role="banned", target_key=self.b, active=True),
            self.evt("post", sender_key=self.b, user="Guest-204",
                     text="message still visible"),
        ]
        state = env["analyze"](entries)
        self.assertEqual(state["verified"], set())
        self.assertEqual(len(state["messages"]), 1)
        self.assertNotIn("owners", state)
        self.assertNotIn("moderators", state)
        self.assertNotIn("banned", state)

    def test_cannot_self_claim_for_someone_else(self):
        state = env["analyze"]([
            self.evt("spoof", kind="role", sender_key=self.a,
                     role="verified", target_key=self.b, active=True,
                     self_verified=True, verification_version=2),
            self.evt("other", kind="role", sender_key=self.a,
                     role="verified", target_key=self.a, active=True,
                     self_verified=False),
            self.evt("unsigned", kind="role", sender_key=self.b,
                     role="verified", target_key=self.b, active=True,
                     self_verified=True, sig="invalid"),
        ])
        self.assertFalse(state["verified"])

    def test_verified_can_rename_and_unverified_cannot(self):
        base = self.evt("base", kind="profile", sender_key=self.a,
                        user="student", account_username="student")
        spoof = self.evt("spoof", kind="profile", sender_key=self.a,
                         user="Cheater", account_username="student", renamed=True)
        self.assertEqual(env["analyze"]([base, spoof])["display_names"][self.a],
                         "student")
        claim = self.evt("claim", kind="role", sender_key=self.a,
                         role="verified", target_key=self.a, active=True,
                         self_verified=True)
        valid = self.evt("rename", kind="profile", sender_key=self.a,
                         user="CoolName", account_username="student", renamed=True)
        state = env["analyze"]([base, spoof, claim, valid])
        self.assertEqual(state["display_names"][self.a], "CoolName")

    def test_no_automatic_or_legacy_verification(self):
        legacy = self.evt("old-auto", kind="role", sender_key=self.a,
                          role="verified", target_key=self.a, active=True,
                          self_verified=True)
        data = env["analyze"]([legacy])
        self.assertNotIn(self.a, data["verified"])
        self.assertIn("if command == '/vf':", source)
        self.assertNotIn("if os.path.ismount('/content/drive'):\n    try:\n        claim_self_verified()", source)

    def test_any_signed_user_can_unverify_any_signed_user(self):
        entries = [
            self.evt("claim", kind="role", sender_key=self.a,
                     role="verified", target_key=self.a, active=True,
                     self_verified=True, verification_version=2),
            self.evt("revoke", kind="role", sender_key=self.b,
                     role="verified", target_key=self.a, active=False,
                     unverify_anyone=True, verification_version=2),
        ]
        result = env["analyze"](entries)
        self.assertNotIn(self.a, result["verified"])
        # A subsequent explicit /vf restores the checkmark.
        entries.append(self.evt("again", kind="role", sender_key=self.a,
                                role="verified", target_key=self.a,
                                active=True, self_verified=True,
                                verification_version=2))
        self.assertIn(self.a, env["analyze"](entries)["verified"])

    def test_bad_revoke_or_fake_verify_is_ignored(self):
        events = [
            self.evt("new-claim", kind="role", sender_key=self.a,
                     role="verified", target_key=self.a, active=True,
                     self_verified=True, verification_version=2),
            self.evt("wrong-version", kind="role", sender_key=self.b,
                     role="verified", target_key=self.a, active=False,
                     unverify_anyone=True),
            self.evt("wrong-role", kind="role", sender_key=self.b,
                     role="moderator", target_key=self.a, active=False,
                     unverify_anyone=True, verification_version=2),
            self.evt("unsigned", kind="role", sender_key=self.b,
                     role="verified", target_key=self.a, active=False,
                     unverify_anyone=True, verification_version=2,
                     sig="bad"),
        ]
        self.assertIn(self.a, env["analyze"](events)["verified"])
        events.append(self.evt("forged-self", kind="role", sender_key=self.b,
                               role="verified", target_key=self.a,
                               active=True, self_verified=True,
                               verification_version=2))
        self.assertNotIn(self.b, env["analyze"](events)["verified"])

    def test_revoke_resolution_is_exact_and_disambiguated(self):
        state = {"display_names": {
            self.a: "Alex",
            self.b: "Emma Marie",
        }}
        resolver = env["resolve_verified_target"]
        self.assertEqual(resolver("alex", state), self.a)
        self.assertEqual(resolver("@Emma Marie", state), self.b)
        self.assertEqual(resolver(self.b[:12], state), self.b)
        self.assertIsNone(resolver("Emm", state))
        self.assertIsNone(resolver("", state))
        self.assertIsNone(resolver("Alex", {
            "display_names": {self.a: "Alex", self.b: "alex"}}))

    def test_name_conflicts_do_not_merge_identity(self):
        events = [
            self.evt("first", kind="profile", sender_key=self.a,
                     user="alex", account_username="alex"),
            self.evt("second", kind="profile", sender_key=self.b,
                     user="alex", account_username="alex"),
        ]
        names = env["analyze"](events)["display_names"]
        self.assertNotEqual(names[self.a], names[self.b])

    def test_filter_normal_text_and_flood(self):
        self.assertTrue(env["valid_incoming_message"]("How's class today?"))
        self.assertFalse(env["valid_incoming_message"]("a"*22))
        self.assertFalse(env["valid_incoming_message"]("x"+chr(0x202e)+"y"))

    def test_only_blue_title_in_ui_and_no_external_role_api(self):
        self.assertIn("colored('☑ ', '#3498FF', true)", source)
        self.assertIn("if command == '/vf':", source)
        help_const = next(node.value for node in tree.body
                          if isinstance(node, ast.Assign)
                          and any(isinstance(t, ast.Name) and t.id == "HELP_TEXT"
                                  for t in node.targets))
        self.assertNotIn("/vf", ast.literal_eval(help_const))
        self.assertNotIn("/uvf", ast.literal_eval(help_const))
        self.assertIn("if command == '/uvf':", source)
        for absent in (
            "OWNER_AUTH_URL", "SERVER_PUBLIC_KEY", "call_owner_server",
            "call_moderator_server", "read_remote_role_events",
            "[OWNER]", "[MOD]", "MOD_HELP", "role_storage",
            "upstash", "vercel", "my_owner", "my_mod", "my_banned",
        ):
            self.assertNotIn(absent.lower(), source.lower())


if __name__ == "__main__":
    unittest.main()
