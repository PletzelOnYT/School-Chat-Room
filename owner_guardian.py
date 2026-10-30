# @title Messaging App — Owner Guardian (separate monitor)
"""Owner-side Google Drive watchdog. Does not run inside messaging_app.py.

- Approves only allowlisted, signed NEW events and stores a durable event ledger.
- Detects corrupted, truncated, replaced or deleted shared chat.json.
- Restores the approved history IN PLACE when possible; never forks the room.
- Preserves the initial pre-existing history as a one-time baseline (not proof
  that historical messages were safe).
- Stores private SQLite backups and incident logs in the OWNER'S My Drive.

This is a monitor and repairer, NOT preventive access control. Anyone with
Editor access can still edit the file between checks. Google Drive mount edits
are not transactional, so even repair operations can race with writers.
"""
import argparse
import base64
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import random
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.request
import uuid

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

ANCHOR = "081cad07-8430-4c0e-8000-f3a4a379536a"
MAX_FILE = 48 * 1024 * 1024
MAX_EVENT = 16 * 1024
MAX_TEXT = 1000
FIELDS = {
    "message": {"id","kind","sender_key","sig","issued_at","issued_at_ns",
                "time","user","account_username","text","reply_to"},
    "profile": {"id","kind","sender_key","sig","issued_at","issued_at_ns",
                "time","user","account_username","renamed"},
    "reaction": {"id","kind","sender_key","sig","issued_at","issued_at_ns",
                 "time","user","message_id","emoji","active"},
    "role": {"id","kind","sender_key","sig","issued_at","issued_at_ns",
             "time","user","role","target_key","active",
             "self_verified","unverify_anyone","verification_version"},
}
RESERVED = {"admin","administrator","moderator","mod","owner","verified",
            "system","server","support","schoolchat"}
SENSITIVE_CONTROL = re.compile(r"[\u202a-\u202e\u2066-\u2069]")
NAME_FORMAT = re.compile(r"^[\w .-]{1,32}$", re.UNICODE)


def canonical(obj):
    return json.dumps({k:v for k,v in obj.items() if k != "sig"},
                      sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


def event_id(event):
    return event.get("id") if isinstance(event, dict) else None


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical_event(obj):
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False,
                      sort_keys=True)


def signature_ok(event):
    if not isinstance(event, dict):
        return False
    try:
        public = base64.b64decode(event["sender_key"], validate=True)
        signature = base64.b64decode(event["sig"], validate=True)
        if len(public) != 32 or len(signature) != 64:
            return False
        Ed25519PublicKey.from_public_bytes(public).verify(signature,canonical(event))
        return True
    except Exception:
        return False


def valid_name(value):
    if not isinstance(value,str) or not NAME_FORMAT.fullmatch(value):
        return False
    if value.strip() != value or value.startswith((".", "-", "_")):
        return False
    if value.casefold() in RESERVED or "  " in value:
        return False
    return True


def invalid_text(text):
    if not isinstance(text,str) or not text.strip() or len(text)>MAX_TEXT:
        return "missing/oversized message"
    if any((ord(x)<32 and x not in "\n\t") or ord(x)==127 for x in text):
        return "control characters"
    if SENSITIVE_CONTROL.search(text):
        return "bidi control characters"
    raw=unicodedata.normalize("NFKC",text).casefold()
    if re.search(r"(.)\1{11,}",raw,re.S):
        return "repeated-character flood"
    if len(raw)>=24 and re.search(r"(.{2,12}?)\1{5,}",raw,re.S):
        return "repeated-pattern flood"
    if len(raw)>=36 and re.search(r"([a-z]{2,15}?)\1{3,}",
                                   re.sub(r"\W+", "", raw)):
        return "keyboard-pattern flood"
    return None


def check_new_event(event, accepted, rate_log, now=None):
    """Verify one NEW event against the v5.23 format and earlier accepted IDs.

    Returns a rejection reason or None. Existing baseline events are retained
    verbatim but do not allow unsafely structured new events to be inserted.
    """
    now = time.time() if now is None else now
    if not isinstance(event,dict):
        return "event is not an object"
    kind=event.get("kind")
    if kind not in FIELDS:
        return "unknown event kind"
    if len(canonical_event(event).encode("utf-8"))>MAX_EVENT:
        return "event too large"
    if set(event)-FIELDS[kind]:
        return "unapproved event fields"
    eid=event.get("id")
    try:
        uuid.UUID(eid)
    except (ValueError, TypeError, AttributeError):
        return "invalid event ID"
    if eid in accepted:
        return "duplicate event ID"
    if not signature_ok(event):
        return "missing or invalid Ed25519 signature"
    stamp=event.get("issued_at")
    if type(stamp) is not int or abs(now-stamp)>48*3600:
        return "invalid or stale timestamp"
    nanos=event.get("issued_at_ns")
    if nanos is not None and (type(nanos) is not int or
                              abs(nanos/1e9-stamp)>3):
        return "invalid nanosecond timestamp"
    if not isinstance(event.get("time"),str) or not re.fullmatch(
            r"\d{2}:\d{2}:\d{2}",event["time"]):
        return "invalid display timestamp"
    if not valid_name(event.get("user")):
        return "invalid display name"
    if "account_username" in event and not valid_name(event["account_username"]):
        return "invalid account name hint"
    sender=event["sender_key"]
    if kind=="message":
        problem=invalid_text(event.get("text"))
        if problem:
            return problem
        parent=event.get("reply_to")
        if parent is not None and (not isinstance(parent,str) or
                                   parent not in accepted or
                                   accepted[parent].get("kind")!="message"):
            return "reply target does not exist"
        history=rate_log.setdefault(sender,[])
        history[:]=[(ts,body) for ts,body in history if stamp-ts<=60]
        if sum(0<=stamp-ts<=10 for ts,_ in history)>=12:
            return "message rate exceeded (10s)"
        if sum(0<=stamp-ts<=60 for ts,_ in history)>=65:
            return "message rate exceeded (60s)"
        body=" ".join(event["text"].casefold().split())
        if sum(body==x and 0<=stamp-ts<=15 for ts,x in history)>=4:
            return "duplicate-message flood"
        history.append((stamp,body))
    elif kind=="profile":
        if "renamed" in event and event["renamed"] is not True:
            return "unsupported profile operation"
    elif kind=="reaction":
        if not isinstance(event.get("message_id"),str) or (
                event["message_id"] not in accepted or
                accepted[event["message_id"]].get("kind")!="message"):
            return "reaction target does not exist"
        emoji=event.get("emoji")
        if not isinstance(emoji,str) or not 1<=len(emoji)<=8 or not any(
                ord(ch)>=0x2600 for ch in emoji):
            return "invalid reaction emoji"
        if type(event.get("active")) is not bool:
            return "invalid reaction action"
    elif kind=="role":
        if event.get("role")!="verified" or event.get("verification_version")!=2:
            return "unapproved role change"
        target=event.get("target_key")
        try:
            if len(base64.b64decode(target,validate=True))!=32:
                return "invalid role target"
        except (TypeError,ValueError):
            return "invalid role target"
        active=event.get("active")
        if active is True:
            if (target!=sender or event.get("self_verified") is not True or
                    event.get("unverify_anyone") is not None):
                return "forged verification claim"
        elif active is False:
            if (event.get("unverify_anyone") is not True or
                    event.get("self_verified") is not None):
                return "invalid unverify action"
        else:
            return "invalid role action"
    return None


def read_shared(path):
    """Return bytes and events. Does not overwrite unreadable/corrupt files."""
    if not path.is_file():
        raise FileNotFoundError(str(path))
    if path.stat().st_size>MAX_FILE:
        raise ValueError("shared file exceeded safety size")
    raw=path.read_bytes()
    if len(raw)>MAX_FILE:
        raise ValueError("shared file exceeded safety size")
    try:
        entries=json.loads(raw)
    except (UnicodeDecodeError,json.JSONDecodeError) as exc:
        raise ValueError("shared JSON is invalid") from exc
    if not isinstance(entries,list):
        raise ValueError("chat.json must be a JSON array")
    if not any(isinstance(x,dict) and x.get("id")==ANCHOR for x in entries):
        raise ValueError("history anchor is missing; wrong or erased room")
    return raw,entries


def connect_db(path):
    path.parent.mkdir(parents=True,exist_ok=True)
    conn=sqlite3.connect(str(path),timeout=10)
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA synchronous=FULL")
    conn.executescript("""
      CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS approved(
        position INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id TEXT NOT NULL UNIQUE,
        kind TEXT, sender TEXT,
        json TEXT NOT NULL, sha256 TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS incidents(
        time TEXT NOT NULL, reason TEXT NOT NULL, event_id TEXT,
        sha256 TEXT, action TEXT NOT NULL);
    """)
    conn.commit()
    return conn


def db_events(conn):
    return [json.loads(row[0]) for row in conn.execute(
        "SELECT json FROM approved ORDER BY position")]


def count_events(conn):
    return conn.execute("SELECT COUNT(*) FROM approved").fetchone()[0]


def save_approved(conn, events):
    for event in events:
        line=canonical_event(event)
        conn.execute(
            "INSERT OR IGNORE INTO approved(event_id,kind,sender,json,sha256) "
            "VALUES(?,?,?,?,?)",
            (event["id"],event.get("kind"),event.get("sender_key"),
             line,digest(line.encode())))
    conn.commit()


def incident(conn, reason, event=None, action="quarantined"):
    eid=event_id(event)
    checksum=digest(canonical_event(event).encode("utf-8")) if event else None
    conn.execute("INSERT INTO incidents(time,reason,event_id,sha256,action)"
                 " VALUES(?,?,?,?,?)",
                 (dt.datetime.now(dt.timezone.utc).isoformat(),
                  reason,str(eid)[:90] if eid is not None else None,
                  checksum,action))
    conn.commit()


def safe_bytes(events):
    return (json.dumps(events,ensure_ascii=False,indent=2)+"\n").encode("utf-8")


def private_db_backup(conn, drive_directory):
    """Copy SQLite by SQLite's backup API; never copy a live SQLite journal."""
    drive_directory.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        local=Path(td)/"guardian-copy.sqlite3"
        dest=sqlite3.connect(str(local))
        try:
            conn.backup(dest)
        finally:
            dest.close()
        pending=drive_directory/"guardian.sqlite3.pending"
        shutil.copyfile(local,pending)
        os.replace(pending,drive_directory/"guardian.sqlite3")


def preserve_forensics(path, private_directory):
    """Keep current tampered bytes before recovery without changing shared file."""
    try:
        data=path.read_bytes()
        if len(data)>MAX_FILE:
            return
        location=private_directory/"incidents"
        location.mkdir(parents=True,exist_ok=True)
        name=f"incident-{int(time.time())}-{uuid.uuid4().hex[:6]}.bin"
        (location/name).write_bytes(data)
    except OSError:
        pass


def recover_existing(path, events, private_directory):
    """Restore only an EXISTING shared file/shortcut; never fork room identity."""
    if not path.is_file():
        raise FileNotFoundError("Original shared file/shortcut is absent")
    data=safe_bytes(events)
    preserve_forensics(path,private_directory)
    # No os.replace() on shared shortcuts: this could replace only the link.
    with open(path,"r+b") as fh:
        fh.seek(0)
        fh.write(data)
        fh.truncate()
        fh.flush()
        os.fsync(fh.fileno())
    raw,check=read_shared(path)
    if check!=events:
        raise IOError("recovery write could not be verified")
    return len(raw)


def restore_trash_by_id(file_id, timeout=8):
    """Best-effort restore SAME Drive file ID if trashed. NO new auth prompt.

    Requires already-granted Google credentials with sufficient Drive scope.
    If a Workspace policy denies this, do not work around it.
    """
    if not file_id:
        return "No Drive file ID configured; cannot untrash original file"
    try:
        import google.auth
        credentials,_=google.auth.default()
        token=getattr(credentials,"token",None)
        if not token or not getattr(credentials,"valid",False):
            return "No existing Drive API authorization (not requesting more scopes)"
        url=f"https://www.googleapis.com/drive/v3/files/{file_id}?fields=id,name,trashed,mimeType"
        headers={"Authorization":"Bearer "+token}
        req=urllib.request.Request(url,headers=headers)
        with urllib.request.urlopen(req,timeout=timeout) as response:
            item=json.load(response)
        if not item.get("trashed"):
            return "Original Drive ID is not marked as trashed; check shortcut/sync"
        req=urllib.request.Request(
            f"https://www.googleapis.com/drive/v3/files/{file_id}?fields=id,trashed",
            data=b'{"trashed":false}',method="PATCH",
            headers={**headers,"Content-Type":"application/json"})
        with urllib.request.urlopen(req,timeout=timeout) as response:
            updated=json.load(response)
        if updated.get("id")==file_id and updated.get("trashed") is False:
            return "Original Drive file restored from trash; wait for Drive mount sync"
        return "Drive restore attempt did not confirm untrash"
    except (OSError,ValueError,urllib.error.HTTPError) as exc:
        return "Drive API unavailable or denied: "+type(exc).__name__


class Guardian:
    def __init__(self,shared,private_directory,db_path,drive_file_id="",repair=True):
        self.shared=Path(shared)
        self.private=Path(private_directory)
        self.db_path=Path(db_path)
        self.drive_file_id=drive_file_id
        self.repair=repair
        self.private.mkdir(parents=True,exist_ok=True)
        if not self.db_path.exists():
            persisted=self.private/"guardian.sqlite3"
            if persisted.exists():
                shutil.copyfile(persisted,self.db_path)
        self.db=connect_db(self.db_path)
        self.last_missing_notice=0.0

    def log(self,msg):
        print("["+dt.datetime.now().strftime("%H:%M:%S")+"] "+msg,flush=True)

    def backup(self):
        private_db_backup(self.db,self.private)

    def process(self):
        """One monitoring pass; no destructive operations when history untrusted."""
        known=db_events(self.db)
        if not known:
            # First run: this existing snapshot is the TRUST BASELINE.
            # An owner should examine and backup chat.json before enrolling.
            raw,initial=read_shared(self.shared)
            seen=set()
            prepared=[]
            for index,event in enumerate(initial):
                if not isinstance(event,dict) or not isinstance(event_id(event),str):
                    raise ValueError("initial history contains an event without ID")
                if event_id(event) in seen:
                    raise ValueError("initial history contains duplicate IDs")
                seen.add(event_id(event))
                prepared.append(event)
            save_approved(self.db,prepared)
            self.backup()
            self.log(f"Baseline enrolled: {len(initial)} events, {len(raw)} bytes."
                     " Historical records grandfathered; new events enforced.")
            return "enrolled"
        expected={item["id"]:item for item in known}
        try:
            raw,live=read_shared(self.shared)
        except (FileNotFoundError,ValueError,OSError) as exc:
            # Read again after a grace delay; an ordinary Drive sync may be
            # exposing a half-written file at this moment.
            time.sleep(1.2)
            try:
                raw,live=read_shared(self.shared)
            except (FileNotFoundError,ValueError,OSError):
                reason="Missing or invalid shared chat.json: "+str(exc)
                incident(self.db,reason,action="restore_attempt")
                self.log("ALERT: "+reason)
                if isinstance(exc,FileNotFoundError):
                    result=restore_trash_by_id(self.drive_file_id)
                    self.log("Drive recovery: "+result)
                    # The shortcut may not reappear immediately after untrash.
                    return "missing"
                if self.repair:
                    recover_existing(self.shared,known,self.private)
                    self.log("Restored last approved chat history IN PLACE.")
                    return "restored"
                return "corrupt"

        # Compare known approved history against live file, even if someone
        # changed a prior signed event or deleted the entire chat array.
        observed=set()
        changed=False
        invalid=[]
        additions=[]
        ids=dict(expected)
        rate={}
        for entry in live:
            eid=event_id(entry)
            if not isinstance(eid,str) or eid in observed:
                changed=True
                invalid.append(("duplicate/malformed event ID",entry))
                continue
            observed.add(eid)
            if eid in expected:
                if canonical_event(expected[eid]) != canonical_event(entry):
                    changed=True
                    invalid.append(("approved history was altered",entry))
                continue
            reason=check_new_event(entry,ids,rate)
            if reason is not None:
                changed=True
                invalid.append((reason,entry))
                continue
            ids[eid]=entry
            additions.append(entry)

        missing=set(expected)-observed
        if missing:
            changed=True
        if invalid:
            for reason,event in invalid[:100]:
                incident(self.db,reason,event)
            self.log(f"ALERT: rejected {len(invalid)} unauthorized/invalid events")
        if missing:
            incident(self.db,f"{len(missing)} approved events deleted",action="restore_attempt")
            self.log(f"ALERT: {len(missing)} approved events disappeared")
        if additions:
            save_approved(self.db,additions)
            self.backup()
            self.log(f"Accepted {len(additions)} signed new events")
        if changed:
            if not self.repair:
                return "needs_repair"
            # Re-read to avoid clobbering a newer, valid concurrent save.
            # This is still a best-effort check, NOT a transaction.
            time.sleep(0.35+random.random()*0.25)
            try:
                fresh,_=read_shared(self.shared)
            except (OSError,ValueError):
                fresh=None
            if fresh is not None and fresh!=raw:
                self.log("Shared file changed during inspection; rescanning next pass")
                return "rescan"
            restored=db_events(self.db)
            recover_existing(self.shared,restored,self.private)
            incident(self.db,"Repaired invalid/deleted history",action="restored")
            self.backup()
            self.log(f"Recovered approved history ({len(restored)} records) IN PLACE")
            return "restored"
        return "healthy"

    def close(self):
        self.db.close()


def drive_root():
    for path in ("/content/drive/MyDrive","/content/drive/My Drive"):
        if os.path.isdir(path):
            return Path(path)
    raise RuntimeError("Google Drive not mounted. Mount it first in Colab.")


def main(argv=None):
    parser=argparse.ArgumentParser(description="Messaging App owner-only guardian")
    parser.add_argument("--file",help="Original shared chat.json shortcut path")
    parser.add_argument("--private",help="PRIVATE owner-only backup directory")
    parser.add_argument("--file-id",default="",help="Original Google Drive file ID for trash recovery")
    parser.add_argument("--interval",type=float,default=5.0,help="Seconds between scans")
    parser.add_argument("--once",action="store_true",help="Scan once and exit")
    parser.add_argument("--audit-only",action="store_true",help="Never modify shared chat.json")
    args=parser.parse_args(argv)
    root=drive_root() if not args.file or not args.private else None
    shared=Path(args.file) if args.file else root/"chat.json"
    private=Path(args.private) if args.private else root/".messaging_guardian"
    private.mkdir(parents=True,exist_ok=True)
    if args.file_id:
        (private/"original_file_id.txt").write_text(args.file_id.strip())
    else:
        file_id_file=private/"original_file_id.txt"
        if file_id_file.exists():
            args.file_id=file_id_file.read_text().strip()
    # SQLite runs locally, with every approved version checkpointed to private
    # Google Drive using the SQLite backup API. No SQLite lock on Drive FUSE.
    local_db=Path(tempfile.gettempdir())/"messaging_owner_guardian.sqlite3"
    guardian=Guardian(shared,private,local_db,args.file_id,not args.audit_only)
    try:
        print("OWNER GUARDIAN started; this process must remain running.",flush=True)
        print("Shared file:",shared,flush=True)
        print("PRIVATE backups:",private,flush=True)
        print("Mode:","AUDIT ONLY" if args.audit_only else "AUTO REPAIR",flush=True)
        while True:
            try:
                guardian.process()
            except Exception as exc:
                guardian.log("ERROR: "+type(exc).__name__+": "+str(exc))
            if args.once:
                break
            time.sleep(max(2.0,args.interval))
    except KeyboardInterrupt:
        guardian.log("Stopped by owner")
    finally:
        guardian.close()


if __name__=="__main__":
    main()
