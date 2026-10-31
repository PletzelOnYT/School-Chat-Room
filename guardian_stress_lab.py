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
import importlib.util
import json
import os
import sys
from pathlib import Path
import shutil
import tempfile
import time
import urllib.request
import uuid

GUARDIAN_URL = (
    "https://raw.githubusercontent.com/PletzelOnYT/"
    "School-Chat-Room/main/owner_guardian.py"
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
}
EXPECTED_BLOCKED = set(ATTACK_LABELS) - {"normal"}
LIVE_NOTE = (
    "This is a cloned-file test, NOT a live attack. "
    "The Guardian's loop and Google Drive FUSE behavior in a real Colab "
    "account cannot be proved by an offline simulation."
)


def obtain_guardian():
    """Load the current repository Guardian. Never load the chat client's code."""
    here = Path(__file__).resolve().parent if "__file__" in globals() else Path.cwd()
    local = here / "owner_guardian.py"
    if local.is_file():
        source_path = local
    else:
        # Public source only, and placed in Python's temporary directory.
        url = GUARDIAN_URL + "?test_cache_bust=" + str(time.time_ns())
        with urllib.request.urlopen(url, timeout=25) as response:
            raw = response.read()
        source_path = Path(tempfile.gettempdir()) / (
            "owner_guardian_lab_" + uuid.uuid4().hex + ".py"
        )
        source_path.write_bytes(raw)
    spec = importlib.util.spec_from_file_location(
        "_school_chat_guardian_lab_" + uuid.uuid4().hex, str(source_path)
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load Guardian source")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        version = tuple(int(x) for x in module.GUARDIAN_VERSION.split("."))
    except (AttributeError, ValueError):
        raise RuntimeError("Unsupported Guardian version") from None
    if version < (2, 7):
        raise RuntimeError("Guardian v2.7 or later is required for this lab")
    return module


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
        else:
            raise ValueError("Unknown test: " + case)
        self.overwrite_records(records)


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

    def _directory(self, case):
        if case not in ATTACK_LABELS:
            raise ValueError("Invalid lab case")
        destination = (self.root / "cases" / case).resolve()
        if not destination.is_relative_to(self.root):
            raise RuntimeError("Unsafe lab path")
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
            attack = ModifiedClient(shared, self.guardian)
            attack.create(case)
            injected = shared.read_bytes() != self.seed
            outcome = monitor.process()
            try:
                _, after = self.guardian.read_shared(shared)
            except (OSError, ValueError):
                after = None
            restored = after == baseline
            approved = outcome in ("healthy", "enrolled") and not restored
            if case == "normal":
                passed = approved
                label = "PASS" if passed else "FAIL"
            else:
                passed = outcome == "restored" and restored
                label = "BLOCKED" if passed else "SECURITY GAP"
            row = {
                "case": case, "name": ATTACK_LABELS[case],
                "status": label, "guardian_result": str(outcome),
                "blocked": bool(passed) if case != "normal" else None,
                "restored": bool(restored),
                "modified_clone": bool(injected),
                "note": ("Expected legitimate write accepted" if case == "normal"
                         else "Test copy restored" if passed
                         else "Potential access-control gap: review Guardian and client."),
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
