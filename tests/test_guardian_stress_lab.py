"""Regression coverage for the isolated modified-client Guardian stress lab."""
import base64
import importlib.util
import json
import os
import sys
from unittest.mock import patch
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "guardian_stress_lab", ROOT / "tools" / "guardian_stress_lab.py"
)
lab_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(lab_module)

SPEC_GUARDIAN = importlib.util.spec_from_file_location(
    "test_attacks_owner_guardian", ROOT / "guardian" / "owner_guardian.py"
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

    def test_colab_ignores_stale_local_guardian(self):
        class Response:
            def __init__(self, body):
                self.body = body
            def read(self):
                return self.body
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False

        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "guardian_stress_lab.py"
            script.write_text("# stress lab")
            stale = Path(tmp) / "owner_guardian.py"
            stale.write_text("GUARDIAN_VERSION = '2.5'\n")
            fresh = b"GUARDIAN_VERSION = '2.7'\n"
            urls = []
            def fetch(request, timeout=25):
                urls.append(request.full_url)
                return Response(fresh)
            with (
                patch.object(lab_module, "__file__", str(script)),
                patch.dict(os.environ, {"COLAB_RELEASE_TAG": "colab-test"}),
                patch("urllib.request.urlopen", side_effect=fetch),
            ):
                current = lab_module.obtain_guardian()
            self.assertEqual(current.GUARDIAN_VERSION, "2.7")
            self.assertEqual(len(urls), 1)
            self.assertIn("lab_fresh=", urls[0])
            self.assertIn("raw.githubusercontent.com", urls[0])
            self.assertEqual(stale.read_text(), "GUARDIAN_VERSION = '2.5'\n")

    def test_remote_fallback_uses_github_contents_api_if_raw_is_stale(self):
        class Response:
            def __init__(self, body):
                self.body = body
            def read(self):
                return self.body
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False

        stale = b"GUARDIAN_VERSION = '2.5'\n"
        fresh = b"GUARDIAN_VERSION = '2.7'\n"
        content = json.dumps({
            "encoding": "base64",
            "content": base64.b64encode(fresh).decode(),
        }).encode()
        urls = []
        def fetch(request, timeout=25):
            urls.append(request.full_url)
            return Response(stale if len(urls) == 1 else content)
        with (
            patch.object(lab_module, "__file__", "https://raw.example/stress_lab"),
            patch("urllib.request.urlopen", side_effect=fetch),
        ):
            current = lab_module.obtain_guardian()
        self.assertEqual(current.GUARDIAN_VERSION, "2.7")
        self.assertEqual(len(urls), 2)
        self.assertIn("raw.githubusercontent.com", urls[0])
        self.assertIn("api.github.com", urls[1])

    def test_local_repo_guardian_can_run_without_network(self):
        with (
            patch.object(lab_module, "__file__",
                         str(ROOT / "tools" / "guardian_stress_lab.py")),
            patch.dict(os.environ, {"COLAB_RELEASE_TAG": "",
                                    "COLAB_GPU": ""}),
            patch("urllib.request.urlopen",
                  side_effect=AssertionError("should not fetch online")),
        ):
            current = lab_module.obtain_guardian()
        self.assertGreaterEqual(
            tuple(map(int, current.GUARDIAN_VERSION.split("."))),
            (2, 7),
        )

    def test_no_live_write_path_exists_in_modified_client(self):
        import inspect
        text = (ROOT / "tools" / "guardian_stress_lab.py").read_text()
        self.assertIn("LIVE_CHAT_READ_ONLY.read_bytes()", text)
        self.assertIn("guardian_stress_lab_", text)
        self.assertNotIn("LIVE_CHAT_READ_ONLY.write", text)
        self.assertNotIn("drive.mount", text)


if __name__ == "__main__":
    unittest.main()
