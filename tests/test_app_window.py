"""The app-style popup must mirror the existing Colab chat, not duplicate it."""
import ast
import shutil
import subprocess
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "app" / "messaging_app.py"
TREE = ast.parse(SOURCE.read_text(encoding="utf-8"))
HTML = next(ast.literal_eval(node.value) for node in TREE.body
            if isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "CHAT_HTML"
                    for t in node.targets))
SCRIPT = HTML.split("<script>", 1)[1].split("</script>", 1)[0]

NODE_TEST = r"""
const assert=require('assert'),vm=require('vm'),fs=require('fs');
class TextNode {
  constructor(s){this.textContent=String(s);}
}
class Element {
  constructor(tag='div'){
    this.tag=tag;this.nodes=[];this.events={};this.style={};
    this.value='';this.isConnected=true;this.scrollTop=0;
    this.scrollHeight=100;this.clientHeight=100;
  }
  appendChild(x){this.nodes.push(x);return x;}
  replaceChildren(...xs){
    this.nodes=xs.flatMap(x=>x.tag==='fragment'?x.nodes:[x]);
  }
  get textContent(){return this.nodes.map(x=>x.textContent||'').join('');}
  set textContent(s){this.nodes=[new TextNode(s)];}
  addEventListener(k,fn){this.events[k]=fn;}
  setAttribute(k,v){this[k]=v;}
  focus(){this.focused=true;}
  setSelectionRange(){}
  remove(){this.removed=true;}
}
const mainElements={};
const doc={
  getElementById:id=>mainElements[id]||(mainElements[id]=new Element()),
  createElement:tag=>new Element(tag),
  createTextNode:t=>new TextNode(t)
};
const makeMsg=(n,user,text)=>({
  id:'m'+n,number:n,user,text,private:false,verified:false,
  time:'18:30:00',issued_at:1793450000,reactions:{},my_reactions:[]
});
let state={
  username:'Alex',my_verified:true,running:true,popup:null,
  messages:[makeMsg(1,'Emma','Hello <b>not markup</b>')],threads:[]
};
const opened=[],sends=[];
let polling=null,allowPopups=true;
const browser={
  screen:{availLeft:0,availTop:0,availWidth:1280,availHeight:800},
  open(url,name,features){
    if(!allowPopups)return null;
    const elems={};
    const popupDoc={
      open(){},close(){},write(s){this.html=s;},
      getElementById:id=>elems[id]||(elems[id]=new Element()),
      createElement:tag=>new Element(tag),
      createTextNode:t=>new TextNode(t),
      createDocumentFragment:()=>new Element('fragment')
    };
    const win={
      document:popupDoc,closed:false,focus(){this.focused=true;},
      close(){this.closed=true;}
    };
    opened.push({win,elems,url,name,features});
    return win;
  }
};
const google={colab:{kernel:{invokeFunction:async(name,args)=>{
  if(name.endsWith('.state')) return {data:{'application/json':state}};
  if(name.endsWith('.send')) {
    sends.push(args[0]);
    return {data:{'application/json':state}};
  }
  throw Error('Unexpected callback '+name);
}}}};
vm.runInNewContext(fs.readFileSync(0,'utf8'),{
  document:doc,window:browser,google,console,
  setTimeout,clearTimeout,
  setInterval:fn=>{polling=fn;return 1;},clearInterval:()=>{},
  STATE_CALLBACK_JSON:'messaging_app.demo.state',
  SEND_CALLBACK_JSON:'messaging_app.demo.send',
  REFRESHMS:1000,URL:{revokeObjectURL(){}},Blob:class{}
});
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
function control(label){
  const area=mainElements['messaging-display-SESSION_ID'];
  const header=area.nodes.find(x=>x.tag==='span'&&
    x.textContent.includes('Connected as'));
  assert(header,'classic Colab header must still exist');
  return header.nodes.find(x=>x.textContent.includes(label));
}
function findControl(node,label){
  if(node.tag==='span'&&node.textContent===label&&node.events?.click)return node;
  for(const n of node.nodes||[]) {
    const found=findControl(n,label);
    if(found)return found;
  }
  return null;
}
async function run(){
  await sleep(20);
  const openButton=control('[Open in App]');
  assert(openButton&&openButton.events.click);
  assert(control('Desktop popups OFF'),
    'existing desktop notifications must remain unchanged');
  assert.strictEqual(opened.length,0,'app must never auto-open');

  openButton.events.click();
  assert.strictEqual(opened.length,1);
  const app=opened[0];
  assert(app.name.includes('pletzel-chat-app-'));
  assert(app.features.includes('popup=yes'));
  assert(app.features.includes('resizable=yes'));
  assert(app.win.document.html.includes('height:100dvh'));
  assert(app.win.document.html.includes('@media(max-width:540px)'));
  assert(app.win.document.html.includes('id="app-input"'));
  assert(app.win.document.html.includes('font-family:monospace;font-size:13px'));
  assert(app.win.document.html.includes('height:40px'));
  assert(app.win.document.html.includes('width:min(INPUTPXpx,100%)'));
  assert(app.win.document.html.includes('class="input-shell"'));
  assert(app.win.document.html.includes('border-radius:11px'));
  assert(app.win.document.html.includes('.input-shell:focus-within'));
  assert(app.win.document.html.includes('placeholder="Type a message…"'));
  assert(app.win.document.html.includes('Enter ↵'));
  assert(app.win.document.html.includes('@media(prefers-reduced-motion:reduce)'));
  assert(app.win.document.html.includes('max-width:WRAPCHch'));
  assert(app.win.document.html.includes('white-space:pre-wrap;overflow-wrap:anywhere'));
  assert(!app.win.document.html.includes('class="message"'),
    'no redesigned chat cards');
  assert(!app.win.document.html.includes('id="app-send"'),
    'the original terminal has only an input, not a Send button');
  assert(!app.win.document.html.includes('Hello <b>'),
    'messages must not be interpolated into app HTML');
  assert(app.elems['app-messages'].textContent.includes('Hello <b>not markup</b>'));
  assert(app.elems['app-messages'].textContent.includes('--- Connected as: '));
  assert(app.elems['app-messages'].textContent.includes('--- Type your message and press Enter.'));
  assert(app.elems['app-messages'].textContent.includes('Alex'));

  // Re-click focuses the existing window without creating another session.
  control('[Open in App]').events.click();
  assert.strictEqual(opened.length,1);
  assert.strictEqual(app.win.focused,true);

  // Sending in the app goes through the existing Colab callback only.
  const appInput=app.elems['app-input'];
  appInput.value='Message from app';
  app.elems['app-form'].events.submit({preventDefault(){}});
  await sleep(40);
  assert.deepStrictEqual(sends,['Message from app']);
  assert.strictEqual(appInput.value,'');
  assert.strictEqual(mainElements['messaging-input-SESSION_ID'].value,'');
  assert.strictEqual(appInput.focused,true);

  // Polling the parent updates the app and preserves its own input.
  appInput.value='draft in progress';
  state.messages.push(makeMsg(2,'Sasha','New message from Colab'));
  polling();
  await sleep(30);
  assert(app.elems['app-messages'].textContent.includes('New message from Colab'));
  assert.strictEqual(appInput.value,'draft in progress');
  const message=app.elems['app-messages'].nodes.find(x=>x.tag==='span' &&
    x.textContent.includes('Hello <b>not markup</b>'));
  assert(message && message.events.click,
    'messages in the popup should be clickable exactly as in Colab');
  message.events.click();
  const reply=findControl(app.elems['app-messages'],'[Reply]');
  assert(reply&&reply.events.click,'original inline [Reply] must work');
  assert(findControl(app.elems['app-messages'],'[React]'),
    'original inline [React] must work');
  reply.events.click();
  assert(appInput.value.startsWith('/reply 1 '));

  // Closing and reopening creates a new usable view, not a duplicate poller.
  app.win.close();
  assert(app.win.closed);
  control('[Open in App]').events.click();
  assert.strictEqual(opened.length,2);
  assert(opened[1].elems['app-messages'].textContent.includes('New message from Colab'));
  assert(control('Desktop popups OFF'));

  // Browsers that block popups retain the classic chat and show a retry cue.
  opened[1].win.close();
  allowPopups=false;
  control('[Open in App]').events.click();
  assert(control('[Open in App]').title.includes('Browser blocked'));
  assert(control('Desktop popups OFF'));
  console.log('Original terminal preserved in app popup, with live mirror, send, replies, and resize: PASS');
}
run().catch(error=>{console.error(error);process.exitCode=1;});
"""


class AppWindowTests(unittest.TestCase):
    def test_layout_and_safe_app_bridge_are_present(self):
        self.assertIn("CLIENT_VERSION = '5.50'", SOURCE.read_text(encoding="utf-8"))
        self.assertIn("textControl('[Open in App]',openAppWindow", SCRIPT)
        self.assertIn("function appWindowFeatures()", SCRIPT)
        self.assertIn("function renderApp(state)", SCRIPT)
        self.assertIn("function appTimeline(state)", SCRIPT)
        self.assertIn("view.replaceChildren()", SCRIPT)
        self.assertNotIn("innerHTML = msg.text", SCRIPT)

    @unittest.skipUnless(shutil.which("node"), "Node.js not installed")
    def test_real_popup_mirrors_sends_resizes_and_reuses_window(self):
        result = subprocess.run(
            ["node", "-e", NODE_TEST], input=SCRIPT,
            text=True, capture_output=True, timeout=20, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + "\n" + result.stderr)


if __name__ == "__main__":
    unittest.main()
