"""Offline regressions for the single root Chat Room Colab launcher."""
from pathlib import Path
from unittest.mock import patch
import unittest


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "launcher.py"
PINNED = "5c826601894edabbacf03fca98579898ba031ef1"


class FakeResponse:
    def __init__(self, data):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self.data


class LauncherTests(unittest.TestCase):
    def run_launcher(self, application_source):
        source = LAUNCHER.read_text(encoding="utf-8")
        requests = []

        def mock_urlopen(request, timeout):
            requests.append((request, timeout))
            return FakeResponse(application_source.encode("utf-8"))

        scope = {"__name__": "__main__"}
        with (patch("urllib.request.urlopen", side_effect=mock_urlopen),
              patch("time.time_ns", return_value=12345678)):
            exec(compile(source, str(LAUNCHER), "exec"), scope)
        return scope, requests

    def test_pinned_chat_starts_through_the_single_launcher(self):
        scope, requests = self.run_launcher(
            "CLIENT_VERSION = '5.60'\nCHAT_TEST_LAUNCHED = True\n"
        )
        self.assertTrue(scope["CHAT_TEST_LAUNCHED"])
        self.assertEqual(scope["EXPECTED_VERSION"], "5.60")
        self.assertEqual(scope["PINNED_COMMIT"], PINNED)
        self.assertEqual(len(requests), 1)
        request, timeout = requests[0]
        self.assertEqual(timeout, 30)
        self.assertIn(PINNED + "/app/messaging_app.py", request.full_url)
        self.assertIn("?fresh=12345678", request.full_url)
        self.assertEqual(request.get_header("Cache-control"), "no-cache")

    def test_rejects_wrong_version_before_running_downloaded_code(self):
        with self.assertRaisesRegex(RuntimeError, "Incorrect Chat Room source"):
            self.run_launcher(
                "CLIENT_VERSION = '5.59'\nSHOULD_NOT_RUN = True\n"
            )

    def test_only_launcher_is_top_level_python_entry_point(self):
        self.assertEqual(
            {p.name for p in ROOT.glob("*.py")}, {"launcher.py"}
        )
        source = LAUNCHER.read_text(encoding="utf-8")
        self.assertNotIn("/main/app/messaging_app.py", source)
        self.assertIn(PINNED, source)


if __name__ == "__main__":
    unittest.main()
