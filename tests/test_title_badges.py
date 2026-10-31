"""Simple Verified title records remain compatible with four-field messages."""
import ast
import json
import tempfile
import threading
import unittest
from pathlib import Path

from test_plain_messages import setup, SOURCE, TREE

EXTRA_NAMES={"claim_self_verified","submit_message"}
EXTRA=[node for node in TREE.body if isinstance(node,ast.FunctionDef)
       and node.name in EXTRA_NAMES]


class TitleBadgeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/"chat.json"
        self.start=[
            {"kind":"message","time":"04:15:50","user":"Pletzel","text":"Hello"},
            {"kind":"message","time":"04:15:51","user":"Emma","text":"Hi"},
        ]
        self.path.write_text(json.dumps(self.start,indent=2)+"\n")
        self.env=setup({"CHAT_FILE_PATH":str(self.path),"username":"Pletzel",
                        "identity_public":"test-local-identity"})
        self.env.update({
            "state_lock":threading.RLock(),
            "running":True,
            "latest_analysis":lambda:self.env["analyze"](self.env["read_messages"]()),
            "note":lambda text:None,
            "snapshot":lambda:self.env["latest_analysis"](),
        })
        exec(compile(ast.Module(body=EXTRA,type_ignores=[]),"<title-commands>","exec"),self.env)

    def test_verified_self_claim_is_saved_as_simple_title(self):
        state=self.env["submit_message"]("/vf")
        self.assertIn("pletzel",state["verified_names"])
        self.assertTrue(state["messages"][0]["verified"])
        self.assertFalse(state["messages"][1]["verified"])
        self.assertEqual(self.env["read_messages"]()[-1],
            {"kind":"title","time":self.env["read_messages"]()[-1]["time"],
             "user":"Pletzel","title":"Verified"})
        self.assertTrue(self.env["is_title_event"](self.env["read_messages"]()[-1]))
        self.assertEqual(len(state["messages"]),2)
        self.assertEqual(set(state["messages"][0]["user"] for _ in [0]),{"Pletzel"})

    def test_unverify_any_name_then_verify_again(self):
        self.env["submit_message"]("/vf")
        state=self.env["submit_message"]("/uvf Pletzel")
        self.assertNotIn("pletzel",state["verified_names"])
        self.assertFalse(state["messages"][0]["verified"])
        self.assertEqual(self.env["read_messages"]()[-1]["title"],"None")
        state=self.env["submit_message"]("/vf")
        self.assertTrue(state["messages"][0]["verified"])
        self.assertEqual([e["title"] for e in self.env["read_messages"]()
                          if e.get("kind")=="title"],
                         ["Verified","None","Verified"])

    def test_title_schema_is_exact_and_other_roles_rejected(self):
        valid=self.env["title_event"]("Pletzel",True)
        self.assertTrue(self.env["is_title_event"](valid))
        for extra in ({"id":"123"},{"sender_key":"fake"},{"sig":"invalid"},
                      {"text":"hidden"}):
            self.assertFalse(self.env["is_title_event"]({**valid,**extra}))
        self.assertFalse(self.env["is_title_event"]({**valid,"title":"Owner"}))
        self.assertFalse(self.env["is_title_event"]({**valid,"title":"Moderator"}))

    def test_names_are_case_insensitive(self):
        self.env["write_event"]({"kind":"title","time":"04:20:00",
                                  "user":"pLeTzEl","title":"Verified"})
        state=self.env["latest_analysis"]()
        self.assertTrue(state["messages"][0]["verified"])
        self.assertTrue(self.env["claim_self_verified"]())
        self.assertEqual(len(self.env["read_messages"]()),3)


if __name__=="__main__":
    unittest.main()
