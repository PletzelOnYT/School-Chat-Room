"""Auto shortcut setup must link original shared Drive file, never copy it."""
import ast
import os
import tempfile
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1].joinpath("app", "messaging_app.py").read_text()
TREE = ast.parse(SOURCE)
FUNC = next(n for n in TREE.body if isinstance(n, ast.FunctionDef)
            and n.name == "ensure_shared_chat_shortcut")
TARGET = "1CysiJWMvyW6AOYXtkAnf99JSIhAQHrMd"
CONTEXT = {
    "os": os,
    "time": __import__("time"),
    "SHARED_CHAT_FILE_ID": TARGET,
    "SHARED_CHAT_FILE_LINK": "https://drive.google.com/file/d/" + TARGET + "/view",
}
exec(compile(ast.Module(body=[FUNC], type_ignores=[]), "<drive-bootstrap>", "exec"),
     CONTEXT)
ensure = CONTEXT["ensure_shared_chat_shortcut"]


class Response:
    def __init__(self, status, payload):
        self.status_code = status
        self.payload = payload

    def json(self):
        return self.payload


class Session:
    def __init__(self, folder, *, state="create", target_status=200):
        self.folder = Path(folder)
        self.state = state
        self.target_status = target_status
        self.posts = []
        self.requests = []

    def get(self, url, params=None, timeout=None):
        self.requests.append(("get", url, params))
        if url.endswith("/" + TARGET):
            return Response(self.target_status,
                            {"id": TARGET, "name": "chat.json",
                             "mimeType": "application/json", "trashed": False})
        if self.state == "exists":
            return Response(200, {"files": [{
                "name": "chat.json",
                "mimeType": "application/vnd.google-apps.shortcut",
                "shortcutDetails": {"targetId": TARGET}
            }]})
        if self.state == "collision":
            return Response(200, {"files": [{
                "name": "chat.json", "mimeType": "application/json",
                "id": "not-the-original"
            }]})
        if self.state == "mixed":
            return Response(200, {"files": [
                {"name": "chat.json",
                 "mimeType": "application/vnd.google-apps.shortcut",
                 "shortcutDetails": {"targetId": TARGET}},
                {"name": "chat.json", "mimeType": "application/json",
                 "id": "other-room-copy"},
            ]})
        return Response(200, {"files": []})

    def post(self, url, json=None, params=None, timeout=None):
        self.posts.append(json)
        assert json["shortcutDetails"]["targetId"] == TARGET
        assert json["mimeType"] == "application/vnd.google-apps.shortcut"
        assert json["parents"] == ["root"]
        (self.folder / "chat.json").write_text("[]", encoding="utf-8")
        return Response(200, {
            "id": "shortcut-id",
            "shortcutDetails": {"targetId": TARGET},
        })


class ShortcutBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "chat.json"

    def test_existing_chat_is_reused_without_api_calls(self):
        self.path.write_text("[]")
        session = Session(self.tmp.name)
        self.assertEqual(ensure(self.tmp.name, session=session), str(self.path))
        self.assertEqual(session.requests, [])
        self.assertEqual(session.posts, [])

    def test_creates_only_original_target_shortcut_not_copy(self):
        session = Session(self.tmp.name)
        self.assertEqual(ensure(self.tmp.name, session=session,
                                sleep=lambda _: None), str(self.path))
        self.assertEqual(len(session.posts), 1)
        self.assertEqual(session.posts[0]["shortcutDetails"]["targetId"], TARGET)
        self.assertEqual(self.path.read_text(), "[]")

    def test_previous_shortcut_is_reused_after_mount_sync(self):
        session = Session(self.tmp.name, state="exists")
        def reveal(_):
            self.path.write_text("[]")
        self.assertEqual(ensure(self.tmp.name, session=session, sleep=reveal,
                                max_polls=2), str(self.path))
        self.assertEqual(session.posts, [])

    def test_rejects_different_existing_chat_filename(self):
        session = Session(self.tmp.name, state="collision")
        with self.assertRaisesRegex(FileNotFoundError, "different chat.json"):
            ensure(self.tmp.name, session=session, sleep=lambda _: None)
        self.assertEqual(session.posts, [])

    def test_mixed_correct_and_wrong_name_is_not_assumed_safe(self):
        session = Session(self.tmp.name, state="mixed")
        with self.assertRaisesRegex(FileNotFoundError, "different chat.json"):
            ensure(self.tmp.name, session=session, sleep=lambda _: None)
        self.assertFalse(self.path.exists())
        self.assertEqual(session.posts, [])

    def test_denied_original_access_never_creates_or_copies(self):
        session = Session(self.tmp.name, target_status=403)
        with self.assertRaisesRegex(FileNotFoundError, "cannot access"):
            ensure(self.tmp.name, session=session, sleep=lambda _: None)
        self.assertFalse(self.path.exists())
        self.assertEqual(session.posts, [])

    def test_unmounted_shortcut_requires_remount_not_new_copy(self):
        session = Session(self.tmp.name, state="exists")
        with self.assertRaisesRegex(FileNotFoundError, "remount Drive"):
            ensure(self.tmp.name, session=session, sleep=lambda _: None,
                   max_polls=2)
        self.assertEqual(session.posts, [])


if __name__ == "__main__":
    unittest.main()
