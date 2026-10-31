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

    def test_every_attack_has_exactly_one_category(self):
        all_cases = sorted(lab_module.ATTACK_LABELS)
        groups = [case for cases in lab_module.ATTACK_GROUPS.values()
                  for case in cases]
        self.assertEqual(sorted(groups), all_cases)
        self.assertEqual(len(groups), len(set(groups)))
        self.assertGreaterEqual(len(all_cases), 55)

    def test_chat_preview_contains_real_attack_and_guardian_snapshots(self):
        row = self.lab.test("verified")
        self.assertEqual(row["status"], "SECURITY GAP")
        snapshots = self.lab.previews["verified"]
        self.assertEqual(set(snapshots),
                         {"before", "attack", "after", "status", "note"})
        before = snapshots["before"]
        attacked = snapshots["attack"]
        after = snapshots["after"]
        self.assertEqual(attacked["events"], before["events"] + 1)
        self.assertEqual(after["events"], attacked["events"])
        self.assertTrue(any("Verified" in x["text"]
                            for x in attacked["rows"]))
        self.assertEqual(self.lab.cases["verified"].read_bytes(),
                         self.lab.cases["verified"].read_bytes())

    def test_rendered_chat_escapes_all_payload_html(self):
        record = lab_module.preview_snapshot(guardian, self.lab.seed)
        record["rows"].append({
            "kind": "message",
            "user": '<img src=x onerror="alert(1)">',
            "time": "<script>",
            "text": '<script>alert("do not run")</script>',
            "index": 10,
        })
        markup = lab_module.render_chat_preview(
            record, "<img src=x onerror=alert(3)>", "ATTACK")
        self.assertIn("&lt;script&gt;", markup)
        self.assertIn("&lt;img", markup)
        self.assertNotIn("<script>", markup)
        self.assertNotIn('<img src=x onerror=', markup)
        self.assertIn("background:#0f172a", markup)

    def test_corrupted_snapshot_is_safely_previewed(self):
        snapshot = lab_module.preview_snapshot(guardian, b"\x1e\xff</style>")
        self.assertIsNotNone(snapshot["error"])
        markup = lab_module.render_chat_preview(snapshot, "Corrupted", "ATTACK")
        self.assertIn("INVALID / UNREADABLE CHAT", markup)
        self.assertNotIn("</style>", markup)

    def test_repeat_same_attack_resets_private_ledger(self):
        first = self.lab.test("verified")
        second = self.lab.test("verified")
        self.assertEqual(first["status"], "SECURITY GAP")
        self.assertEqual(second["status"], "SECURITY GAP")
        self.assertEqual(len(self.lab.previews), 1)
        self.assertEqual(len(self.lab.results), 1)

    def test_new_security_cases_cover_identity_color_spam_and_corruption(self):
        for case in ("verified_revoke", "rename_other",
                     "reaction_spoof", "reaction_remove"):
            with self.subTest(case=case):
                row = self.lab.test(case)
                self.assertEqual(row["status"], "SECURITY GAP")
        for case in ("name_zero_width", "name_confusable",
                     "color_section", "oversize_text", "room_flood",
                     "clock_orphan", "signed_forge", "signed_role",
                     "tamper_user", "erase_all", "null_event"):
            with self.subTest(case=case):
                row = self.lab.test(case)
                self.assertEqual(row["status"], "BLOCKED", row)
                self.assertEqual(row["after_records"],
                                 row["before_records"])
        for case in ("html_text", "script_text", "markdown_title"):
            with self.subTest(case=case):
                row = self.lab.test(case)
                self.assertEqual(row["status"], "ALLOWED TEXT", row)

    def test_partial_append_is_detected_and_preview_shows_broken_journal(self):
        row = self.lab.test("append_partial")
        self.assertEqual(row["status"], "DETECTED ONLY")
        self.assertIsNotNone(self.lab.previews["append_partial"]["attack"]["error"])
        self.assertFalse(row["restored"])

    def test_rollback_also_clears_chat_preview_state(self):
        self.lab.test("rename_other")
        self.assertTrue(self.lab.previews)
        self.lab.revert_all()
        self.assertFalse(self.lab.previews)

    def test_live_overlay_modded_menu_keeps_actual_chat_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            actual = Path(directory) / "chat.json"
            original = lab_module.seed_fixture(guardian)
            actual.write_bytes(original)
            attached = lab_module.LiveChatOverlay(guardian, actual)
            try:
                before = attached.original_preview()
                for hack in lab_module.LOCAL_HACKS:
                    attached.add_hack(hack)
                changed = attached.preview()
                self.assertEqual(actual.read_bytes(), original)
                self.assertNotEqual(before["rows"], changed["rows"])
                self.assertEqual(len(attached.actions), len(lab_module.LOCAL_HACKS))
                self.assertEqual(
                    (attached.root / "before_live_overlay.snapshot").read_bytes(),
                    original)
                attached.revert()
                self.assertEqual(actual.read_bytes(), original)
                self.assertEqual(attached.actions, [])
                self.assertEqual(attached.preview()["rows"],
                                 attached.original_preview()["rows"])
            finally:
                private_dir = attached.root
                attached.close()
            self.assertFalse(private_dir.exists())

    def test_live_mod_menu_renders_rainbow_badges_locally(self):
        with tempfile.TemporaryDirectory() as directory:
            actual = Path(directory) / "chat.json"
            original = lab_module.seed_fixture(guardian)
            actual.write_bytes(original)
            attached = lab_module.LiveChatOverlay(guardian, actual)
            try:
                attached.add_hack("rainbow")
                attached.add_hack("owner_badge")
                attached.add_hack("highlight")
                markup = lab_module.render_chat_preview(
                    attached.preview(), "Modded view", "ATTACK")
                self.assertIn("linear-gradient", markup)
                self.assertIn("OWNER (LOCAL)", markup)
                self.assertIn("background:#28334f", markup)
                self.assertEqual(actual.read_bytes(), original)
            finally:
                attached.close()

    def test_live_overlay_revert_preserves_concurrent_new_chat_messages(self):
        with tempfile.TemporaryDirectory() as directory:
            actual = Path(directory) / "chat.json"
            old = lab_module.seed_fixture(guardian)
            actual.write_bytes(old)
            attached = lab_module.LiveChatOverlay(guardian, actual)
            try:
                attached.add_hack("owner_badge")
                # Simulate another student appending a message after snapshot.
                changed_events = guardian.decode_shared_stream(old)
                changed_events.append({
                    "kind": "message", "time": "12:00:00",
                    "user": "Classmate", "text": "Keep this new message"})
                updated = json.dumps(changed_events).encode()
                actual.write_bytes(updated)
                attached.revert()
                self.assertEqual(actual.read_bytes(), updated)
                self.assertEqual(attached._baseline, old)
                self.assertEqual(attached.preview()["messages"], 3)
            finally:
                attached.close()

    def test_live_overlay_escapes_html_and_rejects_unknown_mods(self):
        with tempfile.TemporaryDirectory() as directory:
            actual = Path(directory) / "chat.json"
            original = lab_module.seed_fixture(guardian)
            actual.write_bytes(original)
            attached = lab_module.LiveChatOverlay(guardian, actual)
            try:
                with self.assertRaisesRegex(ValueError, "Unsupported"):
                    attached.add_hack("permanent_owner_privileges")
                attached.add_hack("ghost")
                attached.add_hack("nick")
                rendered = lab_module.render_chat_preview(
                    attached.preview(), "<script>alert(1)</script>", "ATTACK")
                self.assertNotIn("<script>", rendered)
                self.assertIn("&lt;script&gt;", rendered)
                self.assertEqual(actual.read_bytes(), original)
            finally:
                attached.close()

    def test_live_overlay_missing_source_never_creates_room(self):
        with tempfile.TemporaryDirectory() as directory:
            original = Path(directory) / "no_chat.json"
            with self.assertRaises(FileNotFoundError):
                lab_module.LiveChatOverlay(guardian, original)
            self.assertFalse(original.exists())

    def test_no_live_write_path_exists_in_modified_client(self):
        import inspect
        text = (ROOT / "tools" / "guardian_stress_lab.py").read_text()
        self.assertIn("LIVE_CHAT_READ_ONLY.read_bytes()", text)
        self.assertIn("guardian_stress_lab_", text)
        self.assertNotIn("LIVE_CHAT_READ_ONLY.write", text)
        self.assertNotIn("drive.mount", text)


if __name__ == "__main__":
    unittest.main()
