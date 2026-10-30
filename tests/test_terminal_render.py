"""Exercise the actual embedded browser renderer with a tiny DOM mock."""
import ast
import shutil
import subprocess
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1].joinpath("messaging_app.py").read_text()
TREE = ast.parse(SOURCE)
HTML = next(ast.literal_eval(n.value) for n in TREE.body
            if isinstance(n, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "CHAT_HTML" for t in n.targets))
JAVASCRIPT = HTML.split("<script>", 1)[1].split("</script>", 1)[0]

NODE_TEST = r"""
const vm = require('vm');
const fs = require('fs');
const assert = require('assert');
class TextNode {
  constructor(value) { this.textContent = String(value); }
}
class Element {
  constructor(tag) {
    this.tag = tag;
    this.nodes = [];
    this.events = {};
    this.style = {};
    this.isConnected = true;
    this.value = '';
  }
  appendChild(child) { this.nodes.push(child); return child; }
  replaceChildren(...children) { this.nodes = children; }
  get textContent() { return this.nodes.map(n => n.textContent).join(''); }
  set textContent(value) { this.nodes = [new TextNode(value)]; }
  addEventListener(event, callback) { this.events[event] = callback; }
  setAttribute(name, value) { this[name] = value; }
  focus() {}
  setSelectionRange() {}
}
const elements = {};
const document = {
  getElementById: id => elements[id] || (elements[id] = new Element('div')),
  createElement: type => new Element(type),
  createTextNode: value => new TextNode(value),
};
const state = {
  username: 'Alex',
  my_verified: true,
  running: true,
  popup: null,
  messages: [
    {id:'a', number:101, time:'12:00:01', user:'Alex', verified:true,
     text:'Hey!', reactions:{}, my_reactions:[], private:false},
    {id:'b', number:102, time:'12:00:02', user:'Emma', verified:false,
     text:'Hello!', reactions:{}, my_reactions:[], private:false},
  ],
  threads: [
    {root:{id:'a'},replies:[{id:'b',number:102,time:'12:00:02',user:'Emma',
      text:'Hello!',verified:false,reactions:{},private:false}],total_replies:1},
  ],
};
const google = {colab:{kernel:{invokeFunction: async(name,args) => {
  if (name.endsWith('.state')) return {data:{'application/json':state}};
  if (name.endsWith('.send')) return {data:{'application/json':state}};
  throw Error('unexpected callback: '+name);
}}}};
const sandbox = {
  document, google, console, setInterval:()=>0, clearInterval:()=>{},
  STATE_CALLBACK_JSON:'messaging_app.test.state',
  SEND_CALLBACK_JSON:'messaging_app.test.send',
  REFRESHMS:1000,
  window:{open:()=>null},
  URL:{createObjectURL:()=>'',revokeObjectURL:()=>{}},
  Blob: class {},
};
const script = fs.readFileSync(0, 'utf8');
vm.runInNewContext(script,sandbox);
setTimeout(() => {
  try {
    const area = elements['messaging-display-SESSION_ID'];
    const original = area.textContent;
    assert(original.includes('[12:00:01] #101 ☑ Alex: Hey!'), original);
    assert(original.includes('[12:00:02] #102 Emma: Hello!'), original);
    assert(!original.includes('[React]'));
    assert(!original.includes('[Reply]'));
    assert(!original.includes('↳'));
    assert(!original.includes('▸ Show'));
    assert(!original.includes('  +'));
    const row = area.nodes.find(n => n.tag==='span' &&
      n.textContent.includes('Alex: Hey!'));
    assert(row && typeof row.events.click === 'function');
    row.events.click();
    assert(area.textContent.includes('[React]'));
    assert(area.textContent.includes('[Reply]'));
    assert(area.textContent.includes('[View thread: 1]'));
    const close = area.nodes.find(n => n.tag==='span' &&
      n.textContent==='[Close]');
    assert(close);
    close.events.click({stopPropagation(){}});
    assert.strictEqual(area.textContent,original);
    console.log('Terminal display and click-to-open message actions: PASS');
  } catch(error) {
    console.error(error);
    process.exitCode=1;
  }
}, 0);
"""


class TerminalRenderTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js not installed")
    def test_clean_chronological_rows_and_click_to_open_actions(self):
        result = subprocess.run(
            ["node", "-e", NODE_TEST], input=JAVASCRIPT,
            capture_output=True, text=True, timeout=20, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
