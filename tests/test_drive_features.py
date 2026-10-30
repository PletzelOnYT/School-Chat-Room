"""Single shared chat.json storage, advanced search, and threads tests."""
import ast
import base64
import hashlib
import json
import os
import re
import shlex
import tempfile
import threading
import time
import unittest
import uuid
from datetime import datetime
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1].joinpath("messaging_app.py").read_text()
TREE = ast.parse(SOURCE)
NAMES = {"read_messages", "write_event", "search_messages", "build_threads"}
NODES = [n for n in TREE.body if isinstance(n, ast.FunctionDef) and n.name in NAMES]


def methods(overrides=None):
    env = dict(
        os=os, re=re, shlex=shlex, json=json, time=time, uuid=uuid,
        hashlib=hashlib, datetime=datetime,
        valid_signature=lambda e: isinstance(e, dict) and e.get("sig") == "valid",
        ChatFileError=type("ChatFileError", (Exception,), {}),
        file_lock=threading.RLock(),
        short_id=lambda key: (key or "")[:12],
    )
    if overrides:
        env.update(overrides)
    exec(compile(ast.Module(body=NODES, type_ignores=[]), "<messaging-app-storage>", "exec"), env)
    return env


class SingleDriveFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.file = self.folder / "chat.json"
        self.anchor = str(uuid.uuid4())
        self.original = {"id": self.anchor, "kind": "message", "text": "Original history"}
        self.file.write_text(json.dumps([self.original]))
        self.env = methods({
            "CHAT_FILE_PATH": str(self.file),
            "ORIGINAL_HISTORY_ANCHOR": self.anchor,
        })

    def event(self, msg):
        return {
            "id": str(uuid.uuid4()), "kind": "message", "sig": "valid",
            "issued_at": int(time.time()), "text": msg,
        }

    def test_read_existing_history_and_write_in_one_json(self):
        saved = self.env["read_messages"]()
        self.assertEqual(saved, [self.original])
        for i in range(12):
            self.assertTrue(self.env["write_event"](self.event(f"New message {i}")))
        records = json.loads(self.file.read_text())
        self.assertEqual(len(records), 13)
        self.assertEqual(records[0], self.original)
        self.assertEqual(len(list(self.folder.iterdir())), 1)
        self.assertEqual(self.file.name, "chat.json")

    def test_invalid_signature_cannot_be_saved(self):
        invalid = self.event("spoofed")
        invalid["sig"] = "invalid"
        with self.assertRaises(self.env["ChatFileError"]):
            self.env["write_event"](invalid)
        self.assertEqual(len(json.loads(self.file.read_text())), 1)

    def test_wrong_original_chat_is_rejected(self):
        other = self.event("not this room")
        self.file.write_text(json.dumps([other]))
        with self.assertRaises(self.env["ChatFileError"]):
            self.env["read_messages"]()

    def test_search_filters_work_without_attachments(self):
        messages = [
            {"user": "Alex", "text": "math homework", "issued_at": 1793491200,
             "sender_key": "keyAlex", "reactions": {}, "reply_to": None},
            {"user": "Emma", "text": "quiz notes", "issued_at": 1793577600,
             "sender_key": "keyEmma", "reactions": {"❤️": 2}, "reply_to": "msg1"},
        ]
        search = self.env["search_messages"]
        self.assertEqual(len(search("from:Emma has:reaction", messages)), 1)
        self.assertEqual(len(search("in:replies", messages)), 1)
        self.assertEqual(len(search("from:Alex math", messages)), 1)
        self.assertEqual(len(search("after:2026-11-02 has:reaction", messages)), 1)
        with self.assertRaises(self.env["ChatFileError"]):
            search("has:attachment", messages)
        with self.assertRaises(self.env["ChatFileError"]):
            search("after:2026-99-99", messages)

    def test_nested_replies_expand_under_one_root(self):
        rows = [
            {"id": "a", "number": 1, "user": "Alex", "reply_to": None},
            {"id": "b", "number": 2, "user": "Emma", "reply_to": "a"},
            {"id": "c", "number": 3, "user": "Alex", "reply_to": "b"},
            {"id": "d", "number": 4, "user": "Jay", "reply_to": None},
        ]
        threads = self.env["build_threads"](rows, 15, set(), 0)
        self.assertEqual(len(threads), 2)
        self.assertEqual(threads[0]["root"]["id"], "a")
        self.assertEqual(threads[0]["total_replies"], 2)
        self.assertEqual([row["id"] for row in threads[0]["replies"]], ["b", "c"])


if __name__ == "__main__":
    unittest.main()
