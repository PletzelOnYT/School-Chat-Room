"""Simulated shared Drive journal, attachments, advanced search and threads."""
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
from concurrent.futures import ThreadPoolExecutor

SOURCE = Path(__file__).resolve().parents[1].joinpath("school_chat.py").read_text()
TREE = ast.parse(SOURCE)
FUNCTIONS = [
    node for node in TREE.body if isinstance(node, ast.FunctionDef) and
    node.name in {"read_messages", "write_event", "validate_attachment_bytes",
                  "upload_attachment", "get_attachment", "search_messages",
                  "build_threads"}
]


def methods(overrides=None):
    env = dict(
        os=os, re=re, shlex=shlex, json=json, time=time, uuid=uuid,
        hashlib=hashlib, threading=threading, datetime=datetime,
        b64decode=lambda s: base64.b64decode(s, validate=True),
        b64=lambda x: base64.b64encode(x).decode("ascii"),
        valid_signature=lambda e: e.get("sig") == "valid",
        short_id=lambda key: str(key)[:12],
        event_cache={}, last_event_scan=0.0,
        ChatFileError=type("ChatFileError", (Exception,), {}),
        MESSAGE_LIMIT=1000,
        MAX_ATTACHMENT_BYTES=2*1024*1024,
        ALLOWED_ATTACHMENTS={
            "image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
            "image/webp": ".webp", "application/pdf": ".pdf",
            "text/plain": ".txt", "text/markdown": ".md",
        },
    )
    if overrides:
        env.update(overrides)
    exec(compile(ast.Module(body=FUNCTIONS, type_ignores=[]),
                 "<shared-drive-functions>", "exec"), env)
    return env


class DriveJournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.events = self.base / "events"
        self.events.mkdir()
        self.attachments = self.base / "attachments"
        self.attachments.mkdir()
        self.old = self.base / "chat.json"
        self.anchor = str(uuid.uuid4())
        self.old.write_text(json.dumps([{"id": self.anchor, "kind": "message",
                                        "text": "Old history is preserved"}]))
        self.env = methods({
            "CHAT_FILE_PATH": str(self.old),
            "EVENT_FOLDER": str(self.events),
            "ATTACHMENT_FOLDER": str(self.attachments),
            "ORIGINAL_HISTORY_ANCHOR": self.anchor,
            "file_lock": threading.RLock(),
            "legacy_cache": None, "event_cache": {}, "last_event_scan": 0.0,
        })

    def event(self, content):
        return {
            "id": str(uuid.uuid4()), "kind": "message", "sig": "valid",
            "issued_at": int(time.time()), "text": content,
            "sender_key": base64.b64encode(b"a"*32).decode(),
        }

    def test_writes_separate_files_without_touching_original_history(self):
        original = self.old.read_bytes()
        writes = [self.event("message " + str(i)) for i in range(50)]
        # Every simulated client has a different process-local lock and cache.
        def write(index):
            env = methods({
                "EVENT_FOLDER": str(self.events),
                "file_lock": threading.RLock(),
            })
            return env["write_event"](writes[index])
        with ThreadPoolExecutor(max_workers=12) as pool:
            self.assertTrue(all(pool.map(write, range(len(writes)))))
        self.assertEqual(original, self.old.read_bytes())
        self.assertEqual(len(list(self.events.glob("*.json"))), 50)
        messages = self.env["read_messages"]()
        self.assertEqual(len(messages), 51)
        self.assertEqual(messages[0]["id"], self.anchor)
        self.assertEqual({e["id"] for e in messages[1:]},
                         {e["id"] for e in writes})

    def test_syncing_partial_file_can_be_read_on_next_poll(self):
        event = self.event("recent")
        filename = f'{event["issued_at"]:010d}-{uuid.UUID(event["id"]).hex}.json'
        target = self.events / filename
        target.write_text("{")
        self.env["read_messages"]()
        target.write_text(json.dumps(event))
        self.env["last_event_scan"] = 0
        self.assertIn(event["id"],
                      [e["id"] for e in self.env["read_messages"]()])

    def test_attachment_validation(self):
        validate = self.env["validate_attachment_bytes"]
        self.assertTrue(validate(b"%PDF-1.7\n", "application/pdf"))
        self.assertFalse(validate(b"<script>bad</script>", "application/pdf"))
        self.assertFalse(validate(b"<svg></svg>", "image/svg+xml"))
        self.assertTrue(validate(b"school notes", "text/plain"))
        self.assertFalse(validate(b"\x00exe", "text/plain"))

    def test_attachment_upload_no_json_history_changes(self):
        payload = b"%PDF-1.7\ntest"
        saved = []
        env = methods({
            "ATTACHMENT_FOLDER": str(self.attachments),
            "event_now": lambda kind, **fields: {
                "kind": kind, "id": str(uuid.uuid4()), **fields},
            "write_event": lambda e: saved.append(e) or True,
        })
        env["write_event"] = lambda e: saved.append(e) or True
        result = env["upload_attachment"]({
            "mime": "application/pdf", "name": "algebra.pdf",
            "base64": base64.b64encode(payload).decode(),
        })
        self.assertEqual(result, saved[0]["id"])
        self.assertEqual(saved[0]["attachment"]["name"], "algebra.pdf")
        self.assertEqual(len(list(self.attachments.iterdir())), 1)
        self.assertIn(b"Old history is preserved", self.old.read_bytes())
        with self.assertRaises(env["ChatFileError"]):
            env["upload_attachment"]({
                "mime": "application/pdf", "name": "bad.pdf",
                "base64": base64.b64encode(b"<html>").decode(),
            })

    def test_attachment_download_checks_sha256(self):
        payload = b"PDF-attachment verified"
        name = uuid.uuid4().hex + ".txt"
        (self.attachments / name).write_bytes(payload)
        event_id = str(uuid.uuid4())
        descriptor = {
            "file": name, "name": "notes.txt", "mime": "text/plain",
            "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest(),
        }
        env = methods({
            "ATTACHMENT_FOLDER": str(self.attachments),
            "latest_analysis": lambda: {"messages": [
                {"id": event_id, "attachment": descriptor},
            ]},
            "JSON": lambda item: item,
        })
        output = env["get_attachment"](event_id)
        self.assertEqual(base64.b64decode(output["base64"]), payload)
        (self.attachments / name).write_bytes(b"tampered")
        with self.assertRaises(env["ChatFileError"]):
            env["get_attachment"](event_id)

    def test_search_by_name_date_and_attachments(self):
        a = self.event("math homework")
        messages = [
            {"user": "Alex", "text": "math homework",
             "issued_at": 1793491200, "attachment": None,
             "sender_key": "a", "reactions": {}, "reply_to": None},
            {"user": "Emma", "text": "notes attached",
             "issued_at": 1793577600, "attachment": {"name": "a.pdf"},
             "sender_key": "b", "reactions": {"❤️": 2}, "reply_to": "old"},
        ]
        search = self.env["search_messages"]
        self.assertEqual(len(search('from:Emma has:attachment', messages)), 1)
        self.assertEqual(len(search('in:replies has:reaction', messages)), 1)
        self.assertEqual(len(search('from:Alex math', messages)), 1)
        self.assertEqual(len(search('after:2026-11-01 has:attachment', messages)), 1)
        with self.assertRaises(self.env["ChatFileError"]):
            search('after:2026-99-99', messages)

    def test_nested_replies_form_one_expandable_thread(self):
        records = [
            {"id": "a", "number": 1, "user": "Alex", "reply_to": None},
            {"id": "b", "number": 2, "user": "Emma", "reply_to": "a"},
            {"id": "c", "number": 3, "user": "Alex", "reply_to": "b"},
            {"id": "d", "number": 4, "user": "Jay", "reply_to": None},
        ]
        groups = self.env["build_threads"](records, 15, set(), 0)
        self.assertEqual(len(groups), 2)
        self.assertEqual(groups[0]["root"]["id"], "a")
        self.assertEqual(groups[0]["total_replies"], 2)
        self.assertEqual([x["id"] for x in groups[0]["replies"]], ["b","c"])


if __name__ == "__main__":
    unittest.main()
