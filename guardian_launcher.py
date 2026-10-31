# @title Owner Guardian v2.6 — Google Colab launcher
"""Owner-only one-cell launcher for the latest Drive Chat Guardian.

Uses the owner-provided Colab runtime and Google user authorization. The
Guardian itself mounts Drive and creates/reuses the original chat.json shortcut.
Never run this in every student's chat notebook: only one owner-side monitor
should repair the shared room at a time.
"""
import urllib.request
import time

GUARDIAN_SOURCE_URL = (
    "https://raw.githubusercontent.com/PletzelOnYT/"
    "School-Chat-Room/main/owner_guardian.py"
)
# A unique query avoids an old GitHub raw response cached by Colab proxies.
# This URL still targets the same public source file; no user data is sent.
_FRESH_GUARDIAN_URL = GUARDIAN_SOURCE_URL + "?cache_bust=" + str(time.time_ns())
with urllib.request.urlopen(
    urllib.request.Request(
        _FRESH_GUARDIAN_URL,
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
_guardian_version = str(_guardian_globals.get("GUARDIAN_VERSION", "unknown"))
print("Loaded Owner Guardian v" + _guardian_version +
      " from GitHub. Checking Google Drive authorization now.", flush=True)
try:
    _version_parts = tuple(int(x) for x in _guardian_version.split("."))
except (ValueError, TypeError):
    _version_parts = ()
if _version_parts and _version_parts < (2, 6):
    raise RuntimeError(
        "Outdated Owner Guardian v" + _guardian_version +
        " was returned by GitHub's cache. Refresh the launcher using "
        "a cache-busting URL; do not run the old Guardian.")
_guardian_globals["main"]([])

