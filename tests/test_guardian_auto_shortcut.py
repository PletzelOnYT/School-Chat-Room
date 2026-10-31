"""Owner Guardian v2.2 shares the exact original room via Drive shortcuts."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_auto_shortcut import Session, TARGET
from test_owner_guardian import guardian


class GuardianAutoShortcutTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.shared = self.root / "chat.json"

    def test_guardian_reuses_existing_chat_without_drive_api(self):
        self.shared.write_text("[]", encoding="utf-8")
        session = Session(self.root)
        result = guardian.ensure_shared_chat_shortcut(str(self.root), session=session)
        self.assertEqual(result, str(self.shared))
        self.assertEqual(session.posts, [])
        self.assertEqual(session.requests, [])

    def test_guardian_creates_only_original_drive_shortcut(self):
        session = Session(self.root)
        result = guardian.ensure_shared_chat_shortcut(
            str(self.root), session=session, sleep=lambda _: None)
        self.assertEqual(result, str(self.shared))
        self.assertEqual(len(session.posts), 1)
        self.assertEqual(session.posts[0]["shortcutDetails"]["targetId"], TARGET)
        self.assertEqual(session.posts[0]["mimeType"],
                         "application/vnd.google-apps.shortcut")

    def test_guardian_reuses_existing_shortcut_after_sync(self):
        session = Session(self.root, state="exists")
        def reveal(_):
            self.shared.write_text("[]", encoding="utf-8")
        self.assertEqual(guardian.ensure_shared_chat_shortcut(
            str(self.root), session=session, sleep=reveal,
            max_polls=2), str(self.shared))
        self.assertEqual(session.posts, [])

    def test_guardian_does_not_replace_other_chat(self):
        for state in ("collision", "mixed"):
            with self.subTest(state=state):
                session = Session(self.root, state=state)
                with self.assertRaisesRegex(FileNotFoundError,
                                            "different chat.json"):
                    guardian.ensure_shared_chat_shortcut(
                        str(self.root), session=session, sleep=lambda _: None)
                self.assertEqual(session.posts, [])

    def test_guardian_does_not_copy_on_access_denied(self):
        session = Session(self.root, target_status=403)
        with self.assertRaisesRegex(FileNotFoundError, "cannot access"):
            guardian.ensure_shared_chat_shortcut(
                str(self.root), session=session, sleep=lambda _: None)
        self.assertEqual(session.posts, [])
        self.assertFalse(self.shared.exists())

    def test_guardian_main_automatically_enrolls_private_owner_backup(self):
        legacy = {"id": guardian.ANCHOR, "kind": "message",
                  "user": "Legacy", "text": "original archive"}
        original = json.dumps([legacy])
        self.shared.write_text(original, encoding="utf-8")
        with patch.object(guardian, "drive_root", return_value=self.root), \
             patch.object(guardian, "ensure_shared_chat_shortcut",
                          return_value=str(self.shared)) as bootstrap:
            guardian.main(["--once", "--audit-only"])
            bootstrap.assert_called_once_with(str(self.root))
        self.assertEqual(self.shared.read_text(), original)
        self.assertTrue((self.root / ".messaging_guardian" /
                         "guardian.sqlite3").exists())
        self.assertTrue((self.root / ".messaging_guardian" /
                         "historical_clock_audit.json").exists())
        self.assertFalse((self.root / "SchoolChatEvents").exists())

    def test_custom_file_and_private_path_skip_auto_setup(self):
        self.shared.write_text(json.dumps([{
            "id": guardian.ANCHOR, "kind": "message", "user": "Legacy",
            "text": "safe"
        }]), encoding="utf-8")
        private = self.root / "custom_private"
        with patch.object(guardian, "drive_root") as mount, \
             patch.object(guardian, "ensure_shared_chat_shortcut") as bootstrap:
            guardian.main(["--once", "--audit-only",
                           "--file", str(self.shared),
                           "--private", str(private)])
            bootstrap.assert_not_called()
            mount.assert_not_called()
        self.assertTrue((private / "guardian.sqlite3").exists())

    def test_owner_guarantees_same_target_as_client(self):
        self.assertEqual(guardian.SHARED_CHAT_FILE_ID, TARGET)
        self.assertEqual(guardian.GUARDIAN_VERSION, "2.6")


if __name__ == "__main__":
    unittest.main()
