# @title Owner Guardian v2.4 — Google Colab launcher
"""Owner-only one-cell launcher for the latest Drive Chat Guardian.

Uses the owner-provided Colab runtime and Google user authorization. The
Guardian itself mounts Drive and creates/reuses the original chat.json shortcut.
Never run this in every student's chat notebook: only one owner-side monitor
should repair the shared room at a time.
"""
import urllib.request

GUARDIAN_SOURCE_URL = (
    "https://raw.githubusercontent.com/PletzelOnYT/"
    "School-Chat-Room/main/owner_guardian.py"
)
with urllib.request.urlopen(
    urllib.request.Request(
        GUARDIAN_SOURCE_URL,
        headers={"User-Agent": "SchoolChat-OwnerGuardian-ColabLauncher",
                 "Cache-Control": "no-cache"}),
    timeout=25,
) as _guardian_response:
    _guardian_source = _guardian_response.read().decode("utf-8")

# Run as a module, not as __main__: Colab/Jupyter injects arguments like
# "-f /root/.../kernel-....json" into sys.argv, which argparse would reject.
# Passing an explicit empty list makes the owner's default launcher safe while
# leaving sys.argv and unrelated notebook settings untouched.
_guardian_globals = {"__name__": "_school_chat_owner_guardian",
                     "__file__": GUARDIAN_SOURCE_URL}
exec(compile(_guardian_source, GUARDIAN_SOURCE_URL, "exec"),
     _guardian_globals)
_guardian_globals["main"]([])

