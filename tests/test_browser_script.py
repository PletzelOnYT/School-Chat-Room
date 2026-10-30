"""Make sure the embedded Colab browser script is valid JavaScript."""
import ast
import shutil
import subprocess
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "school_chat.py"
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
        self.assertIn("if (data.my_verified)", script)
        self.assertIn("if (!msg.private && msg.verified)", script)
        self.assertIn("expandedThreads", script)
        self.assertIn("emojiChoices", script)
        self.assertIn("requestFullscreen", script)
        self.assertIn("upload_attachment", script)
        for forbidden in ("awaitingOwnerKey", "my_owner", "my_mod",
                          "[OWNER]", "[MOD]", "appendRolePrefixes"):
            self.assertNotIn(forbidden, script)


if __name__ == "__main__":
    unittest.main()
