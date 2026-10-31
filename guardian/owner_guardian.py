# @title Messaging App — Owner Guardian v3.1
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

GUARDIAN_VERSION = "3.1"
# Exact existing shared file; Guardian never copies the message archive.
SHARED_CHAT_FILE_ID = '1CysiJWMvyW6AOYXtkAnf99JSIhAQHrMd'
SHARED_CHAT_FILE_LINK = (
    'https://drive.google.com/file/d/'
    '1CysiJWMvyW6AOYXtkAnf99JSIhAQHrMd/view')
ANCHOR = "081cad07-8430-4c0e-8000-f3a4a379536a"
MAX_FILE = 48 * 1024 * 1024
MAX_EVENT = 16 * 1024
MAX_TEXT = 1000
# Conservative throttle windows; no ability to authenticate unsigned users.
RATE_10_SECONDS = 10
RATE_60_SECONDS = 45
ROOM_RATE_60_SECONDS = 140
REPEAT_20_SECONDS = 4
CLOCK_SKEW_SECONDS = 300
# Burst limits are measured at observation time; sender-provided dates are
# neither authoritative nor suitable for rate enforcement.
META_RATE_10_SECONDS = 18
META_RATE_60_SECONDS = 75
ROOM_META_RATE_60_SECONDS = 210
ROOM_RATE_10_SECONDS = 40
CROSS_NAME_REPEAT_20_SECONDS = 12
VALID_CLOCK = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d")
INVISIBLE_CONTROL = re.compile("[\u00ad\u180e\u200b\u2060\ufeff]")
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


def simple_message(event):
    """Only the four public message fields; unsigned by design."""
    return (isinstance(event,dict) and
            set(event)=={"kind","time","user","text"} and
            event.get("kind")=="message" and
            isinstance(event.get("time"),str) and
            VALID_CLOCK.fullmatch(event["time"]) is not None and
            isinstance(event.get("user"),str) and
            0<len(event["user"])<=128 and
            isinstance(event.get("text"),str) and
            len(event["text"])<=5000)


def simple_title(event):
    """Name-based Verified/None change. No identity proof is present."""
    return (isinstance(event, dict)
            and set(event) == {"kind", "time", "user", "title"}
            and event.get("kind") == "title"
            and isinstance(event.get("time"), str)
            and VALID_CLOCK.fullmatch(event["time"]) is not None
            and isinstance(event.get("user"), str)
            and 0 < len(event["user"]) <= 128
            and event.get("title") in ("Verified", "None"))


def simple_rename(event):
    """Simple four-key nickname mapping: original username -> new name."""
    return (isinstance(event, dict) and
            set(event) == {"kind", "time", "user", "name"} and
            event.get("kind") == "rename" and
            isinstance(event.get("time"), str) and
            VALID_CLOCK.fullmatch(event["time"]) is not None and
            isinstance(event.get("user"), str) and
            0 < len(event["user"]) <= 128 and
            valid_name(event.get("name")))


def simple_reaction(event):
    """Name-based emoji reaction referencing a previous message number."""
    emoji = event.get("emoji") if isinstance(event,dict) else None
    return (isinstance(event,dict)
            and set(event)=={"kind","time","user","target","emoji","active"}
            and event.get("kind")=="reaction"
            and isinstance(event.get("time"),str)
            and VALID_CLOCK.fullmatch(event["time"]) is not None
            and isinstance(event.get("user"),str)
            and 0<len(event["user"])<=128
            and type(event.get("target")) is int
            and 1<=event["target"]<=10000000
            and isinstance(emoji,str) and 1<=len(emoji)<=8
            and any(ord(ch)>=0x2600 for ch in emoji)
            and type(event.get("active")) is bool)


def simple_clock(event):
    """Separate four-field message timestamp evidence, never a message rewrite."""
    if not isinstance(event,dict) or set(event)!={"kind","time","user","epoch","message_hash"}:
        return False
    stamp=event.get("epoch")
    return (event.get("kind")=="clock" and
            isinstance(event.get("time"),str) and
            re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d",event["time"]) is not None and
            isinstance(event.get("user"),str) and
            0<len(event["user"])<=128 and type(stamp) is int and
            946684800<=stamp<=4102444800 and
            isinstance(event.get("message_hash"),str) and
            re.fullmatch(r"[0-9a-f]{64}",event["message_hash"]) is not None and
            time.strftime("%H:%M:%S",time.gmtime(stamp))==event["time"])


def plain_message_hash(event):
    """Same deterministic SHA-256 as the Colab chat client."""
    return hashlib.sha256(canonical_event(event).encode("utf-8")).hexdigest()


def event_id(event, position=None):
    """Storage index for private ledger, not written to public chat.json."""
    if (simple_message(event) or simple_title(event) or
            simple_rename(event) or simple_reaction(event) or
            simple_clock(event)) and position is not None:
        raw=canonical_event(event).encode("utf-8")
        return "plain-%d-%s" % (position,hashlib.sha256(raw).hexdigest()[:16])
    return event.get("id") if isinstance(event,dict) else None


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


def metadata_rate_limit(event, rate_log, now):
    """Detect floods of clocks, reactions, titles and identity metadata.

    Unsigned events have only a nickname. That is a rate bucket, NOT proof
    of identity, and cannot prevent all malicious Editor-access clients.
    """
    kind=event.get("kind")
    if kind not in ("clock","reaction","title","rename","profile","role"):
        return None
    sender=event.get("sender_key") or "display-name:"+event.get("user","").casefold()
    observed=float(now)
    history=rate_log.setdefault("__meta__:"+sender,[])
    history[:]=[(ts,body) for ts,body in history if 0<=observed-ts<=60]
    room=rate_log.setdefault("__all_metadata__",[])
    room[:]=[(ts,body) for ts,body in room if 0<=observed-ts<=60]
    if sum(0<=observed-ts<=10 for ts,_ in history)>=META_RATE_10_SECONDS:
        return "metadata burst from same sender"
    if len(history)>=META_RATE_60_SECONDS:
        return "metadata flood from same sender"
    if len(room)>=ROOM_META_RATE_60_SECONDS:
        return "room-wide metadata flood"
    history.append((observed,kind))
    room.append((observed,kind))
    return None


def valid_name(value):
    if not isinstance(value,str) or not NAME_FORMAT.fullmatch(value):
        return False
    if value.strip() != value or value.startswith((".", "-", "_")):
        return False
    if value.casefold() in RESERVED or "  " in value:
        return False
    if unicodedata.normalize("NFKC",value)!=value:
        return False
    if SENSITIVE_CONTROL.search(value) or any(
            unicodedata.category(ch).startswith("C") for ch in value):
        return False
    return True


def invalid_text(text):
    if not isinstance(text,str) or not text.strip() or len(text)>MAX_TEXT:
        return "missing/oversized message"
    if any((ord(x)<32 and x not in "\n\t") or ord(x)==127 for x in text):
        return "control characters"
    if SENSITIVE_CONTROL.search(text):
        return "bidi control characters"
    if INVISIBLE_CONTROL.search(text):
        return "invisible formatting characters"
    raw=unicodedata.normalize("NFKC",text).casefold()
    if re.search(r"(.)\1{11,}",raw,re.S):
        return "repeated-character flood"
    if len(raw)>=24 and re.search(r"(.{2,12}?)\1{5,}",raw,re.S):
        return "repeated-pattern flood"
    if len(raw)>=36 and re.search(r"([a-z]{2,15}?)\1{3,}",
                                   re.sub(r"\W+", "", raw)):
        return "keyboard-pattern flood"
    return None


def verified_by_history(sender, approved):
    """Replay only the v2 signed blue-check claims and revocations."""
    checked=set()
    for e in approved.values():
        if not isinstance(e,dict) or e.get("kind")!="role" or (
                e.get("role")!="verified" or e.get("verification_version")!=2 or
                not signature_ok(e)):
            continue
        target=e.get("target_key")
        if e.get("active") is True and e.get("self_verified") is True and (
                target==e.get("sender_key")):
            checked.add(target)
        elif e.get("active") is False and e.get("unverify_anyone") is True:
            checked.discard(target)
    return sender in checked


def check_new_event(event, accepted, rate_log, now=None):
    """Verify one NEW event against the v5.23 format and earlier accepted IDs.

    Returns a rejection reason or None. Existing baseline events are retained
    verbatim but do not allow unsafely structured new events to be inserted.
    """
    now = time.time() if now is None else now
    if not isinstance(event,dict):
        return "event is not an object"
    # Four-field chat messages deliberately have no signature or sender ID.
    # They can be checked for format and spam, NOT identity/authenticity.
    if simple_message(event):
        if not valid_name(event["user"]):
            return "invalid new display name"
        reason=invalid_text(event["text"])
        if reason:
            return reason
        sender="display-name:"+event["user"].casefold()
        history=rate_log.setdefault(sender,[])
        stamp=now
        history[:]=[(ts,body) for ts,body in history if stamp-ts<=60]
        normalized=" ".join(event["text"].casefold().split())
        if sum(0<=stamp-ts<=10 for ts,_ in history)>=RATE_10_SECONDS:
            return "message burst from same nickname"
        if len(history)>=RATE_60_SECONDS:
            return "message flood from same nickname"
        if sum(normalized==body and 0<=stamp-ts<=20
               for ts,body in history)>=REPEAT_20_SECONDS:
            return "duplicate-message flood"
        global_history=rate_log.setdefault("__room_messages__",[])
        global_history[:]=[(ts,body) for ts,body in global_history
                           if 0<=stamp-ts<=60]
        if len(global_history)>=ROOM_RATE_60_SECONDS:
            return "room-wide message flood"
        if sum(0<=stamp-ts<=10 for ts,_ in global_history)>=ROOM_RATE_10_SECONDS:
            return "room-wide message burst"
        if sum(normalized==body and 0<=stamp-ts<=20
               for ts,body in global_history)>=CROSS_NAME_REPEAT_20_SECONDS:
            return "cross-nickname duplicate flood"
        history.append((stamp,normalized))
        global_history.append((stamp,normalized))
        return None
    if simple_clock(event):
        if not valid_name(event["user"]):
            return "invalid clock author"
        if event["epoch"]>now+CLOCK_SKEW_SECONDS:
            return "clock timestamp lies in the future"
        matching=[previous for previous in accepted.values()
                  if simple_message(previous) and
                  plain_message_hash(previous)==event["message_hash"]]
        if not matching:
            return "clock references a missing chat message"
        if not any(previous["time"]==event["time"] and
                   previous["user"]==event["user"] for previous in matching):
            return "clock metadata disagrees with the linked message"
        return metadata_rate_limit(event,rate_log,now)
    if event.get("kind")=="clock":
        return "invalid or corrupted clock metadata"
    if simple_reaction(event):
        if not valid_name(event["user"]):
            return "invalid reaction actor"
        number_of_messages = sum(
            e.get("kind")=="message" for e in accepted.values()
            if isinstance(e,dict))
        if event["target"] > number_of_messages:
            return "reaction target does not exist"
        return metadata_rate_limit(event,rate_log,now)
    if simple_rename(event):
        if not valid_name(event["user"]):
            return "invalid rename source"
        return metadata_rate_limit(event,rate_log,now)
    if simple_title(event):
        # Title records are self-claimed name metadata, not proof of Google ID.
        # The Guardian validates only schema, not the actual user identity.
        if not valid_name(event["user"]):
            return "invalid title display name"
        return metadata_rate_limit(event,rate_log,now)
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
            VALID_CLOCK,event["time"]):
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
        observed=now
        history=rate_log.setdefault(sender,[])
        history[:]=[(ts,body) for ts,body in history if 0<=observed-ts<=60]
        if sum(0<=observed-ts<=10 for ts,_ in history)>=RATE_10_SECONDS:
            return "message rate exceeded (10s)"
        if sum(0<=observed-ts<=60 for ts,_ in history)>=RATE_60_SECONDS:
            return "message rate exceeded (60s)"
        body=" ".join(event["text"].casefold().split())
        if sum(body==x and 0<=observed-ts<=20 for ts,x in history)>=REPEAT_20_SECONDS:
            return "duplicate-message flood"
        history.append((observed,body))
    elif kind=="profile":
        if "renamed" in event:
            if event["renamed"] is not True:
                return "unsupported profile operation"
            if not verified_by_history(sender,accepted):
                return "nickname change without current verified status"
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
    return metadata_rate_limit(event,rate_log,now)


APPEND_RECORD_SEPARATOR = '\x1e'


def decode_shared_stream(raw):
    """Read original JSON array plus appended RS-delimited JSON records."""
    try:
        data=raw.decode('utf-8-sig')
    except UnicodeError as exc:
        raise ValueError("shared chat contains invalid UTF-8") from exc
    def unique_keys(pairs):
        result={}
        for key,value in pairs:
            if key in result:
                raise ValueError("duplicate JSON object key: "+str(key)[:60])
            result[key]=value
        return result
    decoder=json.JSONDecoder(
        object_pairs_hook=unique_keys,
        parse_constant=lambda token: (_ for _ in ()).throw(
            ValueError("non-finite JSON numeric constant: "+token)))
    skip=len(data)-len(data.lstrip(" \t\r\n"))
    try:
        history,pos=decoder.raw_decode(data,skip)
    except ValueError as exc:
        raise ValueError("shared chat does not start with JSON history") from exc
    if not isinstance(history,list):
        raise ValueError("chat history must begin with a JSON array")
    events=list(history)
    while True:
        pos+=len(data[pos:])-len(data[pos:].lstrip(" \t\r\n"))
        if pos==len(data):
            return events
        if data[pos]!=APPEND_RECORD_SEPARATOR:
            raise ValueError("unexpected bytes after chat history")
        try:
            event,pos=decoder.raw_decode(data,pos+1)
        except ValueError as exc:
            raise ValueError("incomplete append-only chat record") from exc
        if not isinstance(event,dict):
            raise ValueError("invalid append-only event")
        events.append(event)


def read_shared(path):
    """Read the original history and append-only records without changing it."""
    if not path.is_file():
        raise FileNotFoundError(str(path))
    if path.stat().st_size>MAX_FILE:
        raise ValueError("shared file exceeded safety size")
    raw=path.read_bytes()
    if len(raw)>MAX_FILE:
        raise ValueError("shared file exceeded safety size")
    entries=decode_shared_stream(raw)
    legacy_anchor=any(isinstance(x,dict) and x.get("id")==ANCHOR for x in entries)
    simplified=all(simple_message(x) or simple_title(x) or
                   simple_rename(x) or simple_reaction(x) or
                    simple_clock(x) for x in entries)
    if not legacy_anchor and not simplified:
        raise ValueError("file is neither legacy history nor simplified chat")
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
    # Upgrade existing Guardian backups in place; no baseline reset.
    existing={row[1] for row in conn.execute("PRAGMA table_info(approved)")}
    if "observed_at" not in existing:
        conn.execute("ALTER TABLE approved ADD COLUMN observed_at REAL")
    conn.commit()
    return conn


class GuardianLedgerError(RuntimeError):
    """Fail closed when the private approved-history ledger is untrustworthy."""


class ConcurrentChatChange(RuntimeError):
    """The shared file changed during recovery; rescan instead of overwriting."""


def db_events(conn):
    """Read verified approved events; never repair from a corrupted checkpoint.

    The SQLite file is owner-private, but damaged disks, interrupted backups
    or accidental database edits can corrupt records. The stored SHA-256
    authenticates each row against accidental changes, not a malicious actor
    with access to BOTH the database and its hashes.
    """
    events=[]
    for event_key,kind,sender,raw_json,expected_sha in conn.execute(
        "SELECT event_id,kind,sender,json,sha256 "
        "FROM approved ORDER BY position"
    ):
        try:
            event=json.loads(raw_json)
            if not isinstance(event,dict):
                raise ValueError("ledger event is not an object")
            if digest(raw_json.encode("utf-8")) != expected_sha:
                raise ValueError("ledger checksum mismatch")
            if canonical_event(event) != raw_json:
                raise ValueError("ledger event is not canonical")
            if event_key!=event_id(event,len(events)):
                raise ValueError("ledger event ID/position mismatch")
            if kind!=event.get("kind") or sender!=event.get("sender_key"):
                raise ValueError("ledger metadata mismatch")
        except (TypeError,ValueError,UnicodeError) as exc:
            raise GuardianLedgerError(
                "Owner Guardian's private approved-history checkpoint failed "
                "integrity verification at record " + str(len(events)+1) +
                ". Repairs are disabled until the owner restores or inspects "
                "their PRIVATE backup. Never reset the baseline or overwrite "
                "the shared chat to resolve this.") from exc
        events.append(event)
    return events


def count_events(conn):
    return conn.execute("SELECT COUNT(*) FROM approved").fetchone()[0]


def save_approved(conn, events, baseline=False):
    start=count_events(conn)
    for offset,event in enumerate(events):
        line=canonical_event(event)
        conn.execute(
            "INSERT OR IGNORE INTO approved(event_id,kind,sender,json,sha256,observed_at) "
            "VALUES(?,?,?,?,?,?)",
            (event_id(event,start+offset),event.get("kind"),event.get("sender_key"),
             line,digest(line.encode()),None if baseline else time.time()))
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
    """Publish a validated SQLite backup without touching the existing backup first."""
    # Refuse to copy a damaged approved-history ledger over the last good
    # private backup. This does not authenticate against a malicious owner.
    db_events(conn)
    drive_directory.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        local=Path(td)/"guardian-copy.sqlite3"
        dest=sqlite3.connect(str(local))
        try:
            conn.backup(dest)
            if dest.execute("PRAGMA quick_check").fetchone()[0]!="ok":
                raise GuardianLedgerError("SQLite backup integrity check failed")
            db_events(dest)
        finally:
            dest.close()
        pending=drive_directory/"guardian.sqlite3.pending"
        try:
            shutil.copyfile(local,pending)
            verify=sqlite3.connect(str(pending))
            try:
                if verify.execute("PRAGMA quick_check").fetchone()[0]!="ok":
                    raise GuardianLedgerError("Pending backup integrity check failed")
                db_events(verify)
            finally:
                verify.close()
            os.replace(pending,drive_directory/"guardian.sqlite3")
        finally:
            # An interrupted or failed backup should not leave an orphan copy.
            try:
                pending.unlink(missing_ok=True)
            except OSError:
                pass


def write_health_status(private_directory, status, *, started_at, scans,
                        errors, last_success, approved, bytes_on_disk,
                        running=True, mode="AUTO REPAIR"):
    """Atomic, minimal owner-private health indicator for 24/7 monitoring.

    No chat text, OAuth data, or user identifiers are written to the status
    JSON. A stale timestamp means the Colab monitor is no longer checking.
    """
    directory=Path(private_directory)
    directory.mkdir(parents=True,exist_ok=True)
    status_doc={
        "schema":1, "guardian_version":GUARDIAN_VERSION,
        "checked_at_utc":dt.datetime.now(dt.timezone.utc).isoformat(),
        "started_at_utc":started_at,
        "last_success_utc":last_success,
        "status":str(status)[:80],
        "running":bool(running), "mode":mode,
        "scans":int(scans), "consecutive_errors":int(errors),
        "approved_records":approved,
        "shared_bytes":bytes_on_disk,
        "note":"A recent heartbeat confirms monitoring only, not preventive "
               "access control. If the timestamp is stale, check Colab runtime."
    }
    pending=directory/"guardian_health.pending.json"
    destination=directory/"guardian_health.json"
    try:
        with open(pending,"w",encoding="utf-8") as fh:
            json.dump(status_doc,fh,indent=2)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        os.replace(pending,destination)
    finally:
        try:
            pending.unlink(missing_ok=True)
        except OSError:
            pass
    return destination


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


def recover_existing(path, events, private_directory, expected_raw=None):
    """Recover in place, refusing to overwrite a concurrently changed snapshot.

    There is no atomic compare-and-swap for Google Drive FUSE. A byte-for-byte
    check while the EXISTING file is already open narrows (but cannot remove)
    the race with another Colab user. We never os.replace the shared shortcut.
    """
    if not path.is_file():
        raise FileNotFoundError("Original shared file/shortcut is absent")
    data=safe_bytes(events)
    if expected_raw is not None and not isinstance(expected_raw,bytes):
        raise TypeError("expected_raw must be original bytes")
    # Do not store or print actual message text in routine status output.
    with open(path,"r+b") as fh:
        current=fh.read(MAX_FILE+1)
        if expected_raw is not None and current!=expected_raw:
            raise ConcurrentChatChange(
                "Shared chat changed during repair; rescan and preserve new messages")
        # Save the exact suspicious bytes before the destructive write.
        preserve_forensics(path,private_directory)
        fh.seek(0)
        fh.write(data)
        fh.truncate()
        fh.flush()
        try:
            os.fsync(fh.fileno())
        except OSError:
            # Some Google Drive FUSE mounts do not implement fsync().
            pass
    raw,check=read_shared(path)
    if check!=events or raw!=data:
        raise IOError("recovery write could not be verified byte-for-byte")
    return len(raw)


def restore_trash_by_id(file_id, timeout=8):
    """Best-effort restore SAME Drive file ID if trashed. NO new auth prompt.

    Requires already-granted Google credentials with sufficient Drive scope.
    If a Workspace policy denies this, do not work around it.
    """
    if not file_id:
        return "No Drive file ID configured; cannot untrash original file"
    if not re.fullmatch(r"[A-Za-z0-9_-]{10,128}",file_id):
        return "Invalid original Drive file ID format"
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
    except Exception as exc:
        # Includes missing Google libraries and blocked Workspace API scopes.
        # Do not request another Google login or invent a replacement file.
        return "Drive API unavailable or denied: "+type(exc).__name__



def historical_clock_audit(events, now=None):
    """Explain old timestamps without changing protected source or post order.

    Absolute time comes only from signed legacy epochs and matching UTC clock
    sidecars. Bare HH:MM:SS is ambiguous; estimates assume an ordered UTC log,
    and MUST NOT be treated as proof of the original sending date.
    """
    anchor=int(time.time() if now is None else now)+300
    known={}
    for e in events:
        if simple_clock(e):
            key=e["message_hash"]
            known.setdefault(key,[]).append(e)
    entries=[]
    for idx,e in enumerate(events):
        if not isinstance(e,dict) or e.get("kind")!="message":
            continue
        raw=e.get("time","")
        if not isinstance(raw,str):
            raw=""
        absolute=None
        basis="unknown"
        if type(e.get("issued_at")) is int and 946684800<=e["issued_at"]<=4102444800:
            absolute=e["issued_at"]
            basis="stored_epoch"
        elif simple_message(e):
            matches=known.get(plain_message_hash(e),[])
            for meta in matches:
                if meta["user"]==e["user"] and meta["time"]==e["time"]:
                    absolute=meta["epoch"]
                    basis="clock_metadata"
                    break
        entries.append({"position":idx,"user":str(e.get("user",""))[:128],
                        "stored_time":raw,"epoch":absolute,"basis":basis})
    counts={"exact":0,"estimated":0,"unknown":0}
    for item in reversed(entries):
        absolute=item["epoch"]
        if type(absolute) is int:
            anchor=absolute
            counts["exact"]+=1
            continue
        clock=re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d):([0-5]\d)",
                           item["stored_time"])
        if not clock:
            counts["unknown"]+=1
            continue
        h,m,sec=[int(x) for x in clock.groups()]
        candidate=(anchor//86400)*86400+h*3600+m*60+sec
        # Concurrent Drive writers may append a message seconds after one
        # that was actually sent later. Do not mistake a small inversion for
        # a UTC midnight rollover (which would invent a one-day gap).
        if candidate>anchor+120:
            candidate-=86400
        delta=anchor-candidate
        if candidate>=946684800 and -120<=delta<=86400+300:
            item["epoch"]=candidate
            item["basis"]="inferred_utc_not_verified"
            counts["estimated"]+=1
            anchor=min(anchor,candidate)
        else:
            counts["unknown"]+=1
    inversions=[]
    for earlier,later in zip(entries,entries[1:]):
        a,b=earlier["epoch"],later["epoch"]
        if type(a) is int and type(b) is int and 0<a-b<=120:
            exact=(earlier["basis"] in ("clock_metadata","stored_epoch")
                   and later["basis"] in ("clock_metadata","stored_epoch"))
            inversions.append({"previous_position":earlier["position"],
                               "next_position":later["position"],
                               "seconds":a-b,
                               "confidence":"exact" if exact else "inferred"})
    return {"schema":1,"generated_utc":dt.datetime.now(dt.timezone.utc).isoformat(),
            "warning":"Date-less clocks may have gaps or used local time. "
                      "Inferred UTC times are estimates, not authenticated; "
                      "the shared chat log is not reordered or rewritten.",
            "counts":counts,
            "near_order_inversions":inversions,
            "messages":entries}


class Guardian:
    def __init__(self,shared,private_directory,db_path,drive_file_id="",repair=True):
        self.shared=Path(shared)
        self.private=Path(private_directory)
        self.db_path=Path(db_path)
        self.db_path.parent.mkdir(parents=True,exist_ok=True)
        self.drive_file_id=drive_file_id
        self.repair=repair
        self.private.mkdir(parents=True,exist_ok=True)
        if not self.db_path.exists():
            persisted=self.private/"guardian.sqlite3"
            if persisted.exists():
                shutil.copyfile(persisted,self.db_path)
        self.db=connect_db(self.db_path)
        self.last_missing_notice=0.0
        self.last_clock_report=0.0

    def log(self,msg):
        print("["+dt.datetime.now().strftime("%H:%M:%S")+"] "+msg,flush=True)

    def backup(self):
        private_db_backup(self.db,self.private)

    def clock_report(self, events, force=False):
        """Emit protected owner-only clock report, never mutate chat.json."""
        now=time.monotonic()
        if not force and now-self.last_clock_report<120:
            return
        report=historical_clock_audit(events)
        destination=self.private/"historical_clock_audit.json"
        pending=self.private/"historical_clock_audit.pending.json"
        with open(pending,"w",encoding="utf-8") as fh:
            json.dump(report,fh,indent=2,ensure_ascii=False)
        os.replace(pending,destination)
        self.last_clock_report=now
        counts=report["counts"]
        self.log("Historical clock audit: "+
                 f'{counts["exact"]} exact, {counts["estimated"]} inferred, '+
                 f'{counts["unknown"]} unknown; '+
                 f'{len(report["near_order_inversions"])} nearby ordering inversions. '+
                 'Original history untouched; newer clients fix display order.')

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
                ident=event_id(event,index)
                if not isinstance(event,dict) or not isinstance(ident,str):
                    raise ValueError("initial history contains an event without ID")
                if ident in seen:
                    raise ValueError("initial history contains duplicate IDs")
                seen.add(ident)
                prepared.append(event)
            # Existing chat history is not a burst of newly sent messages.
            save_approved(self.db,prepared,baseline=True)
            self.backup()
            self.log(f"Baseline enrolled: {len(initial)} events, {len(raw)} bytes."
                     " Historical records grandfathered; new events enforced.")
            self.clock_report(initial,force=True)
            return "enrolled"
        expected={event_id(item,index):item for index,item in enumerate(known)}
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
                self.backup()
                self.log("ALERT: "+reason)
                if isinstance(exc,FileNotFoundError):
                    result=restore_trash_by_id(self.drive_file_id)
                    self.log("Drive recovery: "+result)
                    # The shortcut may not reappear immediately after untrash.
                    return "missing"
                # An incomplete append can be normal during Drive sync.
                # Never clobber the entire file just because a framed record
                # has not finished propagating between Colab sessions.
                if self.shared.is_file():
                    try:
                        raw_snapshot=self.shared.read_bytes()
                    except OSError:
                        raw_snapshot=b""
                    if b'\x1e' in raw_snapshot:
                        self.log("Append-only journal unreadable; leaving shared bytes "
                                 "untouched for next scan / manual inspection.")
                        return "corrupt"
                if self.repair:
                    try:
                        recover_existing(
                            self.shared,known,self.private,
                            expected_raw=raw_snapshot)
                    except ConcurrentChatChange:
                        self.log("Invalid shared file changed during attempted "
                                 "repair; preserving new data for rescan.")
                        return "rescan"
                    self.log("Restored last approved chat history IN PLACE.")
                    self.backup()
                    return "restored"
                return "corrupt"

        # A deliberate four-field migration invalidates old ID-based
        # checkpoints. Never undo it automatically and restore old metadata.
        migrated=(bool(live) and all(
                    simple_message(e) or simple_title(e) or
                    simple_rename(e) or simple_reaction(e) or
                     simple_clock(e) for e in live))
        old_baseline=any(e.get("id")==ANCHOR for e in known)
        if migrated and old_baseline:
            self.log("FORMAT MIGRATION detected. Old guardian checkpoint "
                     "does not apply; restart with --reset-baseline.")
            return "migration_needs_baseline"

        # Compare known approved history against live file, even if someone
        # changed a prior signed event or deleted the entire chat array.
        observed=set()
        changed=False
        invalid=[]
        additions=[]
        ids=dict(expected)
        rate={}
        # Previously approved messages still count during the next 60 seconds,
        # including after restart. The initial baseline has no reliable timestamp.
        recent_cutoff=time.time()-60
        for stored_json, observed_at in self.db.execute(
                "SELECT json,observed_at FROM approved WHERE observed_at>=?",
                (recent_cutoff,)):
            try:
                stored=json.loads(stored_json)
            except (ValueError,TypeError):
                continue
            kind=stored.get("kind")
            if kind=="message" and isinstance(stored.get("text"),str):
                body=" ".join(stored["text"].casefold().split())
                if simple_message(stored):
                    key="display-name:"+stored["user"].casefold()
                    rate.setdefault("__room_messages__",[]).append(
                        (observed_at,body))
                else:
                    key=stored.get("sender_key")
                    if not isinstance(key,str):
                        continue
                rate.setdefault(key,[]).append((observed_at,body))
            elif kind in ("clock","reaction","title","rename","profile","role"):
                sender=stored.get("sender_key") or (
                    "display-name:"+str(stored.get("user","")).casefold())
                rate.setdefault("__meta__:"+sender,[]).append(
                    (observed_at,kind))
                rate.setdefault("__all_metadata__",[]).append(
                    (observed_at,kind))
        # The private checkpoint supplies observation-time counters across
        # polls/restarts; sender-controlled timestamps cannot reset a limit.
        # New records must be APPENDED after every approved checkpoint row.
        # A signed but back-inserted record must not rewrite past chronology.
        if (len(live)<len(known) or
                any(canonical_event(live[index])!=canonical_event(saved)
                    for index,saved in enumerate(known))):
            changed=True
            incident(self.db,"Append-only approved-history prefix changed",
                     action="restore_attempt")
            self.log("ALERT: existing history moved, replaced or back-inserted.")
        live_known_order=[]
        for position,entry in enumerate(live):
            eid=event_id(entry,position)
            if not isinstance(eid,str) or eid in observed:
                changed=True
                invalid.append(("duplicate/malformed event ID",entry))
                continue
            observed.add(eid)
            if eid in expected:
                live_known_order.append(eid)
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
        # Reordering approved events changes the meaning of role grants and
        # reactions, even if every signature remains valid.
        expected_known_order=[
            event_id(e,index) for index,e in enumerate(known)
            if event_id(e,index) in observed]
        if live_known_order!=expected_known_order:
            changed=True
            incident(self.db,"Approved history was reordered",action="restore_attempt")
        if missing:
            changed=True
        if invalid:
            for reason,event in invalid[:100]:
                incident(self.db,reason,event)
            self.log(f"ALERT: rejected {len(invalid)} unauthorized/invalid events")
        if missing:
            incident(self.db,f"{len(missing)} approved events deleted",action="restore_attempt")
            self.log(f"ALERT: {len(missing)} approved events disappeared")
        if additions and not changed:
            save_approved(self.db,additions)
            self.backup()
            self.log(f"Accepted {len(additions)} new events")
        elif additions:
            self.log("New events found during tampering; not approving until clean.")
        if changed:
            if not self.repair:
                self.backup()
                return "needs_repair"
            # Re-read to avoid clobbering a newer, valid concurrent save.
            # This is still a best-effort check, NOT a transaction.
            time.sleep(0.35+random.random()*0.25)
            try:
                fresh,_=read_shared(self.shared)
            except (OSError,ValueError):
                fresh=None
            if fresh is None or fresh!=raw:
                self.log("Shared file changed or became unreadable during scan; "
                         "preserving other writers' data for next pass.")
                return "rescan"
            restored=db_events(self.db)
            try:
                recover_existing(
                    self.shared,restored,self.private,expected_raw=raw)
            except ConcurrentChatChange:
                self.log("Shared file changed just before repair write; "
                         "rescan without erasing new messages.")
                return "rescan"
            incident(self.db,"Repaired invalid/deleted history",action="restored")
            self.backup()
            self.log(f"Recovered approved history ({len(restored)} records) IN PLACE")
            return "restored"
        self.clock_report(live,force=bool(additions))
        return "healthy"

    def close(self):
        self.db.close()


def guardian_authorize_owner_colab(authorize=None):
    """Ask Colab for real *user* Drive API consent before any Drive API call.

    Mounting /content/drive alone does not supply OAuth credentials for REST.
    In affected runtimes google.auth.default() otherwise selects a GCE
    credential, whose metadata service has no service account (HTTP 404).
    Colab authenticate_user() is idempotent for an already authorized account
    and shows a Google consent prompt only when required.
    """
    if authorize is None:
        try:
            from google.colab import auth
            authorize=lambda: auth.authenticate_user(clear_output=False)
        except ImportError as exc:
            raise RuntimeError(
                "Colab authorization is unavailable in this Python runtime. "
                "Add the original chat.json shortcut manually in Google Drive."
            ) from exc
    print("Owner Guardian: checking Google user permission for the Drive "
          "shortcut. Complete a Google sign-in prompt if shown.",flush=True)
    try:
        authorize()
    except Exception as exc:
        raise RuntimeError(
            "Google user authorization was denied or unavailable ("+
            type(exc).__name__+"). Open the original shared chat.json in "
            "Google Drive and use Organize > Add shortcut in My Drive. "
            "Google Workspace account restrictions cannot be bypassed."
        ) from exc


def guardian_user_drive_session(get_default=None, login=None,
                                make_request=None, make_session=None,
                                is_machine=None, colab_adc_loader=None,
                                ephemeral_mode=None):
    """Use Google's owner-authorized Colab credentials, not a VM service account.

    Colab supports both file-based user ADC and temporary user authentication
    routed through the metadata credential class (USE_AUTH_EPHEM=1). Rejecting
    every Compute Engine credential after successful consent caused a loop.
    Never print, copy to Drive, or disclose the user's access/refresh tokens.
    """
    if get_default is None:
        import google.auth
        get_default=google.auth.default
    if make_request is None or make_session is None:
        from google.auth.transport.requests import Request, AuthorizedSession
        if make_request is None:
            make_request=Request
        if make_session is None:
            make_session=AuthorizedSession
    if is_machine is None:
        from google.auth.compute_engine.credentials import Credentials as GceCredentials
        from google.oauth2.service_account import Credentials as ServiceCredentials
        is_machine=lambda cred: isinstance(cred,(GceCredentials,ServiceCredentials))
    if ephemeral_mode is None:
        ephemeral_mode=(os.environ.get("USE_AUTH_EPHEM")=="1")

    def validated(credentials):
        # In Colab's ephemeral-user mode, *authorized user* tokens may be
        # represented by the Compute Engine credentials class. A refresh must
        # actually succeed. Ordinary VM and service-account ADC remain barred.
        is_ephemeral_gce=(ephemeral_mode and
            type(credentials).__module__.startswith("google.auth.compute_engine"))
        if is_machine(credentials) and not is_ephemeral_gce:
            raise RuntimeError("default credentials are not authorized user credentials")
        if not credentials.valid:
            credentials.refresh(make_request())
        if not credentials.valid:
            raise RuntimeError("Google user credentials are not valid")
        return credentials

    def colab_file_credentials():
        # A standard Colab user login writes /content/.adc/adc.json. If
        # google.auth.default() still points at GCE, explicitly select this
        # Colab-managed file, not an arbitrary service-account JSON.
        adc=Path(os.environ.get("DATALAB_ROOT","/"))/"content/.adc/adc.json"
        if not adc.is_file():
            raise RuntimeError("Colab user ADC file is unavailable")
        if colab_adc_loader is None:
            import google.auth
            credentials,_=google.auth.load_credentials_from_file(str(adc))
        else:
            credentials=colab_adc_loader(str(adc))
        if is_machine(credentials):
            raise RuntimeError("Colab ADC file contains machine credentials")
        return validated(credentials)

    def existing_user_credentials():
        errors=[]
        try:
            credentials,_=get_default()
            return validated(credentials)
        except Exception as exc:
            errors.append(type(exc).__name__)
        try:
            return colab_file_credentials()
        except Exception as exc:
            errors.append(type(exc).__name__)
        raise RuntimeError("no usable Colab user credentials ("+
                           ",".join(errors)+")")

    try:
        credentials=existing_user_credentials()
    except Exception:
        print("Owner Guardian: the default Google credentials cannot be used "
              "for this shared Drive file. Requesting Colab user "
              "authorization if needed.",flush=True)
        try:
            if login is None:
                from google.colab import auth
                auth.authenticate_user(clear_output=False)
            else:
                login()
            credentials=existing_user_credentials()
        except Exception as exc:
            raise RuntimeError(
                "Colab did not provide usable Google user credentials ("+
                type(exc).__name__+"). Open the original shared chat.json in "
                "Google Drive using this account and choose "
                "Organize > Add shortcut to My Drive. Google Workspace "
                "restrictions cannot be bypassed."
            ) from exc
    return make_session(credentials)


def ensure_shared_chat_shortcut(drive_root, session=None, sleep=None,
                                max_polls=12):
    """Join the ORIGINAL shared room by making one Drive shortcut, not a copy.

    Use the owner's existing Google authorization if available. If the
    notebook has only unavailable VM credentials, ask the owner to approve
    Colab's normal Google user authorization once. Never bypass a school's
    restriction or switch Google accounts silently.
    """
    chat_path = os.path.join(drive_root, 'chat.json')
    if os.path.isfile(chat_path):
        return chat_path
    if os.path.lexists(chat_path):
        raise RuntimeError('My Drive already contains a non-file chat.json. '
                           'Not replacing it or creating a second room.')
    if sleep is None:
        sleep = time.sleep

    def fail(reason):
        raise FileNotFoundError(
            'Automatic chat.json shortcut setup could not complete: ' + reason +
            '. Open ' + SHARED_CHAT_FILE_LINK +
            ' with the same Google account as Colab and use '
            '"Organize > Add shortcut", selecting the root of My Drive, '
            'then rerun this launcher. Never upload a copy of chat.json.'
        )

    if session is None:
        try:
            # Do this BEFORE creating an AuthorizedSession. Otherwise
            # google.auth.default() can select a broken VM credential and
            # cause the observed metadata.google.internal 404 RefreshError.
            guardian_authorize_owner_colab()
            session = guardian_user_drive_session()
        except RuntimeError as exc:
            fail(str(exc))
        except Exception as exc:
            fail('Google Drive user authorization failed (' +
                 type(exc).__name__ + ')')

    api = 'https://www.googleapis.com/drive/v3/files'
    try:
        # Prevent duplicate shortcuts after a previous launch or Drive sync.
        existing = []
        cursor = None
        for _ in range(5):
            params = {
                'q': ("'root' in parents and name = 'chat.json' "
                      "and trashed = false"),
                'fields': ('nextPageToken,files(id,name,mimeType,'
                           'shortcutDetails(targetId))'),
                'pageSize': 100,
            }
            if cursor:
                params['pageToken'] = cursor
            listing = session.get(api, params=params, timeout=15)
            if listing.status_code != 200:
                fail('Drive did not permit listing My Drive shortcuts '
                     '(HTTP ' + str(listing.status_code) + ')')
            data = listing.json()
            existing.extend(data.get('files', []))
            cursor = data.get('nextPageToken')
            if not cursor:
                break
        if cursor:
            fail('Drive shortcut search was incomplete')
        def is_target_shortcut(item):
            return (item.get('mimeType') == 'application/vnd.google-apps.shortcut'
                    and (item.get('shortcutDetails') or {}).get('targetId')
                    == SHARED_CHAT_FILE_ID)
        # Refuse even ONE colliding chat.json; a Drive mount could expose the
        # wrong item when multiple files have an identical display name.
        if any(not is_target_shortcut(item) for item in existing):
            fail('a different chat.json already exists in My Drive; '
                 'it must not be replaced')
        correct = any(is_target_shortcut(item) for item in existing)
        if not correct:
            # Confirm the target is accessible; permissions are never edited.
            target = session.get(
                api + '/' + SHARED_CHAT_FILE_ID,
                params={'fields': 'id,name,mimeType,trashed'},
                timeout=15,
            )
            if target.status_code != 200:
                fail('this account cannot access the original shared file '
                     '(HTTP ' + str(target.status_code) + ')')
            info = target.json()
            if (info.get('id') != SHARED_CHAT_FILE_ID or
                    info.get('mimeType') != 'application/json' or
                    info.get('trashed')):
                fail('the linked target is not the expected live chat.json')
            shortcut = session.post(
                api,
                json={'name': 'chat.json',
                      'mimeType': 'application/vnd.google-apps.shortcut',
                      'parents': ['root'],
                      'shortcutDetails': {'targetId': SHARED_CHAT_FILE_ID}},
                params={'fields': 'id,name,mimeType,shortcutDetails(targetId)'},
                timeout=15,
            )
            if shortcut.status_code not in (200, 201):
                fail('Drive rejected creating the shortcut '
                     '(HTTP ' + str(shortcut.status_code) + ')')
            created = shortcut.json()
            if ((created.get('shortcutDetails') or {}).get('targetId')
                    != SHARED_CHAT_FILE_ID):
                fail('Drive did not confirm the shortcut target')
            print('Created a shortcut to the existing shared chat.json '
                  'in this account\'s My Drive. No file was copied.')

        # Drive FUSE can take some time to expose new shortcuts.
        for attempt in range(max_polls):
            if os.path.isfile(chat_path):
                return chat_path
            if attempt < max_polls - 1:
                sleep(1)
        fail('Drive created/found the shortcut but it is not yet visible '
             'in the mounted filesystem; remount Drive and rerun')
    except FileNotFoundError:
        raise
    except Exception as exc:
        if type(exc).__name__ in ('TransportError','RefreshError'):
            fail('Google Drive API received invalid machine credentials ('+
                 type(exc).__name__+'). Colab user OAuth is required; '+
                 'do not retry Google VM metadata credentials')
        fail('the Drive API could not complete the request (' +
             type(exc).__name__ + ')')



def drive_root():
    """Automatically mount Drive in Colab with the user's permission."""
    for path in ("/content/drive/MyDrive","/content/drive/My Drive"):
        if os.path.isdir(path):
            return Path(path)
    if os.path.isdir("/content"):
        try:
            from google.colab import drive
            drive.mount("/content/drive")
        except ImportError:
            pass
        except Exception as exc:
            raise RuntimeError("Google Drive could not be mounted in Colab: " +
                               type(exc).__name__ + ": " + str(exc)) from exc
        for path in ("/content/drive/MyDrive","/content/drive/My Drive"):
            if os.path.isdir(path):
                return Path(path)
    raise RuntimeError("Google Drive not mounted. Open in Colab or use "
                       "--file and --private for an explicit local run.")


def main(argv=None):
    # Notebook kernels add their connection-file arguments to sys.argv
    # (for example: colab_kernel_launcher.py -f kernel.json).
    # These are NOT Guardian options. Explicit caller-provided argv and
    # normal terminal invocations are parsed without modification.
    if argv is None and os.path.basename(sys.argv[0]).endswith(
            "kernel_launcher.py"):
        argv=[]
    parser=argparse.ArgumentParser(description="Messaging App owner-only guardian")
    parser.add_argument("--file",help="Original shared chat.json shortcut path")
    parser.add_argument("--private",help="PRIVATE owner-only backup directory")
    parser.add_argument("--file-id",default="",help="Original Google Drive file ID for trash recovery")
    parser.add_argument("--interval",type=float,default=5.0,help="Seconds between scans")
    parser.add_argument("--once",action="store_true",help="Scan once and exit")
    parser.add_argument("--reset-baseline",action="store_true",
                        help="Archive old SQLite and trust migrated 4-field history")
    parser.add_argument("--audit-only",action="store_true",help="Never modify shared chat.json")
    args=parser.parse_args(argv)
    root=drive_root() if not args.file or not args.private else None
    # Auto-enroll only for the default shared room. Explicit --file paths
    # remain completely untouched (tests, audit/recovery and owner overrides).
    shared=(Path(args.file) if args.file else
            Path(ensure_shared_chat_shortcut(str(root))))
    if not args.file and not args.file_id:
        args.file_id=SHARED_CHAT_FILE_ID
    private=Path(args.private) if args.private else root/".messaging_guardian"
    private.mkdir(parents=True,exist_ok=True)
    if args.file_id:
        (private/"original_file_id.txt").write_text(args.file_id.strip())
    else:
        file_id_file=private/"original_file_id.txt"
        if file_id_file.exists():
            args.file_id=file_id_file.read_text().strip()
    if args.reset_baseline:
        current=private/"guardian.sqlite3"
        if current.exists():
            archive=private/("guardian_before_format_migration_%d.sqlite3" %
                             int(time.time()))
            shutil.copy2(current,archive)
            current.unlink()
        print("Old guardian baseline archived; re-enrolling 4-field history.",flush=True)
    # SQLite runs locally, with every approved version checkpointed to private
    # Google Drive using the SQLite backup API. No SQLite lock on Drive FUSE.
    local_temp=tempfile.TemporaryDirectory(prefix="messaging_guardian_")
    local_db=Path(local_temp.name)/"guardian.sqlite3"
    guardian=Guardian(shared,private,local_db,args.file_id,not args.audit_only)
    try:
        print("OWNER GUARDIAN v"+GUARDIAN_VERSION+" started; this process must remain running.",flush=True)
        print("Shared file:",shared,flush=True)
        print("PRIVATE backups:",private,flush=True)
        print("Mode:","AUDIT ONLY" if args.audit_only else "AUTO REPAIR",flush=True)
        last_live_notice=0.0
        started_at=dt.datetime.now(dt.timezone.utc).isoformat()
        last_success=None
        scan_count=0
        consecutive_errors=0
        latest_status="starting"
        latest_approved=None
        latest_bytes=None
        mode="AUDIT ONLY" if args.audit_only else "AUTO REPAIR"
        while True:
            try:
                scan_status=guardian.process()
                scan_count+=1
                latest_status=scan_status
                consecutive_errors=0
                if scan_status in ("healthy","enrolled","restored"):
                    last_success=dt.datetime.now(dt.timezone.utc).isoformat()
            except Exception as exc:
                scan_count+=1
                consecutive_errors+=1
                latest_status="error:"+type(exc).__name__
                guardian.log("ERROR: "+type(exc).__name__+": "+str(exc))
                if isinstance(exc,GuardianLedgerError):
                    guardian.log("CRITICAL: private approved-history ledger "
                                 "integrity failed. AUTO REPAIR IS BLOCKED. "
                                 "Do not reset the baseline.")
            current=time.monotonic()
            if current-last_live_notice>=30.0 or args.once:
                try:
                    raw_live,events_live=read_shared(shared)
                    latest_bytes=len(raw_live)
                    latest_approved=count_events(guardian.db)
                    guardian.log(
                        "LIVE MONITOR: "+latest_status+"; shared file "+
                        str(latest_bytes)+" bytes / "+
                        str(len(events_live))+" records; "+
                        str(latest_approved)+" approved in private checkpoint. "+
                        "Scanning every "+str(max(2.0,args.interval))+
                        " seconds.")
                except (OSError,ValueError,sqlite3.Error) as exc:
                    latest_bytes=None
                    guardian.log("LIVE MONITOR: "+latest_status+
                                 "; shared file/ledger unavailable: "+
                                 type(exc).__name__)
                try:
                    write_health_status(
                        private,latest_status,started_at=started_at,
                        scans=scan_count,errors=consecutive_errors,
                        last_success=last_success,approved=latest_approved,
                        bytes_on_disk=latest_bytes,running=not args.once,
                        mode=mode)
                except OSError as exc:
                    guardian.log("Cannot persist private monitor health: "+
                                 type(exc).__name__)
                last_live_notice=current
            if args.once:
                break
            time.sleep(max(2.0,args.interval))
    except KeyboardInterrupt:
        guardian.log("Stopped by owner")
    finally:
        try:
            write_health_status(
                private,"stopped",
                started_at=started_at if "started_at" in locals() else
                dt.datetime.now(dt.timezone.utc).isoformat(),
                scans=scan_count if "scan_count" in locals() else 0,
                errors=consecutive_errors if
                "consecutive_errors" in locals() else 0,
                last_success=last_success if "last_success" in locals() else None,
                approved=latest_approved if "latest_approved" in locals() else None,
                bytes_on_disk=latest_bytes if "latest_bytes" in locals() else None,
                running=False,
                mode="AUDIT ONLY" if args.audit_only else "AUTO REPAIR")
        except OSError:
            guardian.log("Could not write final stopped health indicator.")
        guardian.close()
        local_temp.cleanup()


if __name__=="__main__":
    main()
