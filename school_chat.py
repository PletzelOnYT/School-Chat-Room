# @title Messaging App v5.36 — Compatibility Launcher
"""Backwards-compatible entry point for existing Google Colab notebooks.

The application was renamed to messaging_app.py. Keeping this lightweight
alias ensures existing users can still run their original GitHub launcher.
It does not use the old SchoolChatEvents folder.
"""
import urllib.request
import time

MESSAGING_APP_SOURCE = (
    'https://raw.githubusercontent.com/PletzelOnYT/'
    'School-Chat-Room/main/messaging_app.py'
)
# The query defeats raw.githubusercontent.com and Colab proxy caches.
# No private data is included in this URL.
_FRESH_MESSAGING_URL = MESSAGING_APP_SOURCE + '?cache_bust=' + str(time.time_ns())
with urllib.request.urlopen(
    urllib.request.Request(
        _FRESH_MESSAGING_URL,
        headers={'User-Agent': 'MessagingApp-Colab-Launcher',
                 'Cache-Control': 'no-cache'}),
    timeout=25,
) as _messaging_response:
    _messaging_code = _messaging_response.read().decode('utf-8')
exec(compile(_messaging_code, MESSAGING_APP_SOURCE, 'exec'), globals())
