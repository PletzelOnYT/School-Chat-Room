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
    def connect(self, default, login, is_machine=lambda _: False, **extras):
        return guardian.guardian_user_drive_session(
            get_default=default,
            login=login,
            make_request=lambda: object(),
            make_session=lambda c: {"authorized_as": c.name},
            is_machine=is_machine,
            **extras,
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

    def test_ephemeral_colab_user_token_can_use_gce_credentials_class(self):
        class ColabEphemeralCredentials(FakeCredentials):
            pass

        ColabEphemeralCredentials.__module__ = (
            "google.auth.compute_engine.credentials"
        )
        ephemeral = ColabEphemeralCredentials("authorized temporary user",
                                             valid=False)
        prompts = []
        result = self.connect(
            lambda: (ephemeral, None),
            lambda: prompts.append("login"),
            lambda _: True,
            ephemeral_mode=True,
        )
        self.assertEqual(result["authorized_as"], "authorized temporary user")
        self.assertEqual(prompts, [])
        self.assertEqual(ephemeral.refresh_count, 1)

    def test_colab_file_credentials_used_when_adc_stays_vm_after_login(self):
        import os
        vm = FakeCredentials("VM credentials")
        actual_user = FakeCredentials("Colab authorized user", valid=False)
        prompts = []
        with tempfile.TemporaryDirectory() as folder:
            adc = Path(folder) / "content/.adc/adc.json"
            adc.parent.mkdir(parents=True)
            adc.write_text('{"type":"authorized_user"}', encoding="utf-8")
            adc_paths = []

            def load(path):
                adc_paths.append(path)
                return actual_user

            with patch.dict(os.environ, {"DATALAB_ROOT": folder,
                                         "USE_AUTH_EPHEM": "0"}):
                result = self.connect(
                    lambda: (vm, None),
                    lambda: prompts.append("login"),
                    lambda cred: cred is vm,
                    colab_adc_loader=load,
                    ephemeral_mode=False,
                )
        self.assertEqual(result["authorized_as"], "Colab authorized user")
        self.assertEqual(prompts, [])
        self.assertEqual(adc_paths, [str(adc)])
        self.assertEqual(vm.refresh_count, 0)
        self.assertEqual(actual_user.refresh_count, 1)

    def test_file_credential_is_rejected_if_it_is_a_machine_account(self):
        import os
        vm = FakeCredentials("VM credentials")
        prompts = []
        with tempfile.TemporaryDirectory() as folder:
            adc = Path(folder) / "content/.adc/adc.json"
            adc.parent.mkdir(parents=True)
            adc.write_text('{"type":"service_account"}', encoding="utf-8")
            with patch.dict(os.environ, {"DATALAB_ROOT": folder,
                                         "USE_AUTH_EPHEM": "0"}):
                with self.assertRaisesRegex(RuntimeError, "Workspace restrictions"):
                    self.connect(
                        lambda: (vm, None),
                        lambda: prompts.append("login"),
                        lambda _: True,
                        colab_adc_loader=lambda _: vm,
                        ephemeral_mode=False,
                    )
        self.assertEqual(prompts, ["login"])

    def test_colab_authorization_prompts_only_after_owner_approval(self):
        events = []
        guardian.guardian_authorize_owner_colab(
            authorize=lambda: events.append("Google user consent"))
        self.assertEqual(events, ["Google user consent"])

    def test_colab_authorization_rejected_without_api_requests(self):
        def deny():
            raise PermissionError("school policy")

        with self.assertRaisesRegex(RuntimeError, "Add shortcut"):
            guardian.guardian_authorize_owner_colab(authorize=deny)

    def test_metadata_refresh_error_never_retried_as_machine_token(self):
        with tempfile.TemporaryDirectory() as folder:
            class BrokenSession:
                def get(self, url, params=None, timeout=None):
                    raise RuntimeError("metadata.google.internal: 404")

            with (
                patch.object(guardian, "guardian_authorize_owner_colab") as consent,
                patch.object(guardian, "guardian_user_drive_session",
                             return_value=BrokenSession()) as session,
            ):
                with self.assertRaisesRegex(FileNotFoundError,
                                            "Drive API could not complete"):
                    guardian.ensure_shared_chat_shortcut(
                        folder, sleep=lambda _: None)
                consent.assert_called_once_with()
                session.assert_called_once_with()

    def test_google_authorization_denial_never_starts_api_session(self):
        with tempfile.TemporaryDirectory() as folder:
            with (
                patch.object(guardian, "guardian_authorize_owner_colab",
                             side_effect=RuntimeError("school restrictions")) as consent,
                patch.object(guardian, "guardian_user_drive_session") as session,
            ):
                with self.assertRaisesRegex(FileNotFoundError,
                                            "school restrictions"):
                    guardian.ensure_shared_chat_shortcut(
                        folder, sleep=lambda _: None)
                consent.assert_called_once_with()
                session.assert_not_called()

    def test_school_account_denial_is_clear_and_never_bypassed(self):
        vm = FakeCredentials("VM")
        prompts = []

        def deny():
            prompts.append(1)
            raise PermissionError("Workspace policy blocks user authorization")

        with self.assertRaisesRegex(RuntimeError, "Workspace restrictions"):
            self.connect(lambda: (vm, None), deny, lambda c: c is vm)
        self.assertEqual(prompts, [1])
        self.assertEqual(vm.refresh_count, 0)

    def test_real_shortcut_flow_uses_authorized_session_factory(self):
        with tempfile.TemporaryDirectory() as folder:
            session = Session(folder)
            target = Path(folder) / "chat.json"
            with (
                patch.object(guardian, "guardian_authorize_owner_colab") as consent,
                patch.object(guardian, "guardian_user_drive_session",
                             return_value=session) as authorized_session,
            ):
                result = guardian.ensure_shared_chat_shortcut(
                    folder, sleep=lambda _: None)
            self.assertEqual(result, str(target))
            consent.assert_called_once_with()
            authorized_session.assert_called_once_with()
            self.assertEqual(len(session.posts), 1)
            self.assertEqual(session.posts[0]["mimeType"],
                             "application/vnd.google-apps.shortcut")


if __name__ == "__main__":
    unittest.main()
