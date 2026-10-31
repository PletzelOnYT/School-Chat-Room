# @title Owner Guardian Stress Lab — isolated modified-client attack simulator
"""Stress-test the actual School Chat Guardian without touching the live room.

The test client directly writes malicious records to PRIVATE TEMPORARY COPIES.
No commands in this script write to /content/drive or the original chat.json.
A red REVERT ALL button restores each cloned file to its exact starting bytes.

Run in Colab:
  import time, urllib.request
  u="https://raw.githubusercontent.com/PletzelOnYT/School-Chat-Room/main/guardian_stress_lab.py"
  exec(compile(urllib.request.urlopen(u+"?v="+str(time.time_ns())).read().decode(),u,"exec"))

Or run from a clone:
  python guardian_stress_lab.py --cli
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import html
import importlib.util
import json
import os
import sys
from pathlib import Path
import shutil
import tempfile
import time
import urllib.request
import unicodedata
import uuid

GUARDIAN_URL = (
    "https://raw.githubusercontent.com/PletzelOnYT/"
    "School-Chat-Room/main/guardian/owner_guardian.py"
)
LIVE_CHAT_READ_ONLY = Path("/content/drive/MyDrive/chat.json")
ALLOWED_ROOT = Path(tempfile.gettempdir()).resolve()
ATTACK_LABELS = {
    "normal": "Normal message (positive control)",
    "impersonate": "Impersonate an ordinary member (unsigned)",
    "verified": "Forge Verified title without authentication",
    "rename": "Forge a user's nickname mapping",
    "owner": "Claim Owner/Moderator authority",
    "red_title": "Try arbitrary colored/admin title",
    "ansi": "Inject terminal ANSI color into nickname",
    "html": "Inject HTML/CSS colored nickname",
    "bidi": "Inject right-to-left spoofed name",
    "invalid_clock": "Spoof a future timestamp sidecar",
    "invalid_time": "Send impossible 99:99:99 clock",
    "reaction": "React to nonexistent message number",
    "extra_fields": "Bypass client schema with hidden fields",
    "duplicate": "Duplicate-message spam burst",
    "spam": "Flood messages past rate limits",
    "rewrite": "Alter an approved historical message",
    "delete": "Delete the approved history",
    "reorder": "Reorder approved history",
    "corrupt": "Corrupt the JSON history",
    "verified_revoke": "Forge a Verified badge removal",
    "rename_other": "Rename somebody else's nickname",
    "reserved_name": "Impersonate reserved Owner username",
    "name_whitespace": "Use padded/spoofed nickname",
    "name_zero_width": "Insert invisible Unicode in nickname",
    "name_confusable": "Use Unicode confusable owner nickname",
    "name_emoji": "Insert emoji in nickname",
    "name_too_long": "Overflow nickname length",
    "color_section": "Inject section-sign color code in nickname",
    "color_field": "Inject unsupported hex color field",
    "html_text": "Insert HTML-tag payload in message text",
    "script_text": "Insert script-tag payload in text",
    "markdown_title": "Write apparent ADMIN text in message",
    "bidi_text": "Hide bidirectional control in message",
    "control_text": "Insert escape/control code in message",
    "blank_text": "Send whitespace-only message",
    "oversize_text": "Send message beyond Guardian text limit",
    "repeat_char": "Send repeated-character flood",
    "keyboard_flood": "Send patterned keyboard flood",
    "room_flood": "Flood room using rotating nicknames",
    "clock_orphan": "Forge clock without a linked message",
    "clock_mismatch": "Forge clock with mismatched username",
    "clock_old": "Send timestamp before supported era",
    "clock_extra": "Inject unauthorized timestamp metadata",
    "unknown_kind": "Write unsupported event kind",
    "profile_unsigned": "Forge unsigned identity profile",
    "signed_forge": "Forge signed message without private key",
    "signed_role": "Forge privileged signed role grant",
    "signed_replay": "Replay forged signed event ID",
    "reaction_spoof": "Forge reaction on existing message",
    "reaction_remove": "Erase someone else's reaction",
    "reaction_extra": "Add forbidden reaction style/color",
    "tamper_user": "Modify a historical sender",
    "erase_all": "Erase the entire message array",
    "append_partial": "Append incomplete journal record",
    "append_binary": "Append invalid UTF-8 bytes",
    "null_event": "Inject a null event into the archive",
    "array_event": "Inject a nested array event",
    "truncate": "Truncate JSON mid-record",
    "plain_json_concat": "Concatenate two JSON documents",
    "text_null": "Insert NUL byte in message text",
}
ATTACK_GROUPS = {
    "Identity & badges": (
        "impersonate", "verified", "rename", "owner", "verified_revoke",
        "rename_other", "reserved_name", "name_whitespace", "name_zero_width",
        "name_confusable", "name_emoji", "name_too_long", "profile_unsigned",
        "signed_role", "signed_forge", "signed_replay",
    ),
    "Color & presentation": (
        "red_title", "ansi", "html", "bidi", "color_section", "color_field",
        "html_text", "script_text", "markdown_title", "bidi_text", "control_text",
    ),
    "Spam & limits": (
        "duplicate", "spam", "room_flood", "blank_text", "oversize_text",
        "repeat_char", "keyboard_flood", "text_null",
    ),
    "Clocks & reactions": (
        "invalid_clock", "invalid_time", "reaction", "clock_orphan",
        "clock_mismatch", "clock_old", "clock_extra", "reaction_spoof",
        "reaction_remove", "reaction_extra",
    ),
    "History & file integrity": (
        "extra_fields", "rewrite", "delete", "reorder", "corrupt",
        "tamper_user", "erase_all", "append_partial", "append_binary",
        "null_event", "array_event", "truncate", "plain_json_concat",
        "unknown_kind",
    ),
    "Control": ("normal",),
}
if sorted(x for group in ATTACK_GROUPS.values() for x in group) != sorted(ATTACK_LABELS):
    raise RuntimeError("Every stress-test case must belong to exactly one group")
CASE_GROUP = {name: group for group, cases in ATTACK_GROUPS.items()
              for name in cases}
# These syntactically valid *unsigned* operations cannot be securely
# attributed to a Google account. Explicitly flag their acceptance.
EXPECTED_IDENTITY_GAPS = {
    "impersonate", "verified", "rename", "verified_revoke", "rename_other",
    "reaction_spoof", "reaction_remove",
}
# Text that looks privileged isn't itself proof of a privilege bypass.
BENIGN_TEXT_CASES = {"html_text", "script_text", "markdown_title"}
EXPECTED_BLOCKED = set(ATTACK_LABELS) - {"normal"}
LIVE_NOTE = (
    "This is a cloned-file test, NOT a live attack. "
    "The Guardian's loop and Google Drive FUSE behavior in a real Colab "
    "account cannot be proved by an offline simulation."
)


def _guardian_version(module):
    try:
        return tuple(int(part) for part in str(module.GUARDIAN_VERSION).split("."))
    except (AttributeError, ValueError, TypeError):
        return ()


def _load_guardian_source(raw, origin):
    """Load only Guardian source; do not accidentally run the monitoring loop."""
    import types
    name = "_school_chat_guardian_lab_" + uuid.uuid4().hex
    module = types.ModuleType(name)
    module.__file__ = str(origin)
    module.__package__ = ""
    exec(compile(raw, str(origin), "exec"), module.__dict__)
    return module


def obtain_guardian():
    """Use a fresh v2.7+ Guardian; never silently use a stale Colab copy.

    Executing a Python script via exec() in an IPython notebook preserves the
    notebook's globals, including a possibly stale __file__ / current directory.
    An older copy of owner_guardian.py there used to win over the updated GitHub
    source. In Colab always fetch. For local repository runs, use a colocated
    up-to-date Guardian; otherwise refresh from GitHub.
    """
    minimum = (2, 7)
    source_label = globals().get("__file__", "")
    script_path = (Path(str(source_label)).resolve()
                   if not str(source_label).startswith("https://") else None)
    colocated = (script_path is not None
                 and script_path.name == "guardian_stress_lab.py"
                 and script_path.is_file())
    in_colab = (bool(os.environ.get("COLAB_RELEASE_TAG")) or
                bool(os.environ.get("COLAB_GPU")) or
                # An imported google.colab module alone is not proof of a
                # notebook: local test runners can import it too.
                ("google.colab" in sys.modules and not colocated))
    local = None
    if colocated:
        # Source now lives in tools/, with Guardian's implementation in
        # guardian/. Root-level compatibility wrappers still work.
        candidates = (
            script_path.parent.parent / "guardian" / "owner_guardian.py",
            script_path.parent / "guardian" / "owner_guardian.py",
            script_path.parent / "owner_guardian.py",
        )
        local = next((p for p in candidates if p.is_file()), None)
    if local is not None and local.is_file() and not in_colab:
        module = _load_guardian_source(local.read_bytes(), str(local))
        if _guardian_version(module) >= minimum:
            print("Stress Lab: using local Guardian v" +
                  module.GUARDIAN_VERSION + ".", flush=True)
            return module
        print("Stress Lab: local Guardian v" +
              str(getattr(module, "GUARDIAN_VERSION", "unknown")) +
              " is outdated; fetching current source from GitHub.",
              flush=True)

    # Cache-busting alone may not beat every Colab/proxy cache. The GitHub
    # Contents API is the fallback if raw.githubusercontent.com serves an
    # old version. Never make a second room or alter any live Drive file.
    raw_url = GUARDIAN_URL + "?lab_fresh=" + str(time.time_ns())
    api_url = (
        "https://api.github.com/repos/PletzelOnYT/"
        "School-Chat-Room/contents/guardian/owner_guardian.py"
        "?ref=main&lab_fresh=" + str(time.time_ns())
    )
    attempts = []
    for source_url, label in ((raw_url, "GitHub raw"),
                              (api_url, "GitHub Contents API")):
        try:
            req = urllib.request.Request(
                source_url,
                headers={"User-Agent": "SchoolChat-GuardianStressLab",
                         "Cache-Control": "no-cache"},
            )
            with urllib.request.urlopen(req, timeout=25) as response:
                downloaded = response.read()
            if label == "GitHub Contents API":
                import base64
                payload = json.loads(downloaded.decode("utf-8"))
                if payload.get("encoding") != "base64":
                    raise RuntimeError("Unexpected GitHub content encoding")
                downloaded = base64.b64decode(payload["content"], validate=False)
            module = _load_guardian_source(downloaded, source_url)
            if _guardian_version(module) >= minimum:
                print("Stress Lab: loaded Guardian v" +
                      module.GUARDIAN_VERSION + " from " + label + ".",
                      flush=True)
                return module
            attempts.append(label + " returned old version " +
                            str(getattr(module, "GUARDIAN_VERSION", "unknown")))
        except Exception as exc:
            attempts.append(label + " unavailable: " + type(exc).__name__)
    raise RuntimeError(
        "Could not fetch Guardian v2.7+ (" + "; ".join(attempts) +
        "). Check GitHub access, then rerun the cache-busted lab cell. "
        "No chat files were changed."
    )

def seed_fixture(guardian):
    """Small *synthetic* history; no copying other people's messages by default."""
    history = [
        {"id": guardian.ANCHOR, "kind": "message",
         "user": "Archived", "text": "Original test baseline"},
        {"id": str(uuid.uuid4()), "kind": "message",
         "user": "Archived", "text": "Second test baseline record"},
    ]
    return (json.dumps(history, ensure_ascii=False, indent=2) + "\n").encode()


class ModifiedClient:
    """A simulated hacked client writing straight to a disposable file.

    Client-side cooldowns, role checks, and UI restrictions are deliberately
    bypassed so the real Guardian must validate the resulting records.
    """

    def __init__(self, target: Path, guardian):
        self.target = target
        self.guardian = guardian

    def overwrite_records(self, records):
        self.target.write_bytes(
            (json.dumps(records, ensure_ascii=False, indent=2) + "\n").encode()
        )

    def create(self, case: str):
        _, original = self.guardian.read_shared(self.target)
        records = copy.deepcopy(original)
        clock = time.strftime("%H:%M:%S", time.gmtime())
        def message(text, user="LabAttacker", time_value=clock):
            return {"kind": "message", "time": time_value,
                    "user": user, "text": text}
        if case == "normal":
            records.append(message("Hello from isolated lab"))
        elif case == "impersonate":
            records.append(message("I am the real Pletzel", user="Pletzel"))
        elif case == "verified":
            records.append({"kind": "title", "time": clock,
                            "user": "LabAttacker", "title": "Verified"})
        elif case == "rename":
            records.append({"kind": "rename", "time": clock,
                            "user": "LabAttacker", "name": "PurpleBoss"})
        elif case == "owner":
            records.append({"kind": "role", "time": clock, "role": "owner",
                            "user": "LabAttacker", "active": True})
        elif case == "red_title":
            records.append({"kind": "title", "time": clock,
                            "user": "LabAttacker", "title": "§cADMIN"})
        elif case == "ansi":
            records.append(message("Red name?", user="\x1b[31mLabAttacker"))
        elif case == "html":
            records.append(message(
                "CSS red name?", user='<span style="color:red">Owner</span>'
            ))
        elif case == "bidi":
            records.append(message("Bidi name?", user="Lab\u202eOwner"))
        elif case == "invalid_clock":
            stamp = int(time.time()) + 3600
            evt = message("Fake future timestamp",
                          time_value=time.strftime("%H:%M:%S", time.gmtime(stamp)))
            records.append(evt)
            records.append({"kind": "clock", "time": evt["time"],
                            "user": evt["user"], "epoch": stamp,
                            "message_hash": self.guardian.plain_message_hash(evt)})
        elif case == "invalid_time":
            records.append(message("Impossible timestamp", time_value="99:99:99"))
        elif case == "reaction":
            records.append({"kind": "reaction", "time": clock,
                            "user": "LabAttacker", "target": 9999999,
                            "emoji": "🔴", "active": True})
        elif case == "extra_fields":
            evt = message("An ordinary payload with forbidden fields")
            evt.update({"title": "Owner", "color": "#ff0000", "admin": True})
            records.append(evt)
        elif case == "duplicate":
            records += [message("identical test flood") for _ in range(9)]
        elif case == "spam":
            records += [message("test flood %03d" % i) for i in range(60)]
        elif case == "rewrite":
            records[0]["text"] = "HACKED historical message"
        elif case == "delete":
            records = records[1:]
        elif case == "reorder":
            records[0], records[1] = records[1], records[0]
        elif case == "corrupt":
            self.target.write_bytes(b"[this is not JSON")
            return
        elif case == "verified_revoke":
            records.append({"kind": "title", "time": clock,
                            "user": "Pletzel", "title": "None"})
        elif case == "rename_other":
            records.append({"kind": "rename", "time": clock,
                            "user": "Pletzel", "name": "LabBoss"})
        elif case == "reserved_name":
            records.append(message("Forged owner", user="Owner"))
        elif case == "name_whitespace":
            records.append(message("Padded name", user=" Pletzel "))
        elif case == "name_zero_width":
            records.append(message("Invisible name", user="Lab\u200bAttacker"))
        elif case == "name_confusable":
            records.append(message("Confusable owner", user="Ｐletzel"))
        elif case == "name_emoji":
            records.append(message("Emoji name", user="Lab🔴Attacker"))
        elif case == "name_too_long":
            records.append(message("Overlong name", user="Attacker" * 7))
        elif case == "color_section":
            records.append(message("Color-code name", user="§cOwner"))
        elif case == "color_field":
            records.append(dict(message("Inject hex color"), color="#ff0033"))
        elif case == "html_text":
            records.append(message('<b style="color:red">ADMIN</b>'))
        elif case == "script_text":
            records.append(message("<script>not executable in preview</script>"))
        elif case == "markdown_title":
            records.append(message("**[ADMIN]** I control this room"))
        elif case == "bidi_text":
            records.append(message("normal\u202eSECRET"))
        elif case == "control_text":
            records.append(message("hello\x1b[31mred"))
        elif case == "blank_text":
            records.append(message("  \n\t  "))
        elif case == "oversize_text":
            records.append(message("N" * (self.guardian.MAX_TEXT + 1)))
        elif case == "repeat_char":
            records.append(message("q" * 90))
        elif case == "keyboard_flood":
            records.append(message("asdfgh" * 12))
        elif case == "room_flood":
            records += [message("room flood #%d" % n, user="Lab%03d" % n)
                        for n in range(self.guardian.ROOM_RATE_60_SECONDS + 2)]
        elif case == "clock_orphan":
            records.append({"kind": "clock", "time": clock,
                            "user": "LabAttacker", "epoch": int(time.time()),
                            "message_hash": "a" * 64})
        elif case == "clock_mismatch":
            evt = message("Linked but wrong author")
            records.append(evt)
            records.append({"kind": "clock", "time": clock,
                            "user": "OtherUser", "epoch": int(time.time()),
                            "message_hash": self.guardian.plain_message_hash(evt)})
        elif case == "clock_old":
            records.append({"kind": "clock", "time": "00:00:00",
                            "user": "LabAttacker", "epoch": 42,
                            "message_hash": "b" * 64})
        elif case == "clock_extra":
            records.append({"kind": "clock", "time": clock,
                            "user": "LabAttacker", "epoch": int(time.time()),
                            "message_hash": "a" * 64, "color": "#ff0000"})
        elif case == "unknown_kind":
            records.append({"kind": "admin-panel", "time": clock, "user": "Lab"})
        elif case == "profile_unsigned":
            records.append({"id": str(uuid.uuid4()), "kind": "profile",
                            "user": "LabAttacker", "renamed": True})
        elif case in ("signed_forge", "signed_role"):
            import base64
            evt = {"id": str(uuid.uuid4()), "kind": "message",
                   "sender_key": base64.b64encode(b"X" * 32).decode(),
                   "sig": base64.b64encode(b"Y" * 64).decode(),
                   "issued_at": int(time.time()),
                   "time": clock, "user": "LabAttacker",
                   "text": "Forged authenticated message"}
            if case == "signed_role":
                evt.update({"kind": "role", "role": "verified",
                            "target_key": evt["sender_key"],
                            "active": True, "self_verified": True,
                            "verification_version": 2})
                del evt["text"]
            records.append(evt)
        elif case == "signed_replay":
            records.append({"id": records[1]["id"],
                            "kind": "message", "time": clock,
                            "user": "Replay", "text": "Reused approved ID"})
        elif case in ("reaction_spoof", "reaction_remove"):
            records.append({"kind": "reaction", "time": clock,
                            "user": "Pletzel", "target": 1,
                            "emoji": "⭐",
                            "active": case == "reaction_spoof"})
        elif case == "reaction_extra":
            records.append({"kind": "reaction", "time": clock,
                            "user": "LabAttacker", "target": 1,
                            "emoji": "⭐", "active": True,
                            "color": "#ff0000"})
        elif case == "tamper_user":
            records[0]["user"] = "HACKED_OWNER"
        elif case == "erase_all":
            records = []
        elif case == "append_partial":
            with self.target.open("ab") as stream:
                stream.write(b"\x1e{\"kind\":\"message\",\"time\":")
            return
        elif case == "append_binary":
            with self.target.open("ab") as stream:
                stream.write(b"\x1e\xff\xfe")
            return
        elif case == "null_event":
            records.append(None)
        elif case == "array_event":
            records.append([{"kind": "message", "text": "nested"}])
        elif case == "truncate":
            data = self.target.read_bytes()
            self.target.write_bytes(data[:max(1, len(data) // 2)])
            return
        elif case == "plain_json_concat":
            with self.target.open("ab") as stream:
                stream.write(b'{"kind":"message","text":"raw concatenation"}')
            return
        elif case == "text_null":
            records.append(message("lab\x00control"))
        else:
            raise ValueError("Unknown test: " + case)
        self.overwrite_records(records)


def _visible_text(value, limit=240):
    """Show hostile input as inert characters, never as runnable HTML."""
    src = str(value)
    printable = []
    for character in src[:limit]:
        if unicodedata.category(character).startswith("C"):
            printable.append(character.encode("unicode_escape").decode("ascii"))
        else:
            printable.append(character)
    result = "".join(printable)
    if len(src) > limit:
        result += " … (" + str(len(src)) + " chars)"
    return html.escape(result, quote=True)


def preview_snapshot(guardian, raw, row_limit=12):
    """Read only a cloned/seed snapshot and summarize it for the test UI.

    The preview uses no chat runtime or browser commands. All raw user text
    is escaped before it is displayed in ipywidgets.HTML.
    """
    data = bytes(raw)
    result = {
        "bytes": len(data), "events": 0, "messages": 0,
        "rows": [], "error": None, "raw_excerpt": "",
    }
    try:
        events = guardian.decode_shared_stream(data)
    except (UnicodeError, ValueError, TypeError) as exc:
        result["error"] = type(exc).__name__ + ": " + str(exc)
        result["raw_excerpt"] = data[:300].decode("utf-8", errors="replace")
        return result
    result["events"] = len(events)
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            result["rows"].append({
                "kind": "INVALID", "user": "raw event",
                "time": "—", "text": repr(event)[:300], "index": index + 1,
            })
            continue
        kind = str(event.get("kind", "message"))
        if kind == "message":
            result["messages"] += 1
            text = str(event.get("text", ""))
        elif kind == "title":
            text = "Badge / title: " + str(event.get("title", ""))
        elif kind == "rename":
            text = "Rename to: " + str(event.get("name", ""))
        elif kind == "reaction":
            text = "Reaction: " + str(event.get("emoji", "")) + " (target " + \
                   str(event.get("target", event.get("message_id", ""))) + ")"
        elif kind == "clock":
            continue
        else:
            text = str(event.get("text", event.get("role", "(metadata)")))
        result["rows"].append({
            "kind": kind, "user": str(event.get("user", "?")),
            "time": str(event.get("time", "—")),
            "text": text, "index": index + 1,
        })
    result["rows"] = result["rows"][-max(1, int(row_limit)):]
    return result


def render_chat_preview(snapshot, heading, status_label="SANDBOX"):
    """Read-only, HTML-escaped terminal-style preview of a chat snapshot."""
    highlight = {
        "BEFORE": "#38bdf8", "ATTACK": "#f97316", "AFTER": "#4ade80"
    }.get(status_label, "#a5b4fc")
    heading = _visible_text(heading, 90)
    details = (
        str(snapshot["messages"]) + " messages · " +
        str(snapshot["events"]) + " records · " +
        str(snapshot["bytes"]) + " bytes"
    )
    rows = []
    for entry in snapshot["rows"]:
        kind = _visible_text(entry["kind"], 26).upper()
        user = _visible_text(entry["user"], 55)
        stamp = _visible_text(entry["time"], 18)
        body = _visible_text(entry["text"], 220)
        style = "#94a3b8" if kind != "MESSAGE" else "#e5e7eb"
        rows.append(
            '<div style="padding:9px 0;border-bottom:1px solid #27364b;'
            'overflow-wrap:anywhere;font-size:12px;color:' + style + ';">'
            '<span style="color:#8ea2bf">[' + stamp + ']</span> '
            '<strong style="color:#93c5fd">' + user + '</strong>'
            '<span style="color:#c4b5fd"> · ' + kind + '</span>'
            '<div style="white-space:pre-wrap;margin-top:3px">'
            + body + '</div></div>'
        )
    if snapshot["error"]:
        rows.append(
            '<div style="color:#fecaca;padding:12px;overflow-wrap:anywhere">'
            '<b>INVALID / UNREADABLE CHAT</b><div>' +
            _visible_text(snapshot["error"]) + '</div>'
            '<pre style="white-space:pre-wrap;overflow-wrap:anywhere">' +
            _visible_text(snapshot["raw_excerpt"], 240) + '</pre></div>'
        )
    if not rows:
        rows.append('<div style="color:#94a3b8;padding:12px">No visible messages</div>')
    return (
        '<div style="font-family:ui-monospace,SFMono-Regular,Consolas,monospace;'
        'background:#0f172a;color:#e5e7eb;border:1px solid #334155;'
        'border-radius:12px;padding:12px;min-height:310px">'
        '<div style="font-size:11px;font-weight:700;color:' + highlight +
        ';letter-spacing:1px">' + _visible_text(status_label) + '</div>'
        '<div style="font-size:14px;font-weight:700;margin:6px 0">' +
        heading + '</div>'
        '<div style="font-size:10px;color:#94a3b8;margin-bottom:8px">' +
        html.escape(details) + '</div>'
        '<div style="max-height:385px;overflow:auto">' +
        ''.join(rows) + '</div></div>'
    )


class StressLab:
    """All writes stay in a unique temporary directory, never Google Drive."""

    def __init__(self, guardian, seed=None):
        self.guardian = guardian
        self.seed = bytes(seed) if seed is not None else seed_fixture(guardian)
        # Verify source *before* any experiments (including read-only mirror).
        with tempfile.TemporaryDirectory(prefix="guardian_seed_verify_") as verify:
            scratch = Path(verify) / "chat.json"
            scratch.write_bytes(self.seed)
            guardian.read_shared(scratch)
        self.temp = tempfile.TemporaryDirectory(prefix="guardian_stress_lab_")
        self.root = Path(self.temp.name).resolve()
        if not self.root.is_relative_to(ALLOWED_ROOT) or (
            "/content/drive" in str(self.root)
        ):
            raise RuntimeError("Unsafe experiment directory")
        self.cases = {}
        self.results = []
        self.previews = {}

    def _directory(self, case):
        if case not in ATTACK_LABELS:
            raise ValueError("Invalid lab case")
        destination = (self.root / "cases" / case).resolve()
        if not destination.is_relative_to(self.root):
            raise RuntimeError("Unsafe lab path")
        # A rerun must start from fresh ledger state. Otherwise an older
        # attack's private SQLite checkpoint can poison subsequent results.
        if destination.exists():
            if not destination.is_relative_to(self.root):
                raise RuntimeError("Unsafe temporary directory")
            shutil.rmtree(destination)
        destination.mkdir(parents=True, exist_ok=True)
        return destination

    def test(self, case):
        """One attack with its own baseline and private Guardian DB."""
        directory = self._directory(case)
        shared = directory / "chat.json"
        if shared.resolve() == LIVE_CHAT_READ_ONLY.resolve():
            raise RuntimeError("Refusing to write the live shared file")
        shared.write_bytes(self.seed)
        monitor = self.guardian.Guardian(
            shared, directory / "owner_private", directory / "local.db",
            drive_file_id="", repair=True
        )
        try:
            start = monitor.process()
            if start not in ("enrolled", "healthy"):
                raise RuntimeError("Test Guardian could not enroll: " + str(start))
            baseline = copy.deepcopy(self.guardian.read_shared(shared)[1])
            before = preview_snapshot(self.guardian, shared.read_bytes())
            attack = ModifiedClient(shared, self.guardian)
            attack.create(case)
            modified_bytes = shared.read_bytes()
            attacked = preview_snapshot(self.guardian, modified_bytes)
            injected = modified_bytes != self.seed
            outcome = monitor.process()
            after_snapshot = preview_snapshot(self.guardian, shared.read_bytes())
            try:
                _, after = self.guardian.read_shared(shared)
            except (OSError, ValueError):
                after = None
            restored = after == baseline
            approved = outcome in ("healthy", "enrolled") and not restored
            if case == "normal":
                passed = approved
                label = "PASS" if passed else "FAIL"
                note = "Legitimate message accepted by Guardian."
            elif outcome == "restored" and restored:
                passed = True
                label = "BLOCKED"
                note = "Guardian removed the injected changes from this clone."
            elif outcome in ("corrupt", "rescan", "needs_repair",
                             "migration_needs_baseline"):
                passed = False
                label = "DETECTED ONLY"
                note = "Guardian noticed an issue but the clone was not restored."
            elif case in BENIGN_TEXT_CASES and approved:
                passed = True
                label = "ALLOWED TEXT"
                note = "Text was accepted; this does NOT demonstrate code execution."
            elif case in EXPECTED_IDENTITY_GAPS and approved:
                passed = False
                label = "SECURITY GAP"
                note = "Unsigned user/title/reaction action accepted without identity proof."
            else:
                passed = False
                label = "SECURITY GAP" if approved else "UNRESOLVED"
                note = "Protection not confirmed. Inspect both snapshots and scan logs."
            row = {
                "case": case, "name": ATTACK_LABELS[case],
                "group": CASE_GROUP[case],
                "status": label, "guardian_result": str(outcome),
                "blocked": bool(passed) if case != "normal" else None,
                "restored": bool(restored),
                "modified_clone": bool(injected),
                "before_records": before["events"],
                "attack_records": attacked["events"],
                "after_records": after_snapshot["events"],
                "note": note,
            }
            self.previews[case] = {
                "before": before, "attack": attacked, "after": after_snapshot,
                "status": label, "note": note,
            }
            self.cases[case] = shared
            self.results = [r for r in self.results if r["case"] != case] + [row]
            return row
        finally:
            monitor.close()

    def run_all(self):
        return [self.test(case) for case in ATTACK_LABELS]

    def revert_all(self):
        """The RED button: byte-perfect rollback of every modified lab copy.

        The real Drive chat is never touched; local private test DBs and
        incident logs are deleted with the attack folders.
        """
        destination = self.root / "cases"
        reverted = 0
        for path in tuple(self.cases.values()):
            if path.is_file():
                path.write_bytes(self.seed)
                if path.read_bytes() != self.seed:
                    raise IOError("Could not verify sandbox rollback")
                reverted += 1
        if destination.exists():
            if not destination.resolve().is_relative_to(self.root):
                raise RuntimeError("Refusing to delete outside lab")
            shutil.rmtree(destination)
        self.cases.clear()
        self.results.clear()
        self.previews.clear()
        return reverted

    def close(self):
        self.revert_all()
        self.temp.cleanup()


def read_live_snapshot_only():
    """Read one original Drive snapshot; never write to it or its folder."""
    if not LIVE_CHAT_READ_ONLY.is_file():
        raise FileNotFoundError(
            "Original chat.json not mounted. Use synthetic tests or mount "
            "Drive in Colab first; no copy will be uploaded."
        )
    return LIVE_CHAT_READ_ONLY.read_bytes()


def launch_panel(guardian=None):
    """Interactive modified-client panel with a prominent red rollback button."""
    import ipywidgets as widgets
    from IPython.display import display

    module = guardian if guardian is not None else obtain_guardian()
    current = {"lab": StressLab(module)}
    status = widgets.HTML(
        "<b>OWNER GUARDIAN STRESS LAB</b> — synthetic local test copy. "
        "<span style='color:#b91c1c'><b>LIVE CHAT: NOT MODIFIED</b></span>"
    )
    use_mirror = widgets.Checkbox(
        value=False, description="Use live chat as READ-ONLY source (copy to lab)",
        indent=False, layout=widgets.Layout(width="460px")
    )
    case_picker = widgets.Dropdown(
        options=[(name, key) for key, name in ATTACK_LABELS.items()],
        description="Attack:",
        layout=widgets.Layout(width="530px")
    )
    run_one = widgets.Button(description="Test selected attack", icon="shield",
                             button_style="info")
    run_all = widgets.Button(description="Run full stress test", icon="play",
                             button_style="warning")
    revert = widgets.Button(
        description="🟥 REVERT ALL TEST CHANGES",
        button_style="danger", icon="undo",
        tooltip="Restore all local test copies byte-for-byte; never touches live Drive",
        layout=widgets.Layout(width="270px", height="43px")
    )
    output = widgets.Output(layout=widgets.Layout(
        border="1px solid #888", max_height="430px", overflow="auto"
    ))

    def get_lab():
        return current["lab"]

    def print_case(row):
        symbol = "✅" if row["status"] in ("PASS", "BLOCKED") else "⚠️"
        print("{} {:12s} {:50s} → {:15s} ({})".format(
            symbol, row["status"], row["name"],
            row["guardian_result"], "clone restored" if row["restored"] else "still changed"
        ))

    def mirror_toggle(change):
        if change["name"] != "value":
            return
        with output:
            try:
                seed = read_live_snapshot_only() if change["new"] else None
                old = get_lab()
                new = StressLab(module, seed)
                old.close()
                current["lab"] = new
                print("Loaded read-only live snapshot into a PRIVATE TEMP sandbox."
                      if change["new"] else "Reset to synthetic test fixture.")
            except Exception as exc:
                print("Could not switch source:", type(exc).__name__, str(exc))
                use_mirror.unobserve(mirror_toggle, names="value")
                use_mirror.value = not change["new"]
                use_mirror.observe(mirror_toggle, names="value")

    def on_one(_):
        with output:
            try:
                print_case(get_lab().test(case_picker.value))
            except Exception as exc:
                print("Test error:", type(exc).__name__, str(exc))

    def on_all(_):
        with output:
            print("Running", len(ATTACK_LABELS), "isolated attacks — no live writes.")
            ok = 0
            gaps = 0
            for case in ATTACK_LABELS:
                try:
                    row = get_lab().test(case)
                    print_case(row)
                    if row["status"] in ("PASS", "BLOCKED"):
                        ok += 1
                    else:
                        gaps += 1
                except Exception as exc:
                    gaps += 1
                    print("❌ ERROR", case, type(exc).__name__, str(exc))
            print("RESULT:", ok, "expected defenses/controls;", gaps,
                  "security gaps or errors.")
            print("NOTE: Name/title events can be untrusted in a public-editor file;"
                  " an offline watchdog cannot stop a participant from copying "
                  "public client code.")

    def on_revert(_):
        with output:
            count = get_lab().revert_all()
            print("🟥 REVERT ALL COMPLETE:", count,
                  "local chat clones restored byte-for-byte, then removed.")
            print("Original Google Drive chat.json: NEVER WRITTEN.")

    use_mirror.observe(mirror_toggle, names="value")
    run_one.on_click(on_one)
    run_all.on_click(on_all)
    revert.on_click(on_revert)
    display(widgets.VBox([
        status, use_mirror, case_picker,
        widgets.HBox([run_one, run_all]), revert, output,
    ]))
    with output:
        print("Guardian version:", module.GUARDIAN_VERSION)
        print(LIVE_NOTE)
        print("All attacks run against a fresh temp copy of chat.json.")
        print("The red button cleans up experiment files and incident logs.")
    return current


def main():
    parser = argparse.ArgumentParser(
        description="Safe, local Guardian modified-client security stress test"
    )
    parser.add_argument(
        "--cli", action="store_true",
        help="Run tests without Colab widgets, then auto-revert every local file"
    )
    parser.add_argument(
        "--mirror-readonly", action="store_true",
        help="Use mounted live chat as a read-only fixture; writes still stay local"
    )
    # Google Colab/Jupyter starts with -f kernel.json in sys.argv.
    # Never treat notebook kernel arguments as stress-lab commands.
    argv = [] if os.path.basename(sys.argv[0]).endswith("kernel_launcher.py") else None
    args = parser.parse_args(argv)
    guardian = obtain_guardian()
    if not args.cli:
        try:
            import ipywidgets  # noqa: F401
            launch_panel(guardian)
            return
        except ImportError:
            print("ipywidgets unavailable: running local CLI suite.")
    lab = StressLab(guardian,
                    read_live_snapshot_only() if args.mirror_readonly else None)
    try:
        for case in ATTACK_LABELS:
            result = lab.test(case)
            print(result["status"], case, "->", result["guardian_result"])
        failed = sum(r["status"] not in ("PASS", "BLOCKED")
                     for r in lab.results)
        print("SECURITY GAPS / UNEXPECTED RESULTS:", failed)
        print("Original chat.json was never written.")
    finally:
        lab.close()


if __name__ == "__main__":
    main()
