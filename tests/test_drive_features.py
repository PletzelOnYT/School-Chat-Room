"""Four-field Google Drive JSON storage, formatting, search, and threads."""
import ast
import hashlib
import json
import os
import re
import shlex
import tempfile
import threading
import time
import uuid
import unittest
from datetime import datetime
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1].joinpath("messaging_app.py").read_text()
TREE = ast.parse(SOURCE)
NAMES = {"read_messages", "write_event", "write_plain_message",
         "message_hash", "is_clock_event", "clock_event",
         "append_event_in_place", "is_plain_message",
         "is_title_event", "is_simple_record", "title_event",
         "is_rename_event", "rename_event",
         "is_simple_reaction", "simple_reaction_event",
         "search_messages", "chronological_display_messages",
         "build_threads", "parse_chat_log",
         "load_outbox", "save_outbox", "remember_outgoing", "reconcile_outbox"}
FUNCTIONS = [node for node in TREE.body
             if isinstance(node, ast.FunctionDef) and node.name in NAMES]


def storage_env(path):
    env = {
        "os": os, "re": re, "json": json, "time": time, "hashlib": hashlib,
        "uuid": uuid, "APPEND_RECORD_SEPARATOR": "\x1e",
        "last_outbox_check": 0.0,
        "shlex": shlex, "datetime": datetime,
        "PLAIN_MESSAGE_FIELDS": frozenset(("kind","time","user","text")),
        "TITLE_FIELDS": frozenset(("kind","time","user","title")),
        "RENAME_FIELDS": frozenset(("kind","time","user","name")),
        "REACTION_FIELDS": frozenset(("kind","time","user","target","emoji","active")),
        "CLOCK_FIELDS": frozenset(("kind","time","user","epoch","message_hash")),
        "is_valid_emoji": lambda emoji: isinstance(emoji,str) and any(ord(x)>=0x2600 for x in emoji),
        "TITLE_VERIFIED": "Verified", "TITLE_NONE": "None",
        "CHAT_FILE_PATH": str(path),
        "PRIVATE_FOLDER": str(path.parent / ".local_receipts"),
        "PRIVATE_OUTBOX_PATH": str(path.parent / ".local_receipts" / "outbox.json"),
        "file_lock": threading.RLock(),
        "ChatFileError": type("ChatFileError",(Exception,),{}),
        "short_id": lambda s: str(s)[:12],
    }
    exec(compile(ast.Module(body=FUNCTIONS,type_ignores=[]),
                 "<message-storage>", "exec"), env)
    return env


def entry(text="hi", user="Pletzel", clock="04:15:50"):
    return {"kind":"message","time":clock,"user":user,"text":text}


class FourFieldDriveTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/"chat.json"
        self.initial=[entry("old one","ColabUser","04:12:01"),
                      entry("old two","Pletzel","04:13:05")]
        self.path.write_text(json.dumps(self.initial,indent=2,ensure_ascii=False)+"\n")
        self.env=storage_env(self.path)

    def test_reads_pretty_history_without_anchor_or_ids(self):
        self.assertEqual(self.env["read_messages"](),self.initial)
        self.assertTrue(all(len(x)==4 and "id" not in x
                            for x in self.env["read_messages"]()))

    def test_utf8_bom_and_windows_newlines_are_supported(self):
        self.path.write_bytes(
            b"\xef\xbb\xbf"+
            (json.dumps(self.initial,indent=2)+"\n").replace("\n","\r\n").encode()
        )
        self.assertEqual(self.env["read_messages"](),self.initial)

    def test_tail_append_is_beautified_and_no_ids(self):
        new=entry("fdg")
        self.assertTrue(self.env["write_event"](new))
        records=self.env["read_messages"]()
        self.assertEqual(records,self.initial+[new])
        raw=self.path.read_text()
        self.assertTrue(raw.startswith(json.dumps(self.initial,indent=2,ensure_ascii=False)+"\n"))
        self.assertIn('\x1e{\n  "kind": "message",\n  "time": "04:15:50"',raw)
        self.assertIn('"text": "fdg"\n}',raw)
        self.assertEqual(len(list(self.path.parent.glob("chat.json"))),1)

    def test_append_never_changes_existing_bytes(self):
        baseline=self.path.read_bytes()
        self.assertTrue(self.env["write_event"](entry("message one")))
        first=self.path.read_bytes()
        self.assertTrue(first.startswith(baseline))
        self.assertTrue(self.env["write_event"](entry("message two","Emma","04:16:00")))
        second=self.path.read_bytes()
        self.assertTrue(second.startswith(first))
        self.assertEqual([x["text"] for x in self.env["read_messages"]()][-2:],
                         ["message one","message two"])

    def test_concurrent_clients_append_without_replacing_history(self):
        from concurrent.futures import ThreadPoolExecutor
        old=self.path.read_bytes()

        def send(index):
            # Different Colab sessions have separate Python locks/outboxes.
            env=storage_env(self.path)
            env["PRIVATE_FOLDER"]=str(self.path.parent/("client-"+str(index)))
            env["PRIVATE_OUTBOX_PATH"]=str(
                self.path.parent/("client-"+str(index))/"receipts.json")
            return env["write_event"](entry("unique concurrent "+str(index),
                                            "User"+str(index), "04:20:00"))

        with ThreadPoolExecutor(max_workers=8) as pool:
            submitted=list(pool.map(send,range(12)))
        self.assertTrue(all(submitted))
        self.assertTrue(self.path.read_bytes().startswith(old))
        found=[x["text"] for x in self.env["read_messages"]()]
        for i in range(12):
            self.assertEqual(found.count("unique concurrent "+str(i)),1)

    def test_private_receipt_can_reappend_lost_entry(self):
        record=entry("this event was lost")
        self.assertTrue(self.env["write_event"](record))
        receipt=self.env["load_outbox"]()
        self.assertEqual(len(receipt),1)
        # Simulate a stale Drive writer erasing the just-appended bytes.
        original=json.dumps(self.initial,indent=2,ensure_ascii=False)+"\n"
        self.path.write_text(original)
        self.env["reconcile_outbox"]()
        self.assertIn(record,self.env["read_messages"]())
        self.assertTrue(self.path.read_text().startswith(original))

    def test_reject_incomplete_append_without_overwriting(self):
        original=self.path.read_bytes()
        self.path.write_bytes(original+b"\n\x1e{")
        broken=self.path.read_bytes()
        with self.assertRaises(self.env["ChatFileError"]):
            self.env["write_event"](entry("unsafe"))
        self.assertEqual(self.path.read_bytes(),broken)

    def test_rejects_metadata_without_rewriting_file(self):
        before=self.path.read_bytes()
        signed={"kind":"role","user":"Owner","id":"fake","sig":"bad"}
        self.assertFalse(self.env["write_event"](signed))
        self.assertEqual(before,self.path.read_bytes())

    def test_rejects_legacy_record_with_extra_fields(self):
        self.path.write_text(json.dumps(self.initial+[dict(entry(),id="legacy")]))
        with self.assertRaises(self.env["ChatFileError"]):
            self.env["read_messages"]()

    def test_damaged_json_is_never_overwritten(self):
        broken=b"[{not valid json"
        self.path.write_bytes(broken)
        with self.assertRaises(self.env["ChatFileError"]):
            self.env["write_event"](entry("hello"))
        self.assertEqual(self.path.read_bytes(),broken)

    def test_search_and_nested_threads_still_work(self):
        rows=[
            {"id":"a","number":1,"user":"Alex","text":"math homework",
             "reply_to":None,"issued_at":1793491200,"sender_key":None,"reactions":{}},
            {"id":"b","number":2,"user":"Emma","text":"quiz",
             "reply_to":"a","issued_at":1793577600,"sender_key":None,
             "reactions":{"❤️":1}},
            {"id":"c","number":3,"user":"Alex","text":"sure",
             "reply_to":"b","issued_at":1793577600,"sender_key":None,"reactions":{}},
        ]
        self.assertEqual(len(self.env["search_messages"]("from:Emma has:reaction",rows)),1)
        self.assertEqual(len(self.env["search_messages"]("in:replies",rows)),2)
        groups=self.env["build_threads"](rows,15,set(),0)
        self.assertEqual(groups[0]["root"]["id"],"a")
        self.assertEqual([m["id"] for m in groups[0]["replies"]],["b","c"])


if __name__=="__main__":
    unittest.main()
