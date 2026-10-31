"""Long-result commands should use private popup windows, not chat entries."""
import ast
import json
from pathlib import Path
import shutil
import subprocess
import threading
import unittest
import uuid

from test_plain_messages import setup, SOURCE, TREE

COMMANDS = {"submit_message", "popup"}
NODES = [node for node in TREE.body if isinstance(node, ast.FunctionDef)
         and node.name in COMMANDS]
CONSTANT_NAMES = {"HELP_TEXT", "RULES"}
CONSTANTS = [
    node for node in TREE.body if isinstance(node, ast.Assign)
    and any(isinstance(t, ast.Name) and t.id in CONSTANT_NAMES
            for t in node.targets)
]
HTML = next(ast.literal_eval(n.value) for n in TREE.body
            if isinstance(n, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "CHAT_HTML"
                for t in n.targets))
JAVASCRIPT = HTML.split("<script>", 1)[1].split("</script>", 1)[0]

class PopupCommandTests(unittest.TestCase):
    def setUp(self):
        self.notices = []
        self.env = setup({
            "username": "Pletzel",
            "identity_public": "private-test-key",
            "running": True,
            "state_lock": threading.RLock(),
            "next_popup": None,
            "muted_names": {"muted-one", "muted-two"},
            "CLIENT_VERSION": "5.37",
            "VISIBLE_MESSAGES": 15,
            "visible_count": 15,
            "clear_through": 0,
            "is_mentioned": lambda text, name: "@" + name in text,
            "search_messages": lambda query, rows: rows[-3:],
            "short_id": lambda _: "test-identity",
        })
        self.env["latest_analysis"] = lambda: self.env["analyze"]([
            {"kind":"message", "time":"01:00:00",
             "user":"Pletzel","text":"Hello @Pletzel"},
            {"kind":"message", "time":"01:00:01",
             "user":"Emma","text":"Search target example"},
            {"kind":"title", "time":"01:00:02",
             "user":"Emma","title":"Verified"},
        ])
        self.env["note"] = self.notices.append
        self.env["snapshot"] = lambda: {
            "popup": self.env["next_popup"],
            "notices": list(self.notices),
        }
        exec(compile(ast.Module(body=CONSTANTS + NODES,
                                type_ignores=[]), "<long-command-popups>", "exec"),
             self.env)

    def test_long_commands_all_open_private_popups(self):
        commands = (
            "/help", "/hellp", "/rules", "/version", "/whoami",
            "/id", "/users", "/verified", "/mentions",
            "/search", "/search homework", "/find from:Emma",
            "/muted",
        )
        ids = set()
        for command in commands:
            with self.subTest(command=command):
                result = self.env["submit_message"](command)
                popup = result["popup"]
                self.assertIsInstance(popup, dict)
                self.assertTrue(popup["title"])
                self.assertTrue(popup["text"])
                self.assertNotIn(popup["id"], ids)
                ids.add(popup["id"])
                self.assertEqual(self.notices, [])
        self.assertEqual(len(ids), len(commands))

    def test_user_listing_and_search_contain_results_not_chat_notes(self):
        users = self.env["submit_message"]("/users")["popup"]
        self.assertIn("@Pletzel", users["text"])
        self.assertIn("@Emma", users["text"])
        self.assertIn("☑", users["text"])
        self.assertEqual(self.notices, [])
        self.assertIn("Search target",
                      self.env["submit_message"]("/search target")["popup"]["text"])
        self.assertEqual(self.notices, [])

    def test_short_command_remains_inline(self):
        # /stats is intentionally a short confirmation, not a popup.
        state = self.env["submit_message"]("/stats")
        self.assertIsNone(state["popup"])
        self.assertTrue(self.notices)
        self.assertIn("messages", self.notices[-1])

    def test_popup_is_private_and_never_calls_drive_writer(self):
        self.env["write_event"] = lambda _: self.fail(
            "Command results should never write to shared chat.json")
        self.env["submit_message"]("/users")
        self.env["submit_message"]("/search homework")
        self.env["submit_message"]("/rules")
        self.assertEqual(self.notices, [])

    def test_server_help_text_mentions_popup_commands(self):
        self.assertIn("/users", self.env["HELP_TEXT"])
        self.assertIn("separate window", self.env["HELP_TEXT"])

JS_TEST = r"""
const assert = require('assert');
const vm = require('vm');
class Element {
 constructor() { this.style = {}; this.events = {}; this.value=''; this.nodes=[]; this.isConnected=true; }
 appendChild(child){ this.nodes.push(child); return child; }
 replaceChildren(...nodes){ this.nodes=nodes; }
 get textContent(){ return this.nodes.map(x=>x.textContent||'').join(''); }
 set textContent(value){this.nodes=[{textContent:String(value)}];}
 setAttribute(k,v){this[k]=v;}
 addEventListener(k,fn){this.events[k]=fn;}
 focus(){}
 setSelectionRange(){}
}
let elems={};
let pendingData=[];
let popups=[];
const document = {
 getElementById:id=>elems[id]||(elems[id]=new Element()),
 createElement:()=>new Element(),
 createTextNode:value=>({textContent:String(value)}),
};
const state={username:"Pletzel",my_verified:false,running:true,popup:null,
 messages:[],threads:[]};
const google={colab:{kernel:{invokeFunction:async (name,args)=>{
 if(name.endsWith('.state')) return {data:{'application/json':state}};
 if(name.endsWith('.send')) {
   const [command]=args;
   if (['/users','/help','/search words'].includes(command)) {
      state.popup={id:String(++sequence),title:command,text:"Content for "+command};
   }
   sent.push(command);
   return {data:{'application/json':state}};
 }
}}}};
let sent=[],sequence=0;
const window={open:()=>{
 const doc={
  fields:{heading:{textContent:''},commands:{textContent:''}},
  open(){},write(){},close(){},
  getElementById(id){return this.fields[id];}
 };
 const win={closed:false,document:doc,focus(){}};
 popups.push(win);
 return win;
}};
vm.runInNewContext(require('fs').readFileSync(0,'utf8'),{
 document,window,google,console, setTimeout, setInterval:()=>0,
 clearInterval:()=>{}, URL:{createObjectURL:()=>'',revokeObjectURL(){}},
 Blob:class {}, STATE_CALLBACK_JSON:'messaging_app.test.state',
 SEND_CALLBACK_JSON:'messaging_app.test.send', REFRESHMS:1000
});
setTimeout(async()=>{
 const input=elems['messaging-input-SESSION_ID'];
 const form=elems['messaging-form-SESSION_ID'];
 function send(value){input.value=value;form.events.submit({preventDefault(){}});}
 send('/users');
 send('/help');
 send('/search words');
 send('/stats');
 assert.equal(popups.length,3,'Three long commands should open three popup windows synchronously');
 assert.equal(popups[0].document.fields.heading.textContent,'Loading command output...');
 await new Promise(resolve=>setTimeout(resolve,3600));
 assert.deepStrictEqual(sent,['/users','/help','/search words','/stats']);
 assert.equal(popups[0].document.fields.commands.textContent,'Content for /users');
 assert.equal(popups[1].document.fields.commands.textContent,'Content for /help');
 assert.equal(popups[2].document.fields.commands.textContent,'Content for /search words');
 assert(!elems['messaging-display-SESSION_ID'].textContent.includes('Content for /users'));
 console.log('Multi-command popup queues: PASS');
},10);
"""

class BrowserPopupTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js not installed")
    def test_long_command_submit_preopens_windows_and_matches_outputs(self):
        result=subprocess.run(["node", "-e", JS_TEST],
                              input=JAVASCRIPT,text=True,
                              capture_output=True,timeout=20,check=False)
        self.assertEqual(result.returncode,0,
                         result.stdout+"\n"+result.stderr)


if __name__=="__main__":
    unittest.main()
