"""Regression tests for Jupyter's injected -f kernel connection argument."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from test_owner_guardian import guardian


class Response:
    def __init__(self, data):
        self.data = data

    def read(self):
        return self.data.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


class ColabKernelArgTests(unittest.TestCase):
    def test_one_cell_launcher_does_not_pass_kernel_flags_to_guardian(self):
        repo = Path(__file__).resolve().parents[1]
        source = (repo / "guardian_launcher.py").read_text(encoding="utf-8")
        fake_guardian = "\n".join((
            "def main(argv=None):",
            "    global received_args",
            "    received_args = argv",
            "",
        ))
        kernel_argv = [
            "colab_kernel_launcher.py", "-f",
            "/root/.local/share/jupyter/runtime/kernel-sample.json",
        ]
        scope = {"__name__": "__main__", "__file__": "guardian_launcher.py"}
        with (
            patch.object(sys, "argv", kernel_argv[:]),
            patch("urllib.request.urlopen", return_value=Response(fake_guardian)),
        ):
            exec(compile(source, "guardian_launcher.py", "exec"), scope)
            self.assertEqual(scope["_guardian_globals"]["received_args"], [])
            self.assertNotEqual(
                scope["_guardian_globals"]["__name__"], "__main__"
            )
            self.assertEqual(sys.argv, kernel_argv)

    def test_guardian_main_ignores_jupyter_kernel_flags_by_default(self):
        with (
            patch.object(guardian.sys, "argv", [
                "/usr/local/lib/python3.13/site-packages/colab_kernel_launcher.py",
                "-f", "/root/.local/share/jupyter/runtime/kernel-example.json",
            ]),
            patch.object(
                guardian.argparse.ArgumentParser, "parse_args",
                side_effect=RuntimeError("intercept arguments"),
            ) as parse,
        ):
            with self.assertRaisesRegex(RuntimeError, "intercept arguments"):
                guardian.main()
            parse.assert_called_once_with([])

    def test_explicit_owner_arguments_are_never_silently_discarded(self):
        with (
            patch.object(guardian.sys, "argv", [
                "colab_kernel_launcher.py", "-f", "kernel.json",
            ]),
            patch.object(
                guardian.argparse.ArgumentParser, "parse_args",
                side_effect=RuntimeError("intercept arguments"),
            ) as parse,
        ):
            with self.assertRaisesRegex(RuntimeError, "intercept arguments"):
                guardian.main(["--once", "--audit-only"])
            parse.assert_called_once_with(["--once", "--audit-only"])

    def test_terminal_owner_arguments_remain_supported(self):
        with (
            patch.object(guardian.sys, "argv", [
                "owner_guardian.py", "--once", "--audit-only",
            ]),
            patch.object(
                guardian.argparse.ArgumentParser, "parse_args",
                side_effect=RuntimeError("intercept arguments"),
            ) as parse,
        ):
            with self.assertRaisesRegex(RuntimeError, "intercept arguments"):
                guardian.main()
            parse.assert_called_once_with(None)


if __name__ == "__main__":
    unittest.main()
