# @title Messaging App v5.24 — Compatibility Launcher
"""Backwards-compatible entry point for existing Google Colab notebooks.

The application was renamed to messaging_app.py. Keeping this lightweight
alias ensures existing users can still run their original GitHub launcher.
It does not use the old SchoolChatEvents folder.
"""
import urllib.request

MESSAGING_APP_SOURCE = (
    'https://raw.githubusercontent.com/PletzelOnYT/'
    'School-Chat-Room/main/messaging_app.py'
)
with urllib.request.urlopen(
    urllib.request.Request(
        MESSAGING_APP_SOURCE,
        headers={'User-Agent': 'MessagingApp-Colab-Launcher',
                 'Cache-Control': 'no-cache'}),
    timeout=25,
) as _messaging_response:
    _messaging_code = _messaging_response.read().decode('utf-8')
exec(compile(_messaging_code, MESSAGING_APP_SOURCE, 'exec'), globals())
