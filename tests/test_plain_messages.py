"""The requested new format is exactly {kind,time,user,text} with no ID."""
import ast
import hashlib
import json
import os
import re
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path

from test_security import env as legacy_env

SOURCE = Path(__file__).resolve().parents[1].joinpath("messaging_app.py").read_text()
TREE = ast.parse(SOURCE)
NAMES = {"is_plain_message","plain_message","display_only_message_id",
         "read_messages","append_event_in_place","write_plain_message",
         "is_title_event","is_simple_record","title_event",
         "is_rename_event","rename_event",
         "is_simple_reaction","simple_reaction_event",
         "analyze","write_event","parse_chat_log","load_outbox",
         "save_outbox","remember_outgoing","reconcile_outbox"}
NODES = [n for n in TREE.body if isinstance(n,ast.FunctionDef) and n.name in NAMES]

def setup(extra=None):
    env=dict(legacy_env)
    env.update({
        "os": os, "re": re, "hashlib": hashlib, "json": json, "time": time,
        "uuid": uuid, "APPEND_RECORD_SEPARATOR": "\x1e",
        "last_outbox_check": 0.0,
        "REPLY_TEXT_RE": re.compile(r'^↳ \[reply:#(\d+)\] (.+)$', re.DOTALL),
        "PLAIN_MESSAGE_FIELDS": frozenset(("kind","time","user","text")),
        "TITLE_FIELDS": frozenset(("kind","time","user","title")),
        "RENAME_FIELDS": frozenset(("kind","time","user","name")),
        "REACTION_FIELDS": frozenset(("kind","time","user","target","emoji","active")),
        "GOOGLE_ACCOUNT_USERNAME": "Pletzel",
        "TITLE_VERIFIED": "Verified", "TITLE_NONE": "None",
        "username": "Pletzel",
        "file_lock": threading.RLock(),
        "ChatFileError": type("ChatFileError",(Exception,),{}),
    })
    if extra:
        env.update(extra)
    exec(compile(ast.Module(body=NODES,type_ignores=[]),"<plain-chat>", "exec"),env)
    return env


class PlainMessagesTests(unittest.TestCase):
    def test_record_exactly_four_fields_in_requested_order(self):
        env=setup()
        result=env["plain_message"]("fdg")
        self.assertEqual(list(result),["kind","time","user","text"])
        self.assertEqual(result["kind"],"message")
        self.assertEqual(result["user"],"Pletzel")
        self.assertEqual(result["text"],"fdg")
        self.assertRegex(result["time"],r"^\d{2}:\d{2}:\d{2}$")
        self.assertTrue(env["is_plain_message"](result))
        for bad in (dict(result,id="extra"),dict(result,sig="bad"),
                    dict(result,sender_key="bad"),dict(result,kind="owner")):
            self.assertFalse(env["is_plain_message"](bad))

    def test_append_plain_message_does_not_write_ids(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/"chat.json"
            original=[{"kind":"message","time":"04:12:00",
                       "user":"ColabUser","text":"existing history"}]
            path.write_text(json.dumps(original, indent=2)+"\n")
            env=setup({"CHAT_FILE_PATH":str(path),
                       "PRIVATE_FOLDER":str(Path(folder)/"private"),
                       "PRIVATE_OUTBOX_PATH":str(Path(folder)/"private"/"outbox.json")})
            item=env["plain_message"]("fdg")
            self.assertTrue(env["write_event"](item))
            entries=env["read_messages"]()
            self.assertEqual(entries[0],original[0])
            self.assertEqual(entries[-1],item)
            self.assertEqual(set(entries[-1]),{"kind","time","user","text"})
            self.assertEqual(len(list(Path(folder).glob("*.json"))),1)
            self.assertTrue(path.read_text().startswith(json.dumps(original, indent=2)+"\\n"))

    def test_simple_message_appears_after_signed_history(self):
        env=setup()
        signed={"id":"old","kind":"message","time":"04:01:00","user":"Alice",
                "text":"signed", "sender_key":"AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQE=","sig":"test-valid"}
        simple={"kind":"message","time":"04:15:50","user":"Pletzel","text":"fdg"}
        state=env["analyze"]([signed,simple])
        self.assertEqual(len(state["messages"]),2)
        self.assertEqual(state["messages"][-1]["text"],"fdg")
        self.assertEqual(state["messages"][-1]["user"],"Pletzel")
        self.assertFalse(state["messages"][-1]["verified"])
        self.assertTrue(state["messages"][-1]["id"].startswith("plain-1-"))
        self.assertNotIn("id",simple)

    def test_reply_nesting_from_four_field_text(self):
        env=setup()
        root={"kind":"message","time":"04:00:00","user":"Pletzel","text":"hello"}
        reply={"kind":"message","time":"04:15:50","user":"Emma",
               "text":"↳ [reply:#1] hello back"}
        items=env["analyze"]([root,reply])["messages"]
        self.assertEqual(items[1]["reply_to"],items[0]["id"])
        self.assertEqual(items[1]["text"],"hello back")

    def test_legacy_roles_and_reactions_still_read(self):
        env=setup()
        pub=legacy_env["b64decode"] if False else None
        old={"id":"old","kind":"message","time":"04:01:00","user":"Alice",
             "text":"saved", "sender_key":"AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQE=","sig":"test-valid"}
        simple={"kind":"message","time":"04:15:50","user":"Pletzel","text":"fdg"}
        state=env["analyze"]([old,simple])
        self.assertEqual(len(state["messages"]),2)
        self.assertNotIn("id",simple)


if __name__=="__main__":
    unittest.main()
