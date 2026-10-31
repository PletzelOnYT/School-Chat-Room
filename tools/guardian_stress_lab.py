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


# Live-attached mod menu: visual changes only. The original Google Drive
# file is never opened with write access. An older entire-file "restore"
# would overwrite messages that other classmates sent while the test ran.
LOCAL_HACKS = {
    "rainbow": "Rainbow-coloured name (local)",
    "owner_badge": "Show Owner badge beside a name (local)",
    "verified_badge": "Show Verified badge beside a name (local)",
    "nick": "Rename displayed sender to Hacker (local)",
    "timestamp": "Display fake 00:00:00 timestamp (local)",
    "mask": "Mask recent message content (local)",
    "highlight": "Highlight most recent message (local)",
    "reverse": "Reverse shown message order (local)",
    "ghost": "Overlay a fake chat message (local)",
    "reaction": "Overlay a fake reaction (local)",
    "hide": "Hide the last message from this preview (local)",
    "uppercase": "Transform last message to uppercase (local)",
}
assert len(LOCAL_HACKS) == len(set(LOCAL_HACKS))


class LiveChatOverlay:
    """Read actual shared chat, save its state in private TEMP, modify view only.

    This is NOT an authorized writer to the shared room. A complete binary
    rewind cannot safely coexist with other users and a running Guardian.
    Changes to an overlay are reversible immediately without a Drive write.
    """

    def __init__(self, guardian, source=None):
        self.guardian = guardian
        self.source = Path(source) if source is not None else LIVE_CHAT_READ_ONLY
        self.temp = tempfile.TemporaryDirectory(prefix="guardian_live_view_")
        self.root = Path(self.temp.name).resolve()
        if not self.root.is_relative_to(ALLOWED_ROOT) or "/content/drive" in str(self.root):
            self.temp.cleanup()
            raise RuntimeError("Unsafe location for a private snapshot")
        self._baseline = None
        self._current = None
        self.actions = []
        self.refresh()

    def refresh(self):
        """Only read the source; never write to the shared file or its parent."""
        if not self.source.is_file():
            raise FileNotFoundError(
                "The original chat.json is not mounted in this Colab session."
            )
        size = self.source.stat().st_size
        if size > self.guardian.MAX_FILE:
            raise ValueError("Live chat exceeds Guardian's safety size limit")
        raw = self.source.read_bytes()
        if len(raw) > self.guardian.MAX_FILE:
            raise ValueError("Live file exceeds Guardian's safety size limit")
        self.guardian.decode_shared_stream(raw)
        if self._baseline is None:
            self._baseline = bytes(raw)
            (self.root / "before_live_overlay.snapshot").write_bytes(raw)
        self._current = bytes(raw)
        return preview_snapshot(self.guardian, self._current, row_limit=18)

    def add_hack(self, name):
        if name not in LOCAL_HACKS:
            raise ValueError("Unsupported local visualization hack")
        self.actions.append(name)

    def preview(self):
        snapshot = preview_snapshot(self.guardian, self._current, row_limit=18)
        rows = snapshot["rows"]
        for action in self.actions:
            if action == "ghost":
                rows.append({
                    "kind": "message", "user": "FakeOwner", "time": "00:00:00",
                    "text": "👻 This message exists ONLY in your modded preview.",
                    "index": -1,
                })
            elif not rows:
                continue
            elif action == "reverse":
                rows.reverse()
            elif action == "hide":
                rows.pop()
            elif action == "mask":
                rows[-1]["text"] = "[hidden by local mod menu]"
            elif action == "uppercase":
                rows[-1]["text"] = str(rows[-1]["text"]).upper()
            elif action == "timestamp":
                rows[-1]["time"] = "00:00:00"
            elif action == "nick":
                rows[-1]["user"] = "Hacker"
            elif action == "owner_badge":
                rows[-1]["user"] += " [OWNER (LOCAL)]"
            elif action == "verified_badge":
                rows[-1]["user"] += " ✓ Verified (LOCAL)"
            elif action == "rainbow":
                rows[-1]["user"] += " 🌈 (LOCAL)"
            elif action == "highlight":
                rows[-1]["kind"] = "HIGHLIGHTED MESSAGE (LOCAL)"
            elif action == "reaction":
                rows[-1]["text"] += "  ⭐×999 (LOCAL)"
        snapshot["rows"] = rows
        # Message count here represents stored messages, never fake overlays.
        snapshot["overlay_count"] = len(self.actions)
        return snapshot

    def original_preview(self):
        return preview_snapshot(self.guardian, self._baseline, row_limit=18)

    def revert(self):
        """Undo every local visual hack; never rewind the live shared file."""
        self.actions.clear()
        self.refresh()
        return self.preview()

    def close(self):
        self.actions.clear()
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
    """Colab dashboard with read-only cloned-chat attack/repair preview."""
    import ipywidgets as widgets
    from IPython.display import display

    module = guardian if guardian is not None else obtain_guardian()
    current = {"lab": StressLab(module)}
    header = widgets.HTML(
        '<div style="background:#0b1220;padding:18px;border-radius:13px;'
        'border:1px solid #334155;color:#e2e8f0">'
        '<div style="font-size:19px;font-weight:800">🛡️ GUARDIAN RED-TEAM LAB</div>'
        '<div style="font-size:12px;color:#94a3b8;margin-top:6px">'
        'Modified-client attack simulator · Guardian v' +
        html.escape(module.GUARDIAN_VERSION) +
        ' · <b style="color:#4ade80">TEMPORARY COPIES ONLY</b></div>'
        '<div style="color:#fbbf24;font-size:11px;margin-top:5px">'
        'Preview shows a test clone, not the live chat UI. No real chat writes.'
        '</div></div>'
    )
    use_mirror = widgets.Checkbox(
        value=False,
        description="Read real chat once as a reference (copy stays in sandbox)",
        indent=False,
        layout=widgets.Layout(width="540px")
    )
    # The LIVE attachment changes what THIS notebook displays, not the
    # original shared Drive file. Snapshot is kept in private /tmp memory.
    live_switch = widgets.Checkbox(
        value=False, description="Attach mod menu to the actual chat (READ ONLY)",
        indent=False, layout=widgets.Layout(width="490px"))
    live_hack = widgets.Dropdown(
        options=[(label, name) for name, label in LOCAL_HACKS.items()],
        description="Visual hack:", layout=widgets.Layout(width="550px"))
    live_apply = widgets.Button(
        description="Apply to live view", icon="wand-magic-sparkles",
        button_style="info")
    live_refresh = widgets.Button(description="Refresh real chat",
                                  icon="refresh-cw")
    live_status = widgets.HTML(
        '<div style="color:#64748b">Not attached. '
        'Connect to inspect the real room without changing its data.</div>')
    live_before = widgets.HTML(layout=widgets.Layout(width="50%"))
    live_after = widgets.HTML(layout=widgets.Layout(width="50%"))
    live_comparison = widgets.HBox(
        [live_before, live_after], layout=widgets.Layout(
            width="100%", gap="9px", overflow_x="auto"))
    live_apply.disabled = True
    live_refresh.disabled = True

    def render_live():
        overlay = current.get("live")
        if overlay is None:
            live_before.value = ""
            live_after.value = ""
            live_status.value = (
                '<div style="color:#94a3b8">Disconnected from live chat.</div>')
            return
        before = overlay.original_preview()
        modified = overlay.preview()
        live_before.value = render_chat_preview(
            before, "Actual chat when attached", "BEFORE")
        live_after.value = render_chat_preview(
            modified, "Your local modded view", "ATTACK")
        live_status.value = (
            '<div style="color:#4ade80;font-size:12px">'
            '<b>LIVE CHAT ATTACHED — READ ONLY.</b> ' +
            str(modified["messages"]) + " messages; " +
            str(len(overlay.actions)) +
            ' local visual modifications. Source snapshot hash: <code>' +
            hashlib.sha256(overlay._baseline).hexdigest()[:16] +
            '</code>. Other people do not see these overlays.</div>')

    def live_toggle(change):
        if change.get("name") != "value":
            return
        with output:
            if change["new"]:
                try:
                    current["live"] = LiveChatOverlay(module)
                    live_apply.disabled = False
                    live_refresh.disabled = False
                    render_live()
                    print("Attached a read-only view of the actual chat. "
                          "Saved its starting bytes privately in /tmp.")
                except Exception as exc:
                    print("Could not attach:", type(exc).__name__, str(exc))
                    live_switch.unobserve(live_toggle, names="value")
                    live_switch.value = False
                    live_switch.observe(live_toggle, names="value")
            else:
                overlay = current.pop("live", None)
                if overlay is not None:
                    overlay.close()
                live_apply.disabled = True
                live_refresh.disabled = True
                render_live()
                print("Detached live viewer; temporary snapshot deleted.")

    def on_visual_hack(_):
        with output:
            overlay = current.get("live")
            if overlay is None:
                print("Attach to the live chat first.")
                return
            overlay.add_hack(live_hack.value)
            render_live()
            print("Applied", live_hack.value, "to this notebook's view ONLY.")

    def on_live_refresh(_):
        with output:
            overlay = current.get("live")
            if overlay is not None:
                try:
                    overlay.refresh()
                    render_live()
                    print("Refreshed real chat read-only; mods remain local.")
                except Exception as exc:
                    print("Live refresh error:", type(exc).__name__, str(exc))

    categories = widgets.Dropdown(
        options=["All categories", *ATTACK_GROUPS],
        value="All categories", description="Category:",
        layout=widgets.Layout(width="330px"),
    )
    case_picker = widgets.Dropdown(
        options=[(name, key) for key, name in ATTACK_LABELS.items()],
        description="Attack:",
        layout=widgets.Layout(width="610px"),
    )
    select_preview = widgets.Dropdown(
        options=[("Initial sandbox chat", "")],
        value="", description="Inspect:",
        layout=widgets.Layout(width="500px"),
    )
    run_one = widgets.Button(
        description="Launch selected attack", icon="shield",
        button_style="info", layout=widgets.Layout(width="245px")
    )
    run_category = widgets.Button(
        description="Run visible category", icon="bolt",
        button_style="warning", layout=widgets.Layout(width="245px")
    )
    run_all = widgets.Button(
        description="Run all " + str(len(ATTACK_LABELS)) + " attacks",
        icon="play", button_style="primary",
        layout=widgets.Layout(width="220px")
    )
    revert = widgets.Button(
        description="🟥 REVERT ALL TEST CHANGES",
        button_style="danger", icon="undo",
        tooltip="Restore and delete all temporary attack clones; never touch the live room",
        layout=widgets.Layout(width="280px", height="47px")
    )
    preview_status = widgets.HTML()
    initial = preview_snapshot(module, current["lab"].seed)
    before_card = widgets.HTML(layout=widgets.Layout(width="33%"))
    attack_card = widgets.HTML(layout=widgets.Layout(width="33%"))
    after_card = widgets.HTML(layout=widgets.Layout(width="33%"))
    preview_cards = widgets.HBox(
        [before_card, attack_card, after_card],
        layout=widgets.Layout(width="100%", gap="9px",
                              overflow_x="auto"),
    )
    output = widgets.Output(layout=widgets.Layout(
        border="1px solid #334155", max_height="390px", overflow="auto"
    ))

    def get_lab():
        return current["lab"]

    def update_inspector(selected=""):
        entries = [("Initial sandbox chat", "")]
        entries += [
            (ATTACK_LABELS[c] + " · " + status["status"], c)
            for c, status in sorted(get_lab().previews.items())
        ]
        select_preview.options = entries
        select_preview.value = selected if selected in get_lab().previews else ""

    def print_case(row):
        symbol = (
            "✅" if row["status"] in ("PASS", "BLOCKED", "ALLOWED TEXT")
            else "🟡" if row["status"] == "DETECTED ONLY" else "⚠️"
        )
        print("{} {:14s} {:48s} → {:19s}".format(
            symbol, row["status"], row["name"][:48],
            row["guardian_result"],
        ))

    def on_category(change):
        if change.get("name") != "value":
            return
        cat = change["new"]
        keys = ATTACK_GROUPS[cat] if cat != "All categories" else tuple(ATTACK_LABELS)
        case_picker.options = [(ATTACK_LABELS[c], c) for c in keys]

    def on_select_preview(change):
        if change.get("name") == "value":
            refresh_preview(change["new"])

    def mirror_toggle(change):
        if change.get("name") != "value":
            return
        with output:
            try:
                seed = read_live_snapshot_only() if change["new"] else None
                old = get_lab()
                new = StressLab(module, seed)
                old.close()
                current["lab"] = new
                nonlocal_initial[0] = preview_snapshot(module, new.seed)
                update_inspector()
                refresh_preview("")
                print("Source: read-only snapshot of original Drive chat, "
                      "written ONLY to a private temporary folder."
                      if change["new"] else
                      "Source: small synthetic private-room fixture.")
            except Exception as exc:
                print("Could not switch source:", type(exc).__name__, str(exc))
                use_mirror.unobserve(mirror_toggle, names="value")
                use_mirror.value = not change["new"]
                use_mirror.observe(mirror_toggle, names="value")

    nonlocal_initial = [initial]

    def refresh_preview(case=""):
        info = get_lab().previews.get(case)
        if info is None:
            before_card.value = render_chat_preview(
                nonlocal_initial[0], "Original test data", "BEFORE")
            empty = {"bytes": 0, "events": 0, "messages": 0, "rows": [],
                     "error": None, "raw_excerpt": ""}
            attack_card.value = render_chat_preview(
                empty, "Select an attack and run it", "ATTACK")
            after_card.value = render_chat_preview(
                nonlocal_initial[0], "Awaiting Guardian scan", "AFTER")
            preview_status.value = (
                '<div style="font-size:12px;color:#64748b">'
                'Select a case to see the cloned chat before, during, and after.'
                '</div>'
            )
            return
        preview_status.value = (
            '<div style="margin:9px 0;padding:10px;border-radius:8px;'
            'background:#1e293b;color:#e2e8f0;font-size:12px">'
            '<b>' + _visible_text(info["status"]) + '</b> — ' +
            _visible_text(info["note"]) + '</div>'
        )
        before_card.value = render_chat_preview(info["before"], "Before injection", "BEFORE")
        attack_card.value = render_chat_preview(info["attack"], "Modified client wrote", "ATTACK")
        after_card.value = render_chat_preview(info["after"], "Guardian checked", "AFTER")

    def on_one(_):
        with output:
            try:
                row = get_lab().test(case_picker.value)
                print_case(row)
                update_inspector(case_picker.value)
            except Exception as exc:
                print("Test error:", type(exc).__name__, str(exc))

    def execute_many(keys):
        enabled = (run_one, run_category, run_all)
        for button in enabled:
            button.disabled = True
        with output:
            print("Running", len(keys), "isolated attacks; live chat NEVER written.")
            counts = {}
            first_gap = None
            for i, case in enumerate(keys, 1):
                try:
                    row = get_lab().test(case)
                    counts[row["status"]] = counts.get(row["status"], 0) + 1
                    if first_gap is None and row["status"] in (
                        "SECURITY GAP", "DETECTED ONLY", "UNRESOLVED"):
                        first_gap = case
                    print(str(i) + "/" + str(len(keys)), end=" ")
                    print_case(row)
                except Exception as exc:
                    counts["ERROR"] = counts.get("ERROR", 0) + 1
                    print("❌ ERROR", case, type(exc).__name__, str(exc))
            chosen = first_gap or (keys[-1] if keys else "")
            update_inspector(chosen)
            print("RESULTS:", ", ".join(k + "=" + str(v)
                                       for k, v in sorted(counts.items())))
            print("This tests Guardian on local snapshots, not live Google "
                  "Drive race conditions or anti-copying protections.")
        for button in enabled:
            button.disabled = False

    def on_category_run(_):
        keys = ATTACK_GROUPS[categories.value] \
            if categories.value != "All categories" else tuple(ATTACK_LABELS)
        execute_many(keys)

    def on_all(_):
        execute_many(tuple(ATTACK_LABELS))

    def on_revert(_):
        with output:
            count = get_lab().revert_all()
            live_overlay = current.get("live")
            if live_overlay is not None:
                live_overlay.revert()
                render_live()
            update_inspector()
            refresh_preview("")
            print("🟥 REVERT COMPLETE:", count,
                  "isolated chat copies restored byte-for-byte and removed.")
            print("All local mod menu effects also reset. The original "
                  "Google Drive chat was never written.")

    live_switch.observe(live_toggle, names="value")
    live_apply.on_click(on_visual_hack)
    live_refresh.on_click(on_live_refresh)
    categories.observe(on_category, names="value")
    select_preview.observe(on_select_preview, names="value")
    use_mirror.observe(mirror_toggle, names="value")
    run_one.on_click(on_one)
    run_category.on_click(on_category_run)
    run_all.on_click(on_all)
    revert.on_click(on_revert)
    refresh_preview("")
    display(widgets.VBox([
        header,
        widgets.HTML('<b>MOD MENU — attach to actual chat, local changes only</b>'),
        live_switch, widgets.HBox([live_hack, live_apply, live_refresh]),
        live_status, live_comparison,
        widgets.HTML('<b>GUARDIAN ATTACK LAB — full simulation in test copies</b>'),
        use_mirror,
        widgets.HBox([categories, case_picker]),
        widgets.HBox([run_one, run_category, run_all]), revert,
        widgets.HTML('<b>CHAT PREVIEW — test data only</b>'),
        select_preview, preview_status, preview_cards,
        widgets.HTML('<b>Attack / Guardian test log</b>'), output,
    ], layout=widgets.Layout(gap="10px", width="100%")))
    with output:
        print("Guardian version:", module.GUARDIAN_VERSION)
        print("Attack scenarios:", len(ATTACK_LABELS))
        print(LIVE_NOTE)
        print("Use the red button to remove all attack test files.")
    current["refresh_preview"] = refresh_preview
    current["render_live"] = render_live
    current["preview_cards"] = preview_cards
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
