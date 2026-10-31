"""Keep the reorganized repository and all legacy Colab entry points valid."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class RepositoryLayoutTests(unittest.TestCase):
    def test_source_files_are_grouped_by_role(self):
        for filename in (
            "app/messaging_app.py",
            "guardian/owner_guardian.py",
            "guardian/guardian_launcher.py",
            "tools/guardian_stress_lab.py",
            "docs/OWNER_GUARDIAN_SETUP.md",
        ):
            with self.subTest(path=filename):
                self.assertTrue((ROOT / filename).is_file())

    def test_legacy_links_still_have_entry_points(self):
        for old, new in (
            ("messaging_app.py", "app/messaging_app.py"),
            ("owner_guardian.py", "guardian/owner_guardian.py"),
            ("guardian_launcher.py", "guardian/guardian_launcher.py"),
            ("guardian_stress_lab.py", "tools/guardian_stress_lab.py"),
        ):
            with self.subTest(old=old):
                content = (ROOT / old).read_text(encoding="utf-8")
                self.assertIn(new, content)
                self.assertIn("compat_refresh", content)
        school = (ROOT / "school_chat.py").read_text(encoding="utf-8")
        self.assertIn("main/app/messaging_app.py", school)
        self.assertTrue((ROOT / "OWNER_GUARDIAN_SETUP.md").is_file())

    def test_internal_launchers_fetch_moved_code(self):
        app = (ROOT / "app/messaging_app.py").read_text(encoding="utf-8")
        guardian = (ROOT / "guardian/guardian_launcher.py").read_text(encoding="utf-8")
        stress = (ROOT / "tools/guardian_stress_lab.py").read_text(encoding="utf-8")
        self.assertIn("main/app/messaging_app.py", app)
        self.assertIn("main/guardian/owner_guardian.py", guardian)
        self.assertIn("main/guardian/owner_guardian.py", stress)
        self.assertIn("contents/guardian/owner_guardian.py", stress)

    def test_continuous_integration_targets_new_paths(self):
        workflow = (ROOT / ".github/workflows/check-python.yml").read_text()
        for folder in ("app/**", "guardian/**", "tools/**", "docs/**"):
            self.assertEqual(workflow.count(folder), 2)
        for name in ("app/messaging_app.py", "guardian/owner_guardian.py",
                     "guardian/guardian_launcher.py", "tools/guardian_stress_lab.py"):
            self.assertIn(name, workflow)


if __name__ == "__main__":
    unittest.main()
