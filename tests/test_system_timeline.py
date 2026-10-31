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
  // /last 2: the local System notice must consume one of two message slots.
  // Older shared groups still exist as reply-preview context, not visible rows.
  state.threads=[group(a),group(b),group(c),group(d)];
  state.messages=[system2,c];
  text=await rendered();
  assert(text.includes('System: Second command warning'),text);
  assert(text.includes('Sasha: third new message'),text);
  assert(!text.includes('Alex: first normal message'),text);
  assert(!text.includes('Emma: second normal message'),text);
  assert(!text.includes('Alex: fourth new message'),text);
  // When two new shared messages arrive, that System notice scrolls out.
  state.messages=[c,d];
  text=await rendered();
  assert(!text.includes('System: Second command warning'),text);
  assert(text.includes('Sasha: third new message'),text);
  assert(text.includes('Alex: fourth new message'),text);
  console.log('Private System messages consume /last slots and scroll away: PASS');
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


    def test_backend_last_quota_counts_system_notices_before_thread_building(self):
        """A local warning is one display slot, not a permanent extra row."""
        import threading
        import time
        source=APP.read_text(encoding="utf-8")
        tree=ast.parse(source)
        snapshot_node=next(n for n in tree.body
                           if isinstance(n,ast.FunctionDef) and n.name=="snapshot")
        messages=[dict(number=n,id="m"+str(n),user="Emma",text="Post "+str(n))
                  for n in (1,2,3,4)]
        local=[dict(id="local1",number=None,anchor=3,user="System",
                    text="Usage warning",private=True,time="12:00:00",reactions={})]
        counts=[]
        def build_groups(shared,count,muted,cleared):
            counts.append(count)
            return []
        env={
            "check_upstream_version":lambda:None,
            "reconcile_outbox":lambda:None,
            "latest_analysis":lambda:{"messages":messages},
            "update_required":False,"update_notice_shown":False,
            "running":True,"local_messages":local,"state_lock":threading.RLock(),
            "clear_through":0,"muted_names":set(),"visible_count":2,
            "is_mentioned":lambda text,name:False,
            "build_threads":build_groups,"JSON":lambda obj:obj,
            "username":"Alex","next_popup":None,"time":time,
            "ChatFileError":type("ChatFileError",(Exception,),{}),
        }
        compiled=compile(ast.Module(body=[snapshot_node],type_ignores=[]),
                         "<snapshot-test>","exec")
        exec(compiled,env)
        snap=env["snapshot"]()
        self.assertEqual([m["text"] for m in snap["messages"]],
                         ["Usage warning","Post 4"])
        self.assertEqual(counts[-1],1)
        messages.append(dict(number=5,id="m5",user="Emma",text="Post 5"))
        snap=env["snapshot"]()
        self.assertEqual([m["text"] for m in snap["messages"]],
                         ["Post 4","Post 5"])
        self.assertEqual(counts[-1],2)
        # /last 1 can consist entirely of a private System notice.
        env["visible_count"]=1
        local.append(dict(id="local2",number=None,anchor=5,user="System",
                          text="Most recent warning",private=True,
                          time="12:00:01",reactions={}))
        snap=env["snapshot"]()
        self.assertEqual([m["text"] for m in snap["messages"]],
                         ["Most recent warning"])
        self.assertEqual(counts[-1],0)

    def test_zero_shared_slots_do_not_reintroduce_all_old_thread_groups(self):
        source=APP.read_text(encoding="utf-8")
        tree=ast.parse(source)
        build=next(n for n in tree.body
                   if isinstance(n,ast.FunctionDef) and n.name=="build_threads")
        segment=ast.get_source_segment(source,build)
        self.assertIn("if count <= 0:",segment)
        self.assertIn("return []",segment)



if __name__=="__main__":
    unittest.main()
