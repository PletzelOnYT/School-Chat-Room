"""Regressions for Colab Drive REST OAuth when ADC selects VM metadata."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_auto_shortcut import Session
from test_owner_guardian import guardian


class FakeCredentials:
    def __init__(self, name, valid=True, refresh_error=None):
        self.name = name
        self.valid = valid
        self.refresh_error = refresh_error
        self.refresh_count = 0

    def refresh(self, request):
        self.refresh_count += 1
        if self.refresh_error is not None:
            raise self.refresh_error
        self.valid = True


class GuardianColabOAuthTests(unittest.TestCase):
    def connect(self, default, login, is_machine=lambda _: False):
        return guardian.guardian_user_drive_session(
            get_default=default,
            login=login,
            make_request=lambda: object(),
            make_session=lambda c: {"authorized_as": c.name},
            is_machine=is_machine,
        )

    def test_existing_valid_user_oauth_requires_no_prompt(self):
        user = FakeCredentials("owner")
        login_calls = []
        result = self.connect(lambda: (user, None), lambda: login_calls.append(1))
        self.assertEqual(result["authorized_as"], "owner")
        self.assertEqual(login_calls, [])
        self.assertEqual(user.refresh_count, 0)

    def test_vm_service_account_triggers_normal_user_authorization(self):
        vm = FakeCredentials("colab-vm")
        user = FakeCredentials("owner", valid=False)
        state = {"authorized": False, "prompts": 0}

        def get_default():
            return (user if state["authorized"] else vm), None

        def login():
            state["prompts"] += 1
            state["authorized"] = True

        result = self.connect(get_default, login, lambda c: c is vm)
        self.assertEqual(result["authorized_as"], "owner")
        self.assertEqual(state["prompts"], 1)
        self.assertEqual(vm.refresh_count, 0)
        self.assertEqual(user.refresh_count, 1)

    def test_metadata_404_refresh_error_triggers_user_authorization(self):
        invalid = FakeCredentials(
            "bad ADC",
            valid=False,
            refresh_error=RuntimeError("metadata.google.internal: 404"),
        )
        user = FakeCredentials("approved owner")
        state = {"authorized": False}

        def default():
            return (user if state["authorized"] else invalid), None

        def approve():
            state["authorized"] = True

        result = self.connect(default, approve)
        self.assertEqual(result["authorized_as"], "approved owner")
        self.assertEqual(invalid.refresh_count, 1)
        self.assertTrue(state["authorized"])

    def test_school_account_denial_is_clear_and_never_bypassed(self):
        vm = FakeCredentials("VM")
        prompts = []

        def deny():
            prompts.append(1)
            raise PermissionError("Workspace policy blocks user authorization")

        with self.assertRaisesRegex(RuntimeError, "School account permissions"):
            self.connect(lambda: (vm, None), deny, lambda c: c is vm)
        self.assertEqual(prompts, [1])
        self.assertEqual(vm.refresh_count, 0)

    def test_real_shortcut_flow_uses_authorized_session_factory(self):
        with tempfile.TemporaryDirectory() as folder:
            session = Session(folder)
            target = Path(folder) / "chat.json"
            with patch.object(guardian, "guardian_user_drive_session",
                              return_value=session) as authorize:
                result = guardian.ensure_shared_chat_shortcut(
                    folder, sleep=lambda _: None)
            self.assertEqual(result, str(target))
            authorize.assert_called_once_with()
            self.assertEqual(len(session.posts), 1)
            self.assertEqual(session.posts[0]["mimeType"],
                             "application/vnd.google-apps.shortcut")


if __name__ == "__main__":
    unittest.main()
