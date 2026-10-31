"""Regression: launch-time shared chat validation must handle existing reactions.

The normal Colab startup calls read_messages() before the rest of the app
initializes. Preserve the module's original statement order in this test.
"""
import ast
import json
import re
import tempfile
import threading
import time
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "app" / "messaging_app.py"
TREE = ast.parse(SOURCE.read_text(encoding="utf-8"))
STARTUP_FUNCTIONS = {
    "is_plain_message", "is_title_event", "is_rename_event",
    "is_simple_reaction", "is_valid_emoji", "is_clock_event",
    "is_simple_record", "parse_chat_log", "read_messages"
}


def startup_module():
    """Use the real production definitions in actual source-code order."""
    statements = []
    for node in TREE.body:
        if isinstance(node, ast.FunctionDef) and node.name in STARTUP_FUNCTIONS:
            statements.append(node)
        elif (isinstance(node, ast.Expr) and
              isinstance(node.value, ast.Call) and
              isinstance(node.value.func, ast.Name) and
              node.value.func.id == "read_messages"):
            statements.append(node)
            break
    return compile(ast.Module(body=statements, type_ignores=[]),
                   str(SOURCE), "exec")


class StartupHistoryTests(unittest.TestCase):
    def make_env(self, entries):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "chat.json"
        path.write_text(json.dumps(entries), encoding="utf-8")
        error_class = type("ChatFileError", (Exception,), {})
        env = {
            "json": json, "re": re, "time": time,
            "file_lock": threading.RLock(),
            "CHAT_FILE_PATH": str(path),
            "ChatFileError": error_class,
            "APPEND_RECORD_SEPARATOR": "\\x1e",
            "PLAIN_MESSAGE_FIELDS": frozenset(("kind", "time", "user", "text")),
            "TITLE_FIELDS": frozenset(("kind", "time", "user", "title")),
            "RENAME_FIELDS": frozenset(("kind", "time", "user", "name")),
            "REACTION_FIELDS": frozenset(("kind", "time", "user", "target",
                                           "emoji", "active")),
            "CLOCK_FIELDS": frozenset(("kind", "time", "user", "epoch",
                                        "message_hash")),
            "TITLE_VERIFIED": "Verified", "TITLE_NONE": "None",
            "valid_username": lambda value: value if isinstance(value, str) else None,
        }
        # Executes the actual startup read in source order. This raised
        # NameError in v5.40 whenever chat.json contained any reactions.
        exec(startup_module(), env)
        return env

    def test_startup_validates_existing_chat_reactions_before_opening_ui(self):
        now = int(time.time())
        entries = [
            {"kind": "message", "time": "12:00:00", "user": "Alex",
             "text": "Welcome"},
            {"kind": "reaction", "time": "12:00:01", "user": "Emma",
             "target": 1, "emoji": "👍", "active": True},
            {"kind": "title", "time": "12:00:02", "user": "Emma",
             "title": "Verified"},
            {"kind": "rename", "time": "12:00:03", "user": "Emma",
             "name": "Emma R"},
            {"kind": "clock",
             "time": time.strftime("%H:%M:%S", time.gmtime(now)),
             "user": "Alex", "epoch": now, "message_hash": "a" * 64},
        ]
        env = self.make_env(entries)
        self.assertEqual(env["read_messages"](), entries)

    def test_reaction_validator_exists_before_startup_read(self):
        helper = next(n for n in TREE.body
                      if isinstance(n, ast.FunctionDef) and
                      n.name == "is_valid_emoji")
        startup = next(n for n in TREE.body
                       if isinstance(n, ast.Expr) and
                       isinstance(n.value, ast.Call) and
                       isinstance(n.value.func, ast.Name) and
                       n.value.func.id == "read_messages")
        self.assertLess(helper.lineno, startup.lineno)

    def test_malformed_emoji_is_rejected_not_name_or_type_error(self):
        entries = [{"kind": "message", "time": "12:00:00",
                    "user": "Alex", "text": "test"}]
        env = self.make_env(entries)
        for value in (None, [], {}, 42, True, ""):
            with self.subTest(value=value):
                self.assertFalse(env["is_valid_emoji"](value))
                invalid = {"kind": "reaction", "time": "12:00:01",
                           "user": "Emma", "target": 1, "emoji": value,
                           "active": True}
                self.assertFalse(env["is_simple_reaction"](invalid))


if __name__ == "__main__":
    unittest.main()
