"""Make sure the embedded Colab browser script is valid JavaScript."""
import ast
import shutil
import subprocess
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "app" / "messaging_app.py"
tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
html = next(ast.literal_eval(node.value)
            for node in tree.body if isinstance(node, ast.Assign)
            if any(isinstance(t, ast.Name) and t.id == "CHAT_HTML"
                   for t in node.targets))
script = html.split("<script>", 1)[1].split("</script>", 1)[0]


class BrowserTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js not installed")
    def test_embedded_browser_script_parses(self):
        result = subprocess.run(
            ["node", "--check"], input=script, text=True,
            capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_one_title_no_owner_password_prompt(self):
        self.assertIn("appendVerifiedTitle(state.my_verified, header)", script)
        self.assertIn("if (!msg.private) appendVerifiedTitle(msg.verified, line)", script)
        self.assertIn("showSelectedThread", script)
        self.assertIn("selectedMessageId", script)
        self.assertIn("emojiChoices", script)
        # No inline + or reply arrow after every ordinary chat message.
        self.assertNotIn("area.appendChild(textControl('  +'", script)
        self.assertNotIn("area.appendChild(textControl('  ↳'", script)
        self.assertIn("for (const group of groups)", script)
        self.assertIn("renderDescendant(group.root, 0)", script)
        self.assertIn("if (!msg.private && interactive && selectedMessageId === msg.id)", script)
        self.assertIn("requestFullscreen", script)
        self.assertNotIn("upload_attachment", script)
        self.assertNotIn("attachCallback", script)
        self.assertNotIn("fileInput", script)
        # Match the original v5.17 compact terminal layout, not the
        # v5.18/v5.19 tall bordered HTML application.
        self.assertIn('height:19px', html)
        self.assertIn('width:min(INPUTPXpx,100%)', html)
        self.assertIn('max-width:WRAPCHch', html)
        self.assertIn('font-family:monospace;font-size:13px', html)
        self.assertNotIn('chat-toolbar', html)
        self.assertNotIn('chat-viewport', html)
        self.assertNotIn('chat-compose', html)
        self.assertIn('textControl', script)
        self.assertIn('renderLine', script)
        for forbidden in ("awaitingOwnerKey", "my_owner", "my_mod",
                          "[OWNER]", "[MOD]", "appendRolePrefixes"):
            self.assertNotIn(forbidden, script)


if __name__ == "__main__":
    unittest.main()
