"""A separate real browser popup window, not an iframe notification card."""
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
const assert = require('assert'), vm = require('vm'), fs = require('fs');
class TextNode {constructor(t) {this.textContent = String(t);}}
class Element {
  constructor(tag='span') {
    this.tag=tag;this.nodes=[];this.events={};this.style={};
    this.isConnected=true;this.value='';
  }
  appendChild(x) {this.nodes.push(x);return x;}
  replaceChildren(...x) {this.nodes=x;}
  get textContent() {return this.nodes.map(x=>x.textContent).join('');}
  set textContent(s) {this.nodes=[new TextNode(s)];}
  addEventListener(name,fn) {this.events[name]=fn;}
  setAttribute(k,v) {this[k]=v;}
  focus(){}
  setSelectionRange(){}
}
const elements = {};
const document = {
  getElementById:id=>elements[id]||(elements[id]=new Element()),
  createElement:tag=>new Element(tag),
  createTextNode:t=>new TextNode(t)
};
const makeMsg=(number,user,text,privateMessage=false)=>({
  id:'m'+number,number,user,text,private:privateMessage,
  time:'12:30:00',reactions:{},my_reactions:[]
});
let state = {
  username:'Alex',my_verified:false,running:true,popup:null,
  messages:[makeMsg(100,'Emma','old history')],threads:[]
};
let polling, opened=[], allowWindows=true, timers=new Map(), nextTimer=1;
let backendSends=[];
const setTimer=(fn,ms)=>{let id=nextTimer++;timers.set(id,{fn,ms});return id;};
const clearTimer=(id)=>timers.delete(id);
const browser={
  screen:{availLeft:0,availTop:0,availWidth:1280,availHeight:800},
  open(url,target,features){
    if(!allowWindows) return null;
    const fields = {};
    const doc={
      open(){},write(html){this.html=html;},close(){},
      getElementById:id=>fields[id]||(fields[id]=new Element())
    };
    const win={document:doc,closed:false,focused:0,focus(){this.focused++;},
      close(){this.closed=true;}};
    opened.push({url,target,features,win,fields});
    return win;
  }
};
const google={colab:{kernel:{invokeFunction:async(name,args)=>{
  if(name.endsWith('.state')) return {data:{'application/json':state}};
  if(name.endsWith('.send')) {
    backendSends.push(args[0]);
    return {data:{'application/json':state}};
  }
  throw Error('Unexpected Colab callback '+name);
}}}};
vm.runInNewContext(fs.readFileSync(0,'utf8'),{
  document,window:browser,google,console,
  setTimeout:setTimer,clearTimeout:clearTimer,
  setInterval:fn=>{polling=fn;return 1;},clearInterval:()=>{},
  STATE_CALLBACK_JSON:'messaging_app.fake.state',
  SEND_CALLBACK_JSON:'messaging_app.fake.send',
  REFRESHMS:1000,URL:{revokeObjectURL(){}},Blob:class{}
});
const flush=()=>new Promise(r=>setTimeout(r,15));
async function poll(){polling();await flush();}
function headerControl(label) {
  const area=elements['messaging-display-SESSION_ID'];
  const header=area.nodes.find(node=>node.tag==='span' &&
    node.textContent.includes('Connected as'));
  if(!header) throw Error('Missing chat header');
  return header.nodes.find(x=>x.tag==='span' &&
    x.textContent.includes(label));
}
async function run(){
  await flush();
  assert.strictEqual(opened.length,0,'no auto popup from old history');
  assert(!elements['messaging-toast-SESSION_ID'],'no in-chat card');

  const enable=headerControl('Desktop popups OFF');
  assert(enable && enable.events.click);
  enable.events.click();
  assert.strictEqual(opened.length,1,'opt-in click opens a real window');
  let p=opened[0];
  assert(p.features.includes('popup=yes'));
  assert(p.features.includes('width=330'));
  assert(p.features.includes('height=56'));
  assert(p.features.includes('left=938'),p.features);
  assert(p.features.includes('top=732'),p.features);
  assert(p.doc===undefined);
  assert(p.win.document.html.includes('Pletzel Chat'));
  assert(p.win.document.html.includes('@keyframes expire'));
  assert(p.win.document.html.includes('white-space:nowrap;overflow:hidden;text-overflow:ellipsis'));
  assert(p.win.document.html.includes('height:3px'));
  assert(p.win.document.html.includes('.card{position:fixed;inset:0'),
    'notification card must fill viewport, not the requested window height');
  assert(p.win.document.html.includes('.track{position:fixed;bottom:0'),
    'blue timer must attach to actual viewport bottom');
  assert(p.win.document.html.includes('min-height:0;'),
    'min-height must not create empty region below timer');
  assert(p.win.document.html.includes('display:flex;align-items:center;background:#fff'));
  assert(p.win.document.html.includes('border-left:3px solid #1a73e8'));
  assert(!p.win.document.html.includes('height:53px'),
    'old fixed strip caused blank space in tall browser popups');
  assert(!p.win.document.html.includes('-webkit-line-clamp:2'));
  assert(p.fields.sender.textContent.includes('Preview'));
  assert.strictEqual(timers.size,1);
  assert.strictEqual([...timers.values()][0].ms,6500);
  assert(headerControl('Desktop popups ON'));

  // Auto-close THE ACTUAL SEPARATE WINDOW after the timer.
  const firstTimer=[...timers.values()][0];
  firstTimer.fn();
  assert.strictEqual(p.win.closed,true);
  assert.strictEqual(timers.size,0);

  state.messages.push(makeMsg(101,'Sasha','Hello there!'));
  await poll();
  assert.strictEqual(opened.length,2,'incoming other user starts desktop popup');
  p=opened[1];
  assert(p.fields.sender.textContent.includes('Sasha'));
  assert(p.fields.preview.textContent.includes('Hello there!'));
  assert.strictEqual(timers.size,1);

  await poll();
  assert.strictEqual(opened.length,2,'duplicate poll cannot reopen');
  state.messages.push(makeMsg(102,'Alex','mine'));
  state.messages.push(makeMsg(103,'System','private',true));
  await poll();
  assert.strictEqual(opened.length,2,'own and private messages ignored');

  // New incoming message while window open: update, no additional window.
  state.messages.push(makeMsg(104,'Emma','  New\n  hi   friend '));
  await poll();
  assert.strictEqual(opened.length,2);
  assert.strictEqual(p.fields.preview.textContent,'New hi friend');
  assert(p.fields.sender.textContent.includes('Emma'));
  assert.strictEqual(timers.size,1);

  // New batch summarized in a single popup.
  state.messages.push(makeMsg(105,'Sasha','one'));
  state.messages.push(makeMsg(106,'Emma','two'));
  await poll();
  assert.strictEqual(opened.length,2);
  assert(p.fields.sender.textContent.includes('2 new messages'));
  assert(p.fields.preview.textContent.includes('Emma: two'));

  state.messages.push(makeMsg(107,'Emma','<img src=x onerror=alert(1)>'));
  await poll();
  assert(p.fields.preview.textContent.includes('<img'));
  assert.strictEqual(p.fields.preview.nodes.length,1,'escape via textContent');
  assert.strictEqual(backendSends.length,0);

  // X button closes this actual popup without disabling later alerts.
  p.fields.close.events.click();
  assert(p.win.closed);
  assert.strictEqual(timers.size,0);

  // /testpopup is local-only and creates an actual popup on its own click.
  const input=elements['messaging-input-SESSION_ID'];
  const form=elements['messaging-form-SESSION_ID'];
  input.value='/testpopup';
  form.events.submit({preventDefault(){}});
  assert.strictEqual(input.value,'');
  assert.strictEqual(opened.length,3);
  assert.strictEqual(backendSends.length,0);
  opened[2].fields.close.events.click();

  // If policy blocks automatic windows, disable future attempts and show
  // BLOCKED in the chat header rather than silently using an in-page toast.
  allowWindows=false;
  state.messages.push(makeMsg(108,'Sasha','new blocked message'));
  await poll();
  assert.strictEqual(opened.length,3);
  assert(headerControl('Desktop popups BLOCKED'));
  state.messages.push(makeMsg(109,'Emma','next blocked message'));
  await poll();
  assert.strictEqual(opened.length,3);
  allowWindows=true;
  const retry=headerControl('Desktop popups BLOCKED');
  retry.events.click();
  assert.strictEqual(opened.length,4,'retry must be a real user click');
  assert(headerControl('Desktop popups ON'));

  state.running=false;
  await poll();
  assert.strictEqual(opened[3].win.closed,true);
  assert.strictEqual(timers.size,0);
  console.log('Desktop popup: real window, position, timer, X, privacy, opt-in, policy-block: PASS');
}
run().catch(e=>{console.error(e);process.exitCode=1;});
"""

class DesktopPopupTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js required")
    def test_real_popup_window_behavior_and_policy_failure(self):
        run = subprocess.run(["node","-e",NODE_TEST],input=JAVASCRIPT,
                             text=True,capture_output=True,timeout=20)
        self.assertEqual(run.returncode,0,run.stdout+"\n"+run.stderr)

    def test_no_built_in_browser_notifications_or_embedded_toast(self):
        self.assertIn("CLIENT_VERSION = '5.53'",SOURCE.read_text(encoding="utf-8"))
        self.assertNotIn('messaging-toast-SESSION_ID',HTML)
        self.assertIn("popup=yes,width=",JAVASCRIPT)
        self.assertIn("window.open('', 'pletzel-desktop-SESSION_ID'",JAVASCRIPT)
        self.assertIn("setTimeout(closeDesktopPopup, DESKTOP_POPUP_MS)",JAVASCRIPT)
        self.assertNotIn("Notification.requestPermission",JAVASCRIPT)
        self.assertNotIn("new Notification(",JAVASCRIPT)
        self.assertNotIn("requestPermission()",JAVASCRIPT)


if __name__=="__main__":
    unittest.main()
