"""Exercise the Discord-style popup without disturbing the original Colab UI."""
import ast
import shutil
import subprocess
import unittest
from pathlib import Path

SOURCE=Path(__file__).resolve().parents[1]/"app"/"messaging_app.py"
TREE=ast.parse(SOURCE.read_text(encoding="utf-8"))
HTML=next(ast.literal_eval(n.value) for n in TREE.body if isinstance(n,ast.Assign)
          if any(isinstance(t,ast.Name) and t.id=="CHAT_HTML" for t in n.targets))
SCRIPT=HTML.split("<script>",1)[1].split("</script>",1)[0]

NODE_TEST=r"""
const assert=require('assert'),vm=require('vm'),fs=require('fs');
class TextNode{constructor(t){this.textContent=String(t)}}
class Element{
 constructor(tag='div'){
   this.tag=tag;this.nodes=[];this.events={};this.style={};this.value='';
   this.isConnected=true;this.scrollTop=0;this.scrollHeight=150;this.clientHeight=150;
   this.className='';this.placeholder='';
 }
 appendChild(x){this.nodes.push(x);return x}
 replaceChildren(...nodes){this.nodes=nodes}
 get textContent(){return this.nodes.map(x=>x.textContent||'').join('')}
 set textContent(text){this.nodes=[new TextNode(text)]}
 setAttribute(key,value){this[key]=value}
 addEventListener(name,fn){this.events[name]=fn}
 focus(){this.focused=true}
 setSelectionRange(){}
}
const elems={};
const document={
 getElementById:id=>elems[id]||(elems[id]=new Element()),
 createElement:tag=>new Element(tag),
 createTextNode:t=>new TextNode(t)
};
const makeMsg=(number,user,text,extra={})=>({
 id:'m'+number,number,user,text,private:false,
 time:'18:30:00',issued_at:1793450000,reactions:{},my_reactions:[],
 ...extra
});
let state={
 username:'Alex',my_verified:false,running:true,popup:null,
 messages:[
   makeMsg(1,'Emma','Hello <img src=x onerror=alert(1)>',{verified:true}),
   makeMsg(2,'Alex','Good morning',{reactions:{'👍':2},my_reactions:['👍']}),
 ],threads:[]
};
let polling,allowed=true,openCount=0;
const opened=[],sent=[];
const browser={
 screen:{availLeft:0,availTop:0,availWidth:1450,availHeight:930},
 open(url,name,features){
   if(!allowed)return null;
   const nodes={};
   const popupDocument={
     open(){},write(text){this.html=text},close(){},
     getElementById:id=>nodes[id]||(nodes[id]=new Element()),
     createElement:tag=>new Element(tag),
     createTextNode:t=>new TextNode(t)
   };
   const win={document:popupDocument,closed:false,
     close(){this.closed=true},focus(){this.focused=true}};
   opened.push({win,name,features,nodes});openCount++;
   return win;
 }
};
const google={colab:{kernel:{invokeFunction:async(name,args)=>{
 if(name.endsWith('.state'))return {data:{'application/json':state}};
 if(name.endsWith('.send')){
   sent.push(args[0]);return {data:{'application/json':state}};
 }
 throw Error('Invalid callback');
}}}};
const logged=[];
vm.runInNewContext(fs.readFileSync(0,'utf8'),{
 document,window:browser,google,console:{
   debug:(...args)=>logged.push(args.map(String).join(' ')),
   error:(...args)=>logged.push(args.map(String).join(' '))
 },
 setTimeout,clearTimeout,setInterval:fn=>{polling=fn;return 1;},
 clearInterval:()=>{},URL:{revokeObjectURL(){}},Blob:class{},
 STATE_CALLBACK_JSON:'chat.test.state',SEND_CALLBACK_JSON:'chat.test.send',
 REFRESHMS:1000
});
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
function find(node,predicate){
 if(predicate(node))return node;
 for(const child of node.nodes||[]){
   const hit=find(child,predicate);
   if(hit)return hit;
 }
 return null;
}
function all(node,predicate){
 const results=[];
 function visit(n){
   if(predicate(n))results.push(n);
   for(const child of n.nodes||[])visit(child);
 }
 visit(node);return results;
}
function clickButton(node,text){
 const hit=find(node,n=>n.tag==='button' && n.textContent.includes(text));
 assert(hit && hit.events.click,'missing '+text);
 hit.events.click();return hit;
}
function classicControl(label){
 const area=elems['messaging-display-SESSION_ID'];
 const header=find(area,n=>n.tag==='span'&&n.textContent.includes('Connected as'));
 return header?.nodes.find(n=>n.tag==='span'&&n.textContent.includes(label));
}
async function run(){
 await sleep(25);
 assert(classicControl('[Open in App]'),'open button stays in Colab');
 assert(classicControl('Desktop popups OFF'),'desktop notifications unchanged');
 assert.strictEqual(opened.length,0,'no unsolicited popup');
 classicControl('[Open in App]').events.click();
 assert.strictEqual(opened.length,1);
 const app=opened[0],html=app.win.document.html,el=app.nodes;
 assert(app.features.includes('resizable=yes'));
 assert(app.features.includes('width=1080'));
 assert(html.includes('class="rail"')&&html.includes('class="sidebar"'));
 assert(html.includes('class="topbar"'));
 assert(html.includes('<title>Chat Room</title>'));
 assert(html.includes('<div class="server-title"><span>Chat Room</span></div>'));
 assert(html.includes('class="rail-badge" title="Chat Room">C'));
 assert(!html.includes('Pletzel Chat'),'do not use old app name');
 assert(!html.includes('server-sub'),'remove decorative dropdown chevron');
 assert(!html.includes('One shared chat room.'),'remove redundant sidebar explanation');
 assert(!html.includes('Messages sync through your Colab session.'));
 assert(!html.includes('Pletzel Chat · Shared room'));
 assert(!html.includes('channel-description'),'remove redundant topbar subtitle');
 assert(html.includes('class="composer-wrap"'));
 assert(html.includes('class="message"')===false,'messages must be dynamic');
 assert(html.includes('id="app-search"'));
 assert(!html.includes('id="app-thread-panel"'));
 assert(!html.includes('id="app-thread-form"'));
 assert(!html.includes('ACTIVE THREADS'));
 assert(!html.includes('id="app-threads-nav"'));
 assert(!html.includes('thread-open'));
 assert(html.includes('id="app-mention-list"'));
 assert(html.includes('id="app-pings"'));
 assert(!html.includes('@keyframes thread-in'));
 assert(html.includes('@keyframes message-arrive'));
 assert(html.includes('prefers-reduced-motion:reduce'));
 assert(html.includes('@media(max-width:670px)'));
 assert(html.includes('background:var(--surface)'));
 assert(!html.includes('onerror=alert'),'untrusted text must not be in markup');
 assert(el['app-profile-name'].textContent.includes('Alex'));
 assert(el['app-profile-status'].textContent.includes('Colab'));
 const transcript=el['app-transcript'];
 assert(transcript.textContent.includes('Hello <img src=x onerror=alert(1)>'));
 const message=find(transcript,n=>n.tag==='article'&&n.className==='message');
 assert(message,'each message is a proper Discord-style row');
 assert(find(message,n=>n.className==='avatar'),'message has avatar');
 assert(find(message,n=>n.className==='message-user'),'message has username');
 assert(find(message,n=>n.className==='message-time'),'message has timestamp');
 const verified=find(message,n=>n.className==='verified');
 assert(verified,'verified icon exists');
 assert.strictEqual(verified.textContent,'✓','the badge must be checkmark only');
 assert(!verified.textContent.includes('Verified'),'no redundant verified label');
 assert(find(transcript,n=>n.className==='react-pill mine'),'reactions retained');
 assert.strictEqual(opened.length,1);
 classicControl('[Open in App]').events.click();
 assert.strictEqual(opened.length,1,'repeated opens focus existing popup');

 const input=el['app-input'],form=el['app-form'];
 input.value='Hi from popup';
 form.events.submit({preventDefault(){}});
 await sleep(45);
 assert.deepStrictEqual(sent,['Hi from popup']);
 assert.strictEqual(input.value,'');
 assert.strictEqual(elems['messaging-input-SESSION_ID'].value,'');
 assert(input.focused,'app input remains focused');

 // A reply is sent using the unchanged /reply server command.
 clickButton(transcript,'↳ Reply');
 assert.strictEqual(el['app-replybar'].style.display,'flex');
 assert(el['app-replyname'].textContent.includes('Emma'));
 assert(el['app-replydetail'].textContent.includes('Hello <img src=x onerror=alert(1)>'),
   'reply composer previews the original text without HTML interpretation');
 input.value='Reply from popup';
 form.events.submit({preventDefault(){}});
 await sleep(820);
 assert(sent.some(x=>x==='/reply 1 Reply from popup'),sent);
 assert.strictEqual(el['app-replybar'].style.display,'none');

 // Emoji reactions use the unchanged server command.
 const first=find(transcript,n=>n.tag==='article'&&n.textContent.includes('Emma'));
 clickButton(first,'☺ React');
 const reactPicker=find(el['app-transcript'],n=>n.className==='picker');
 assert(reactPicker);
 clickButton(reactPicker,'😂');
 await sleep(810);
 assert(sent.includes('/react 1 😂'),sent);

 // Search is local and only filters the current visible chat snapshot.
 el['app-search'].value='good morning';
 el['app-search'].events.input();
 assert(transcript.textContent.includes('Good morning'));
 assert(!transcript.textContent.includes('Hello <img'));
 el['app-search'].value='';
 el['app-search'].events.input();

 // Polls synchronize the app, not just the Colab panel.
 input.value='unsent draft';
 state.messages.push(makeMsg(3,'Sasha','Incoming live!'));
 polling();
 await sleep(30);
 assert(transcript.textContent.includes('Incoming live!'));
 assert.strictEqual(input.value,'unsent draft');


 // Username pings are highlighted and counted only when newly received.
 state.messages.push(makeMsg(4,'Emma','hey @Alex check this out'));
 polling();
 await sleep(40);
 assert(find(transcript,n=>n.className==='ping-mention mine'),'self ping styled');
 assert(find(transcript,n=>n.className==='ping-marker'),'ping highlight label');
 assert.strictEqual(el['app-ping-count'].textContent,'1');
 el['app-pings'].events.click();
 assert(transcript.textContent.includes('hey @Alex'));
 assert(!transcript.textContent.includes('Good morning'));
 assert.strictEqual(el['app-ping-count'].textContent,'0');
 el['app-pings'].events.click();
 assert(transcript.textContent.includes('Good morning'));

 // @mention suggestions insert a real username without sending a message.
 input.value='hello @Em';
 input.events.input();
 const mentionList=el['app-mention-list'];
 assert.strictEqual(mentionList.style.display,'block');
 clickButton(mentionList,'@Emma');
 assert.strictEqual(input.value,'hello @Emma ');
 assert.strictEqual(mentionList.style.display,'none');

 // Existing reply records are still rendered inline with Discord-style
 // parent previews; there is NO thread sidebar or separate thread composer.
 state.threads=[{root:state.messages[0],replies:[
   makeMsg(5,'Sasha','First reply',{reply_to:'m1'}),
   makeMsg(6,'Alex','Nested reply',{reply_to:'m5'})
 ],total_replies:2}];
 polling();
 await sleep(35);
 const reply=find(transcript,n=>n.tag==='article'&&n._appMessageId==='m5');
 assert(reply,'reply appears directly in channel');
 const preview=find(reply,n=>String(n.className||'').includes('reply-reference'));
 assert(preview && preview.textContent.includes('Emma'));
 assert(preview.textContent.includes('Hello <img src=x onerror=alert(1)>'));
 assert(!reply.textContent.includes('Thread reply'));
 preview.events.click();
 const original=find(transcript,n=>n.tag==='article'&&n._appMessageId==='m1');
 assert(original.className.includes('reply-focus'),
   'preview jumps to original and highlights it');

 const nested=find(transcript,n=>n.tag==='article'&&n._appMessageId==='m6');
 assert(nested,'nested reply remains in main chat');
 const nestedPreview=find(nested,n=>String(n.className||'').includes('reply-reference'));
 assert(nestedPreview && nestedPreview.textContent.includes('Sasha'));
 assert(nestedPreview.textContent.includes('First reply'),
   'nested reply points to its direct parent');
 assert(!find(transcript,n=>n.tag==='button' &&
   String(n.textContent).includes('Thread')),'no thread buttons');
 assert(!find(transcript,n=>n.tag==='button' &&
   String(n.textContent).includes('View thread')),'no thread navigation links');
 assert(!String(el['app-layout'].className).includes('thread-open'));
 assert(!html.includes('thread-open'),'popup cannot open a separate thread view');

 // Ordinary Reply still sends the existing /reply backend command.
 clickButton(nested,'↳ Reply');
 assert(el['app-replyname'].textContent.includes('Alex'));
 assert(el['app-replydetail'].textContent.includes('Nested reply'));
 input.value='Answer to nested reply';
 form.events.submit({preventDefault(){}});
 await sleep(820);
 assert(sent.includes('/reply 6 Answer to nested reply'),sent);

 // The mocked backend must restore its ordinary full timeline when reopened.
 state.threads=[];
 app.win.close();
 classicControl('[Open in App]').events.click();
 assert.strictEqual(opened.length,2);
 assert(opened[1].nodes['app-transcript'].textContent.includes('Incoming live!'));
 opened[1].win.close();
 allowed=false;
 classicControl('[Open in App]').events.click();
 assert(classicControl('[Open in App]').title.includes('Browser blocked'));
 assert(classicControl('Desktop popups OFF'));
 assert(logged.every(x=>x.includes('App popup blocked: Error: Browser blocked popup')),
   'Unexpected browser errors: '+logged.join(' | '));
 console.log('Discord-style app: layout, safety, send, reply, reactions, search, live sync PASS');
}
run().catch(e=>{console.error(e);process.exitCode=1});
"""


class AppWindowTests(unittest.TestCase):
    def test_discord_style_only_affects_popup(self):
        source=SOURCE.read_text(encoding="utf-8")
        self.assertIn("CLIENT_VERSION = '5.56'",source)
        self.assertIn('font-family:monospace;font-size:13px',HTML)
        self.assertIn('height:19px',HTML)
        self.assertIn("textControl('[Open in App]',openAppWindow",SCRIPT)
        self.assertIn("function renderApp(state",SCRIPT)
        self.assertIn("function appTimeline(state)",SCRIPT)
        self.assertIn("appTextWithPings(doc,body",SCRIPT)
        self.assertNotIn("function appRenderThread(state)",SCRIPT)
        self.assertNotIn("function appOpenThread(",SCRIPT)
        self.assertNotIn("app-thread-panel",SCRIPT)
        self.assertIn("function appReplyPreview(",SCRIPT)
        self.assertIn("function appUpdateMentions()",SCRIPT)
        self.assertIn("function appReplyPreview(",SCRIPT)
        self.assertIn("function appJumpToMessage(",SCRIPT)
        self.assertIn("badge.textContent='✓'",SCRIPT)
        self.assertIn("appHasMention(msg.text,state.username)",SCRIPT)
        self.assertNotIn("innerHTML = msg.text",SCRIPT)

    @unittest.skipUnless(shutil.which("node"),"Node.js required")
    def test_app_visual_structure_and_live_operations(self):
        result=subprocess.run(
            ["node","-e",NODE_TEST],input=SCRIPT,text=True,
            capture_output=True,timeout=25,check=False)
        self.assertEqual(result.returncode,0,result.stdout+"\n"+result.stderr)


if __name__=="__main__":
    unittest.main()
