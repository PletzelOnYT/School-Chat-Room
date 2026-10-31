# @title Messaging App — compatibility entry point
"""Legacy GitHub entry point; implementation moved to app/messaging_app.py.

Old Google Colab URLs and direct python launches continue working.
"""
from pathlib import Path as _Path
import time as _time
import urllib.request as _urllib_request

_impl_path = None
try:
    _possible = _Path(__file__).resolve().parent / "app/messaging_app.py"
    if _possible.is_file():
        _impl_path = _possible
except (NameError, OSError, TypeError):
    pass

if _impl_path is not None:
    _impl_bytes = _impl_path.read_bytes()
    _impl_label = str(_impl_path)
else:
    _impl_label = "https://raw.githubusercontent.com/PletzelOnYT/School-Chat-Room/main/app/messaging_app.py"
    _request = _urllib_request.Request(
        _impl_label + "?compat_refresh=" + str(_time.time_ns()),
        headers={"User-Agent": "SchoolChat-Compatibility-Launcher",
                 "Cache-Control": "no-cache"},
    )
    with _urllib_request.urlopen(_request, timeout=25) as _response:
        _impl_bytes = _response.read()
exec(compile(_impl_bytes.decode("utf-8"), _impl_label, "exec"), globals())
