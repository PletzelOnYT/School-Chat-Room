"""Colab-styled in-chat toast previews instead of browser permission popups."""
import ast
import shutil
import subprocess
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "app" / "messaging_app.py"
TREE = ast.parse(SOURCE.read_text(encoding="utf-8"))
HTML = next(ast.literal_eval(n.value) for n in TREE.body
            if isinstance(n, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "CHAT_HTML"
                for t in n.targets))
JAVASCRIPT = HTML.split("<script>", 1)[1].split("</script>", 1)[0]

NODE_TEST = r"""
const assert = require('assert');
const vm = require('vm');
const fs = require('fs');

class TextNode {
  constructor(text) { this.textContent = String(text); }
}
class Element {
  constructor(tag='div') {
    this.tag = tag; this.nodes = []; this.events = {};
    this.style = {}; this.isConnected = true; this.value = '';
  }
  appendChild(node) { this.nodes.push(node); return node; }
  replaceChildren(...nodes) { this.nodes = nodes; }
  get textContent() { return this.nodes.map(n=>n.textContent).join(''); }
  set textContent(t) { this.nodes = [new TextNode(t)]; }
  addEventListener(type, callback) { this.events[type] = callback; }
  setAttribute(name,value) { this[name] = value; }
  focus() {}
  setSelectionRange() {}
}
const elements = {};
const document = {
  getElementById: id => elements[id] || (elements[id] = new Element()),
  createElement: tag => new Element(tag),
  createTextNode: t => new TextNode(t)
};
const shared = (number,user,text) => ({
  id:'m'+number,number,user,text,time:'12:34:56',
  private:false,reactions:{},my_reactions:[]
});
const priv = (text) => ({
  id:'local',number:null,user:'System',text,time:'12:34:57',private:true
});
let state = {
  username:'Alex',my_verified:false,running:true,popup:null,
  messages:[shared(100,'Emma','old message')],threads:[]
};
let poll;
let nextTimerId=0;
const timers = new Map();
const fakeTimeout = (callback,ms) => {
  const id=++nextTimerId;
  timers.set(id,{callback,ms});
  return id;
};
const fakeClearTimeout = id=>timers.delete(id);
const google = {colab:{kernel:{invokeFunction: async(name,args)=>{
  if(name.endsWith('.state')) return {data:{'application/json':state}};
  if(name.endsWith('.send')) return {data:{'application/json':state}};
  throw Error('unexpected callback');
}}}};
const sandbox = {
  document,google,console,
  setTimeout:fakeTimeout,clearTimeout:fakeClearTimeout,
  setInterval:fn=>{poll=fn;return 1},clearInterval:()=>{},
  window:{open(){throw Error('No window.open for toast messages');}},
  URL:{createObjectURL:()=>'',revokeObjectURL(){}},Blob:class {},
  REFRESHMS:1000,
  STATE_CALLBACK_JSON:'messaging_app.test.state',
  SEND_CALLBACK_JSON:'messaging_app.test.send'
};
const js = fs.readFileSync(0,'utf8');
vm.runInNewContext(js,sandbox);
const toast = elements['messaging-toast-SESSION_ID'];
const title = elements['messaging-toast-title-SESSION_ID'];
const preview = elements['messaging-toast-preview-SESSION_ID'];
const progress = elements['messaging-toast-timer-SESSION_ID'];
const close = elements['messaging-toast-close-SESSION_ID'];
async function flush() {
  await new Promise(resolve=>setTimeout(resolve,12));
}
async function refresh() {
  poll();
  await flush();
}
async function run() {
  await flush();
  assert.strictEqual(toast.style.display,undefined,
    'DOM mock initial CSS is static, no JS auto-show on launch');
  assert.strictEqual(timers.size,0,'no toast for old history');
  assert(!js.includes('Notification.requestPermission'));
  assert(!js.includes('desktopAlertsWindow'));
  assert(!js.includes('new API('));

  state.messages.push(shared(101,'Sasha','Hello from class!'));
  await refresh();
  assert.strictEqual(toast.style.display,'block');
  assert(title.textContent.includes('Sasha'),title.textContent);
  assert(preview.textContent.includes('Hello from class!'));
  assert(progress.style.animation.includes('6500ms'));
  assert.strictEqual(timers.size,1);
  let firstTimer=[...timers.keys()][0];

  await refresh();
  assert.strictEqual(timers.size,1,'no duplicate on unchanged poll');
  assert(timers.has(firstTimer),'unchanged poll cannot restart timer');

  // Own messages and local System entries do not produce alerts.
  state.messages.push(shared(102,'Alex','my sent message'),priv('Private warning'));
  await refresh();
  assert(timers.has(firstTimer),'own/system messages must not replace toast');

  // Replace existing toast when a new message appears.
  state.messages.push(shared(103,'Emma','New   message \n with   spaces!'));
  await refresh();
  assert.strictEqual(timers.size,1);
  assert(!timers.has(firstTimer),'old countdown must be canceled');
  assert(title.textContent.includes('Emma'));
  assert.strictEqual(preview.textContent,'New message with spaces!');
  assert(!preview.textContent.includes('System'));

  // Multiple fresh messages: single toast with latest author's preview.
  state.messages.push(shared(104,'Sasha','one'),shared(105,'Emma','two'));
  await refresh();
  assert(title.textContent.includes('2 new messages'));
  assert(preview.textContent.includes('Emma: two'));
  assert.strictEqual(timers.size,1);

  // Untrusted chat markup must only appear as text, not HTML.
  state.messages.push(shared(106,'Emma','<img src=x onerror=alert(1)>'));
  await refresh();
  assert(preview.textContent.includes('<img'));
  assert.strictEqual(preview.nodes.length,1);

  // Auto-close after progress line finishes.
  assert.strictEqual(timers.size,1);
  const current=[...timers.values()][0];
  assert.strictEqual(current.ms,6500);
  current.callback();
  assert.strictEqual(toast.style.display,'none');
  assert.strictEqual(timers.size,0);

  // Click X immediately closes the next toast.
  state.messages.push(shared(107,'Sasha','bye'));
  await refresh();
  assert.strictEqual(toast.style.display,'block');
  close.events.click();
  assert.strictEqual(toast.style.display,'none');
  assert.strictEqual(timers.size,0);

  state.running=false;
  await refresh();
  assert.strictEqual(toast.style.display,'none');
  console.log('In-chat toasts: delivery, privacy, preview, timer, close, XSS: PASS');
}
run().catch(err=>{ console.error(err);process.exitCode=1; });
"""

class ChatToastTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js not installed")
    def test_new_messages_show_deduplicated_self_closing_toasts(self):
        result = subprocess.run(
            ["node","-e",NODE_TEST],input=JAVASCRIPT,
            capture_output=True,text=True,timeout=20,check=False)
        self.assertEqual(result.returncode,0,result.stdout+"\n"+result.stderr)

    def test_colab_style_and_no_permission_prompt(self):
        source = SOURCE.read_text(encoding="utf-8")
        self.assertIn("CLIENT_VERSION = '5.42'",source)
        self.assertIn('position:fixed;right:12px;bottom:12px',HTML)
        self.assertIn('background:#ffffff;color:#202124',HTML)
        self.assertIn('background:#1a73e8',HTML)
        self.assertIn('width:min(330px',HTML)
        self.assertIn('aria-label="Dismiss chat notification"',HTML)
        self.assertNotIn('notificationAccess',JAVASCRIPT)
        self.assertNotIn('desktopAlertsWindow',JAVASCRIPT)
        self.assertNotIn('requestPermission',JAVASCRIPT)
        self.assertNotIn('new Notification(',JAVASCRIPT)


if __name__ == "__main__":
    unittest.main()
