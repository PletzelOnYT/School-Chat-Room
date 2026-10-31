"""Verify the organized production repository and current Colab entry points."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class RepositoryLayoutTests(unittest.TestCase):
    def test_production_files_are_grouped_by_role(self):
        for filename in (
            "app/messaging_app.py",
            "guardian/owner_guardian.py",
            "guardian/guardian_launcher.py",
            "docs/OWNER_GUARDIAN_SETUP.md",
        ):
            with self.subTest(path=filename):
                self.assertTrue((ROOT / filename).is_file())

    def test_current_compatibility_entry_points_work(self):
        for old, new in (
            ("messaging_app.py", "app/messaging_app.py"),
            ("owner_guardian.py", "guardian/owner_guardian.py"),
            ("guardian_launcher.py", "guardian/guardian_launcher.py"),
        ):
            with self.subTest(old=old):
                content = (ROOT / old).read_text(encoding="utf-8")
                self.assertIn(new, content)
                self.assertIn("compat_refresh", content)
        self.assertTrue((ROOT / "OWNER_GUARDIAN_SETUP.md").is_file())

    def test_removed_legacy_and_stress_files_are_absent(self):
        for deleted in (
            "school_chat.py",
            "guardian_stress_lab.py",
            "tools/guardian_stress_lab.py",
            "tests/test_guardian_stress_lab.py",
        ):
            with self.subTest(deleted=deleted):
                self.assertFalse((ROOT / deleted).exists())

    def test_internal_launchers_fetch_current_code(self):
        app = (ROOT / "app/messaging_app.py").read_text(encoding="utf-8")
        guardian = (ROOT / "guardian/guardian_launcher.py").read_text(encoding="utf-8")
        self.assertIn("main/app/messaging_app.py", app)
        self.assertIn("main/guardian/owner_guardian.py", guardian)

    def test_continuous_integration_targets_production_paths(self):
        workflow = (ROOT / ".github/workflows/check-python.yml").read_text()
        for folder in ("app/**", "guardian/**", "docs/**"):
            self.assertEqual(workflow.count(folder), 2)
        for name in ("app/messaging_app.py", "guardian/owner_guardian.py",
                     "guardian/guardian_launcher.py"):
            self.assertIn(name, workflow)
        for removed in ("school_chat.py", "guardian_stress_lab.py",
                        "tools/**"):
            self.assertNotIn(removed, workflow)


if __name__ == "__main__":
    unittest.main()
