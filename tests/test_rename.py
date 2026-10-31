"""Verify /rename persists in four-field Drive JSON and Verified follows."""
import ast
import json
import re
import tempfile
import threading
import unittest
from pathlib import Path

from test_plain_messages import setup, TREE

EXTRA=[node for node in TREE.body if isinstance(node,ast.FunctionDef)
       and node.name in {"claim_self_verified","submit_message","latest_analysis"}]


class RenameIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.t=tempfile.TemporaryDirectory()
        self.addCleanup(self.t.cleanup)
        self.path=Path(self.t.name)/"chat.json"
        self.path.write_text(json.dumps([
            {"kind":"message","time":"04:15:50",
             "user":"Guest-626","text":"original message"}
        ],indent=2)+"\n")
        self.notes=[]
        self.env=setup({
            "CHAT_FILE_PATH":str(self.path),
            "username":"Guest-626",
            "GOOGLE_ACCOUNT_USERNAME":"Guest-626",
            "identity_public":"test-local-id",
            "state_lock":threading.RLock(),
            "running":True,
            "note":self.notes.append
        })
        exec(compile(ast.Module(body=EXTRA,type_ignores=[]),
                     "<rename-commands>","exec"),self.env)
        self.env["snapshot"]=lambda:self.env["latest_analysis"]()

    def test_verified_user_renames_and_badge_moves(self):
        self.env["submit_message"]("/vf")
        state=self.env["submit_message"]("/rename Pletzel")
        self.assertEqual(self.env["username"],"Pletzel")
        self.assertIn("pletzel",state["verified_names"])
        self.assertTrue(state["messages"][0]["verified"])
        self.assertEqual(state["messages"][0]["user"],"Pletzel")
        records=json.loads(self.path.read_text())
        self.assertEqual(records[-1]["kind"],"rename")
        self.assertEqual(set(records[-1]),{"kind","time","user","name"})
        self.assertEqual(records[-1]["user"],"Guest-626")
        self.assertEqual(records[-1]["name"],"Pletzel")
        self.assertEqual(set(records[0]),{"kind","time","user","text"})
        self.assertEqual([e["kind"] for e in records],["message","title","rename"])
        self.assertNotIn("Could not save your new name. Please try again.",
                         self.notes)

    def test_restart_restores_renamed_username(self):
        self.env["submit_message"]("/vf")
        self.env["submit_message"]("/rename Pletzel")
        other=setup({
            "CHAT_FILE_PATH":str(self.path),
            "username":"Guest-626",
            "GOOGLE_ACCOUNT_USERNAME":"Guest-626",
            "identity_public":"test-local-id",
        })
        latest=next(x for x in TREE.body if isinstance(x,ast.FunctionDef)
                    and x.name=="latest_analysis")
        exec(compile(ast.Module(body=[latest],type_ignores=[]),
                     "<restart>","exec"),other)
        state=other["latest_analysis"]()
        self.assertEqual(other["username"],"Pletzel")
        self.assertTrue(state["messages"][0]["verified"])

    def test_multiple_renames_and_uvf(self):
        self.env["submit_message"]("/vf")
        self.env["submit_message"]("/rename Pletzel")
        self.env["submit_message"]("/rename NewName")
        state=self.env["latest_analysis"]()
        self.assertEqual(self.env["username"],"NewName")
        self.assertIn("newname",state["verified_names"])
        self.assertNotIn("pletzel",state["verified_names"])
        state=self.env["submit_message"]("/uvf NewName")
        self.assertNotIn("newname",state["verified_names"])
        state=self.env["submit_message"]("/vf")
        self.assertIn("newname",state["verified_names"])

    def test_rejects_rename_without_vf(self):
        result=self.env["submit_message"]("/rename Pletzel")
        self.assertEqual(len(json.loads(self.path.read_text())),1)
        self.assertEqual(result["messages"][0]["user"],"Guest-626")
        self.assertTrue(any("Rename is not available" in m for m in self.notes))

    def test_reactions_work_with_simple_messages_and_renames(self):
        self.env["submit_message"]("/vf")
        self.env["submit_message"]("/react 1 ❤️")
        state=self.env["latest_analysis"]()
        self.assertEqual(state["messages"][0]["reactions"],{"❤️":1})
        self.assertIn("❤️",state["messages"][0]["my_reactions"])
        self.env["submit_message"]("/rename Pletzel")
        state=self.env["latest_analysis"]()
        self.assertEqual(state["messages"][0]["reactions"],{"❤️":1})
        self.assertIn("❤️",state["messages"][0]["my_reactions"])
        self.env["submit_message"]("/unreact 1 ❤️")
        state=self.env["latest_analysis"]()
        self.assertFalse(state["messages"][0]["reactions"])
        self.assertNotIn("❤️",state["messages"][0]["my_reactions"])
        records=json.loads(self.path.read_text())
        self.assertEqual([r["kind"] for r in records],
                         ["message","title","reaction","rename","reaction"])
        self.assertEqual(set(records[2]),
                         {"kind","time","user","target","emoji","active"})

    def test_invalid_name_does_not_break_chat(self):
        self.env["submit_message"]("/vf")
        self.env["submit_message"]("/rename Owner")
        self.assertEqual(len(json.loads(self.path.read_text())),2)
        self.assertEqual(self.env["username"],"Guest-626")
        self.assertTrue(self.env["is_plain_message"](
            {"kind":"message","time":"04:15:50",
             "user":"Guest-626","text":"still works"}))


if __name__=="__main__":
    unittest.main()
