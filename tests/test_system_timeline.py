"""Private System notices follow normal chat traffic; no pinned footer."""
import ast
import shutil
import subprocess
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app" / "messaging_app.py"
TREE = ast.parse(APP.read_text(encoding="utf-8"))
HTML = next(ast.literal_eval(n.value) for n in TREE.body
            if isinstance(n, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "CHAT_HTML"
                for t in n.targets))
SCRIPT = HTML.split("<script>", 1)[1].split("</script>", 1)[0]

NODE_TIMELINE_TEST = r"""
const assert = require('assert');
const vm = require('vm');
class TextNode { constructor(text) { this.textContent = String(text); } }
class Element {
  constructor() {
    this.tag = 'div'; this.nodes=[]; this.events={}; this.style={};
    this.isConnected=true; this.value='';
  }
  appendChild(node) { this.nodes.push(node); return node; }
  replaceChildren(...nodes) { this.nodes=nodes; }
  get textContent() { return this.nodes.map(node=>node.textContent).join(''); }
  set textContent(text) { this.nodes=[new TextNode(text)]; }
  addEventListener(type, fn) { this.events[type]=fn; }
  setAttribute(name, value) { this[name]=value; }
  focus() {}
  setSelectionRange() {}
}
const elements={};
const document={
  getElementById: id=>elements[id]||(elements[id]=new Element()),
  createElement: tag=>{const e=new Element();e.tag=tag;return e;},
  createTextNode: text=>new TextNode(text),
};
const mk = (id,number,user,text,reply_to) => ({
  id,number,user,text,reply_to,
  time:'12:00:00',reactions:{},my_reactions:[],private:false,
});
const a=mk('a',100,'Alex','first normal message');
const b=mk('b',101,'Emma','second normal message');
const c=mk('c',102,'Sasha','third new message');
const d=mk('d',103,'Alex','fourth new message');
const system={
  id:'local-system',anchor:101,number:null,time:'12:00:01',
  user:'System',text:'Usage: /last <1-100>',private:true,reactions:{},
};
const system2={
  id:'local-system2',anchor:101,number:null,time:'12:00:02',
  user:'System',text:'Second command warning',private:true,reactions:{},
};
const group = msg => ({root:msg,replies:[],total_replies:0});
const state={
  username:'Alex',my_verified:false,running:true,popup:null,
  messages:[a,b,system,system2,c],
  threads:[group(a),group(b),group(c)],
};
let refreshHandler;
const google={colab:{kernel:{invokeFunction:async (name,args)=>{
  if(name.endsWith('.state')) return {data:{'application/json':state}};
  if(name.endsWith('.send')) return {data:{'application/json':state}};
  throw Error('unexpected callback');
}}}};
const sandbox={
  document,google,console,setTimeout,clearTimeout,
  setInterval: fn=>{refreshHandler=fn;return 0;}, clearInterval:()=>{},
  STATE_CALLBACK_JSON:'messaging_app.test.state',
  SEND_CALLBACK_JSON:'messaging_app.test.send',
  REFRESHMS:1000,
  window:{open:()=>null},
  URL:{createObjectURL:()=>'',revokeObjectURL(){}},
  Blob:class {},
};
vm.runInNewContext(require('fs').readFileSync(0,'utf8'),sandbox);
const area=elements['messaging-display-SESSION_ID'];
const rendered=async()=>{
  refreshHandler();
  await new Promise(resolve=>setTimeout(resolve,15));
  return area.textContent;
};
function before(text,a,b) {
  assert(text.includes(a), 'Missing '+a+': '+text);
  assert(text.includes(b), 'Missing '+b+': '+text);
  assert(text.indexOf(a) < text.indexOf(b),
    'Expected '+a+' before '+b+': '+text);
}
async function test(){
  await new Promise(resolve=>setTimeout(resolve,20));
  let text=area.textContent;
  before(text,'Emma: second normal message','System: Usage: /last <1-100>');
  before(text,'System: Usage: /last <1-100>','System: Second command warning');
  before(text,'System: Second command warning','Sasha: third new message');
  assert(!text.includes('#null System'));
  assert(!text.includes('#undefined System'));
  // Incoming shared messages must appear BELOW the earlier private notice.
  state.threads.push(group(d));
  state.messages.push(d);
  text=await rendered();
  before(text,'System: Usage: /last <1-100>','Alex: fourth new message');
  before(text,'Sasha: third new message','Alex: fourth new message');
  // A reply grouped with its parent does not pin the private notice.
  const reply=mk('r',104,'Emma','reply after warning','d');
  state.threads[3].replies.push(reply);
  state.messages.push(reply);
  text=await rendered();
  before(text,'System: Usage: /last <1-100>','Emma: reply after warning');
  assert(text.includes('↳ [12:00:00] #104 Emma: reply after warning'));
  // When the private note is no longer in backend visible history, it
  // disappears. No client-side special footer keeps it alive.
  state.messages=state.messages.filter(item=>!item.private);
  text=await rendered();
  assert(!text.includes('Usage: /last <1-100>'),text);
  assert(text.includes('Emma: reply after warning'));
  // Empty thread list uses backend mixed chronology, not private footer.
  state.threads=[];
  state.messages=[a,system,b,c];
  text=await rendered();
  before(text,'Alex: first normal message','System: Usage: /last <1-100>');
  before(text,'System: Usage: /last <1-100>','Emma: second normal message');
  console.log('Private System messages scroll with shared chat traffic: PASS');
}
test().catch(e=>{console.error(e);process.exitCode=1;});
"""

class SystemTimelineTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js not installed")
    def test_private_notices_insert_between_threads_and_age_out(self):
        result=subprocess.run(
            ["node", "-e", NODE_TIMELINE_TEST],
            input=SCRIPT,text=True,capture_output=True,timeout=20,check=False,
        )
        self.assertEqual(result.returncode,0,result.stdout+"\n"+result.stderr)

    def test_backend_notices_stay_local_and_anchor_when_created(self):
        source=APP.read_text(encoding="utf-8")
        tree=ast.parse(source)
        note=next(n for n in tree.body
                  if isinstance(n,ast.FunctionDef) and n.name=="note")
        src=ast.get_source_segment(source,note)
        self.assertIn("len(latest_analysis()['messages'])",src)
        self.assertIn("'private': True",src)
        self.assertNotIn("write_event",src)
        self.assertNotIn("append_event_in_place",src)


if __name__=="__main__":
    unittest.main()
