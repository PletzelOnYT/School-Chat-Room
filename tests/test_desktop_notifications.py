"""Test real desktop-notification opt-in without Chrome extensions."""
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

NODE = r"""
const vm = require('vm');
const assert = require('assert');
const fs = require('fs');
class TextNode { constructor(t){this.textContent=String(t);} }
class Element {
  constructor(tag='span'){
    this.tag=tag; this.nodes=[]; this.events={}; this.style={};
    this.isConnected=true; this.value=''; this.checked=false;
  }
  appendChild(x){this.nodes.push(x);return x;}
  replaceChildren(...x){this.nodes=x;}
  get textContent(){return this.nodes.map(x=>x.textContent||'').join('');}
  set textContent(v){this.nodes=[new TextNode(v)];}
  addEventListener(name,cb){this.events[name]=cb;}
  setAttribute(k,v){this[k]=v;}
  focus(){}
  setSelectionRange(){}
}
const e={}, docs=[];
const document={
  getElementById:id=>e[id]||(e[id]=new Element()),
  createElement:tag=>new Element(tag),
  createTextNode:txt=>new TextNode(txt)
};
let interval;
let notifications=[];
let notificationPermission='default';
function makeWindow(){
  const fields={},doc={
    open(){},write(page){this.html=page;},close(){},
    getElementById(id){return fields[id]||(fields[id]=new Element());}
  };
  docs.push(doc);
  class BrowserNotification {
    constructor(title,opts){
      if(BrowserNotification.permission!=='granted') throw Error('permission denied');
      this.title=title; this.options=opts; notifications.push(this);
    }
    close(){}
    static get permission(){return notificationPermission;}
    static async requestPermission(){
      notificationPermission=(process.argv.includes('--denied')?'denied':'granted');
      return notificationPermission;
    }
  }
  return {document:doc,closed:false,isSecureContext:true,
    Notification:BrowserNotification,focus(){}};
}
let current = {
  username:'Alex', running:true, popup:null, my_verified:false,
  messages:[],
  threads:[]
};
const msg=(id,num,user,text)=>({
  id,number:num,user,text,time:'20:54:30',private:false,reactions:{}
});
current.messages=[msg('old',100,'Emma','an old message')];
const google={colab:{kernel:{invokeFunction:async(name,args)=>{
  if(name.endsWith('.state')) return {data:{'application/json':current}};
  if(name.endsWith('.send')) return {data:{'application/json':current}};
}}}};
const browserWindow={open:makeWindow,focus(){}};
vm.runInNewContext(fs.readFileSync(0,'utf8'),{
  document,window:browserWindow,google,console,
  setTimeout,setInterval:fn=>{interval=fn;return 4},clearInterval:()=>{},
  STATE_CALLBACK_JSON:'messaging_app.test.state',
  SEND_CALLBACK_JSON:'messaging_app.test.send',
  REFRESHMS:1000,URL:{createObjectURL:()=>'',revokeObjectURL(){}},
  Blob:class{}
});
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
async function tick(){interval();await sleep(20);}
function control(){
  let area=e['messaging-display-SESSION_ID'];
  let header=area.nodes.find(n=>n.tag==='span' &&
    n.textContent.includes('Connected as'));
  assert(header,area.textContent);
  return header.nodes.find(n=>n.tag==='span' &&
    n.textContent.includes('Desktop alerts'));
}
async function test(){
  await sleep(25);
  assert(control().events.click, 'opt-in control must be clickable');
  assert.strictEqual(notifications.length,0,'no notification for existing history');
  control().events.click();
  assert(docs.length===1,'permission popup should open from click');
  const d=docs[0];
  assert(d.html.includes('Enable notifications'));
  assert(d.html.includes('This window must stay open'));
  assert(d.getElementById('allow').events.click,
    'permission should only be asked in popup on explicit click');
  await d.getElementById('allow').events.click();
  await sleep(10);
  if(process.argv.includes('--denied')){
    assert(d.getElementById('status').textContent.includes('denied'));
    current.messages.push(msg('new',101,'Emma','after denial'));
    await tick();
    assert.strictEqual(notifications.length,0);
    console.log('School/browser-denied case: PASS');
    return;
  }
  assert.strictEqual(notifications.length,1,'enable button sends one test banner');
  assert(notifications[0].options.body.includes('Test notification'));
  assert(d.getElementById('status').textContent.includes('Test sent'));
  current.messages.push(msg('new',101,'Emma','new message here'));
  await tick();
  assert.strictEqual(notifications.length,2);
  assert(notifications[1].options.body.includes('New message from Emma'));
  await tick();
  assert.strictEqual(notifications.length,2,'no duplicate alerts on poll');
  current.messages.push(msg('self',102,'Alex','my own message'));
  await tick();
  assert.strictEqual(notifications.length,2,'own messages must be ignored');
  current.messages.push(msg('other',103,'Sasha','hi'));
  current.messages.push(msg('other2',104,'Emma','hey'));
  await tick();
  assert.strictEqual(notifications.length,3,'one banner per batch');
  assert(notifications[2].options.body.includes('2 new messages'));
  const checkbox=d.getElementById('preview');
  checkbox.checked=true;
  checkbox.events.change();
  current.messages.push(msg('preview',105,'Sasha','Preview text'));
  await tick();
  assert(notifications[3].options.body.includes('Preview text'));
  current.messages.push(msg('priv',106,'System','private command output'));
  current.messages[current.messages.length-1].private=true;
  await tick();
  assert.strictEqual(notifications.length,4);
  assert(control().textContent.includes('Alerts ON'));
  // Disabling alerts must not change the chat or shared content.
  let header=e['messaging-display-SESSION_ID'].nodes.find(n=>n.tag==='span' &&
    n.textContent.includes('Connected as'));
  const disable=header.nodes.find(n=>n.tag==='span' && n.textContent.includes('Alerts ON'));
  assert(disable && disable.events.click);
  disable.events.click();
  current.messages.push(msg('stop',107,'Sasha','after stop'));
  await tick();
  assert.strictEqual(notifications.length,4,'opt-out suppresses notifications');
  console.log('Desktop opt-in, test banner, new messages, no spam, opt-out: PASS');
}
test().catch(err=>{console.error(err);process.exitCode=1;});
"""

class DesktopNotificationTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js required")
    def test_permission_granted_then_only_new_remote_messages(self):
        res=subprocess.run(["node","-e",NODE],input=JAVASCRIPT,
                            text=True,capture_output=True,timeout=20)
        self.assertEqual(res.returncode,0,res.stdout+"\n"+res.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node.js required")
    def test_browser_or_school_permission_denied(self):
        res=subprocess.run(["node","-e",NODE,"--","--denied"],input=JAVASCRIPT,
                            text=True,capture_output=True,timeout=20)
        self.assertEqual(res.returncode,0,res.stdout+"\n"+res.stderr)

    def test_notification_code_does_not_change_shared_drive_schema(self):
        source=SOURCE.read_text(encoding="utf-8")
        self.assertIn("desktopAlertsEnabled",source)
        self.assertIn("new API('Pletzel Chat'",source)
        self.assertIn("msg.private",source)
        self.assertIn("CLIENT_VERSION = '5.39'",source)


if __name__=="__main__":
    unittest.main()
