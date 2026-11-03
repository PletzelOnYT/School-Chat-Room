# @title 💬 Chat Room — Google Colab launcher (tested v5.60)
"""Single root-level launcher for Chat Room.

Run this file in Google Colab. The actual chat application lives in
app/messaging_app.py, and this launcher loads the verified v5.60 revision.
To upgrade the app later, update PINNED_COMMIT and EXPECTED_VERSION together,
then run the launcher regression tests. Nothing here creates a new chat room.
"""

import time as _time
import urllib.request as _urllib_request

PINNED_COMMIT = "5c826601894edabbacf03fca98579898ba031ef1"
EXPECTED_VERSION = "5.60"
CHAT_URL = (
    "https://raw.githubusercontent.com/PletzelOnYT/School-Chat-Room/"
    + PINNED_COMMIT
    + "/app/messaging_app.py"
)

_request = _urllib_request.Request(
    CHAT_URL + "?fresh=" + str(_time.time_ns()),
    headers={
        "User-Agent": "SchoolChat-ColabLauncher",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    },
)

print("💬 Launching Chat Room v" + EXPECTED_VERSION + "...", flush=True)
with _urllib_request.urlopen(_request, timeout=30) as _response:
    _source = _response.read().decode("utf-8")

_version_line = "CLIENT_VERSION = '" + EXPECTED_VERSION + "'"
if _version_line not in _source:
    raise RuntimeError(
        "Incorrect Chat Room source returned by GitHub. "
        "No chat code was run; please check the pinned commit."
    )

print("✅ Verified Chat Room v" + EXPECTED_VERSION, flush=True)
exec(compile(_source, CHAT_URL, "exec"), globals())
