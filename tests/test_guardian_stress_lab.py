"""Regression coverage for the isolated modified-client Guardian stress lab."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "guardian_stress_lab", ROOT / "guardian_stress_lab.py"
)
lab_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(lab_module)

SPEC_GUARDIAN = importlib.util.spec_from_file_location(
    "test_attacks_owner_guardian", ROOT / "owner_guardian.py"
)
guardian = importlib.util.module_from_spec(SPEC_GUARDIAN)
SPEC_GUARDIAN.loader.exec_module(guardian)


class GuardianStressLabTests(unittest.TestCase):
    def setUp(self):
        self.lab = lab_module.StressLab(guardian)
        self.addCleanup(self.lab.close)

    def test_all_attack_cases_are_isolated_and_identified(self):
        rows = self.lab.run_all()
        self.assertEqual(len(rows), len(lab_module.ATTACK_LABELS))
        self.assertEqual({r["case"] for r in rows},
                         set(lab_module.ATTACK_LABELS))
        self.assertEqual(next(r for r in rows if r["case"] == "normal")["status"],
                         "PASS")
        for case in ("ansi", "html", "bidi", "owner", "red_title",
                     "reaction", "spam", "duplicate", "rewrite",
                     "reorder", "delete", "corrupt", "invalid_clock"):
            with self.subTest(case=case):
                row = next(r for r in rows if r["case"] == case)
                self.assertEqual(row["status"], "BLOCKED", row)
                self.assertTrue(row["restored"], row)

    def test_untrusted_title_and_member_impersonation_are_reported(self):
        # This documents current weaknesses rather than hiding them.
        outcomes = {case: self.lab.test(case)
                    for case in ("verified", "rename", "impersonate")}
        for case, row in outcomes.items():
            self.assertEqual(row["status"], "SECURITY GAP",
                             (case, row))
            self.assertEqual(row["guardian_result"], "healthy",
                             (case, row))

    def test_red_rollback_restores_exact_original_bytes_then_removes_cases(self):
        baseline = self.lab.seed
        for case in ("verified", "owner", "spam"):
            self.lab.test(case)
        self.assertTrue(self.lab.cases)
        changed_paths = tuple(self.lab.cases.values())
        self.assertEqual(self.lab.revert_all(), 3)
        for path in changed_paths:
            self.assertFalse(path.exists())
        self.assertEqual(self.lab.cases, {})
        self.assertEqual(self.lab.results, [])
        self.assertEqual(self.lab.revert_all(), 0)
        self.assertEqual(self.lab.seed, baseline)

    def test_read_only_fixture_never_mutated(self):
        with tempfile.TemporaryDirectory() as tmp:
            live = Path(tmp) / "readonly_original.json"
            original = lab_module.seed_fixture(guardian)
            live.write_bytes(original)
            newlab = lab_module.StressLab(guardian, seed=live.read_bytes())
            try:
                row = newlab.test("rewrite")
                self.assertEqual(row["status"], "BLOCKED")
                self.assertEqual(live.read_bytes(), original)
                self.assertTrue(newlab.root.is_relative_to(
                    Path(tempfile.gettempdir()).resolve()))
                self.assertNotEqual(newlab.root, live.parent)
            finally:
                newlab.close()
            self.assertEqual(live.read_bytes(), original)

    def test_no_live_write_path_exists_in_modified_client(self):
        import inspect
        text = (ROOT / "guardian_stress_lab.py").read_text()
        self.assertIn("LIVE_CHAT_READ_ONLY.read_bytes()", text)
        self.assertIn("guardian_stress_lab_", text)
        self.assertNotIn("LIVE_CHAT_READ_ONLY.write", text)
        self.assertNotIn("drive.mount", text)


if __name__ == "__main__":
    unittest.main()
