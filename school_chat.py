# @title Messaging App v5.19 — Google Drive Messenger
"""Messaging App — Google Colab chat using a shared Google Drive chat.json.

Messages, threads, emoji reactions, search and responsive layout all use the
original shared JSON file. No separate shared folder or attachment features.
There is one self-claimed blue Verified title and no privileged roles.

Shared JSON is NOT a transactional database: concurrent saves may overwrite
another client's changes. The checkmark does not prove Google ownership.
"""
import base64
import hashlib
import json
import urllib.request
import urllib.error
import os
import re
import threading
import time
import uuid
import unicodedata
import shlex
from datetime import datetime

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey
)
from cryptography.hazmat.primitives import serialization
from google.colab import drive, output
from IPython.display import HTML, JSON, display

# Original terminal look and chat behavior
VISIBLE_MESSAGES = 15
INPUT_WIDTH_PX = 540
WRAP_WIDTH_CH = 80
REFRESH_INTERVAL_MS = 1000
COOLDOWN_SECONDS = 1.5
MESSAGE_LIMIT = 1000
CLIENT_VERSION = '5.19'
UPSTREAM_VERSION_CHECK_SECONDS = 25.0
GITHUB_CHAT_SOURCE_URL = (
    'https://raw.githubusercontent.com/PletzelOnYT/'
    'School-Chat-Room/main/school_chat.py')
# Anchor from the original school chat. Prevent accidentally splitting the room.
ORIGINAL_HISTORY_ANCHOR = '081cad07-8430-4c0e-8000-f3a4a379536a'

if not os.path.ismount('/content/drive'):
    drive.mount('/content/drive')

DRIVE_ROOT = next((p for p in ('/content/drive/MyDrive', '/content/drive/My Drive')
                   if os.path.isdir(p)), None)
if not DRIVE_ROOT:
    raise RuntimeError('Your Google Drive is not mounted.')

CHAT_FILE_PATH = os.path.join(DRIVE_ROOT, 'chat.json')
if not os.path.isfile(CHAT_FILE_PATH):
    raise FileNotFoundError(
        'Shared chat.json not found. Add a SHORTCUT to the original shared '
        'chat.json in the ROOT of your own My Drive. Do not copy it.'
    )

PRIVATE_FOLDER = os.path.join(DRIVE_ROOT, '.schoolchat')
PRIVATE_IDENTITY_PATH = os.path.join(PRIVATE_FOLDER, 'identity.json')


# Google Colab uses the same system username for many students. Do not use
# ColabUser or getpass as a chat nickname, not even momentarily.
# The value is assigned only after loading this person's private identity.
username = None

file_lock = threading.RLock()
state_lock = threading.RLock()
started_at = time.monotonic()
last_message_time = 0.0
recent_sent_messages = []  # Only local: (timestamp, normalized text)
running = True
local_messages = []  # Private, inline System messages, NEVER sent to Drive.
next_popup = None  # One-shot private /help popup
visible_count = VISIBLE_MESSAGES
muted_names = set()
clear_through = 0
minimum_client_version = ''
update_required = False
update_notice_shown = False
last_upstream_version_check = 0.0

class ChatFileError(Exception):
    pass


def b64(raw):
    return base64.b64encode(raw).decode('ascii')


def b64decode(value):
    return base64.b64decode(value, validate=True)


def load_identity():
    """Keep a stable ID across renames and Colab restarts, using OWN Drive.

    IP addresses are shared/changeable; Colab OS usernames are usually all
    'root'. We never silently collect school email addresses or IPs.
    Only possession of this locally stored private key establishes identity.
    """
    os.makedirs(PRIVATE_FOLDER, exist_ok=True)
    if not os.path.exists(PRIVATE_IDENTITY_PATH):
        key = Ed25519PrivateKey.generate()
        raw = key.private_bytes(serialization.Encoding.Raw,
                                serialization.PrivateFormat.Raw,
                                serialization.NoEncryption())
        pub = key.public_key().public_bytes(serialization.Encoding.Raw,
                                             serialization.PublicFormat.Raw)
        try:
            # Multiple Colab tabs can start simultaneously. Never overwrite
            # the key already created by another session on this Drive.
            with open(PRIVATE_IDENTITY_PATH, 'x', encoding='utf-8') as f:
                json.dump({'type': 'school-chat-identity-v1',
                           'private_key': b64(raw), 'public_key': b64(pub)}, f, indent=2)
        except FileExistsError:
            pass
    try:
        with open(PRIVATE_IDENTITY_PATH, encoding='utf-8') as f:
            obj = json.load(f)
        key = Ed25519PrivateKey.from_private_bytes(b64decode(obj['private_key']))
    except Exception as exc:
        raise ChatFileError('Your private identity file could not be read. '
                            'Do NOT delete it: it controls your permanent chat ID.') from exc
    public = b64(key.public_key().public_bytes(serialization.Encoding.Raw,
                                               serialization.PublicFormat.Raw))
    return key, public


identity_private, identity_public = load_identity()


def google_account_username():
    """Optional existing Google display name; no new Google sign-in is started.

    Google Workspace for Education accounts can be restricted by age or
    administrator policy. Respect that restriction: never call
    google.colab.auth.authenticate_user(), request extra OAuth scopes, or
    attempt a different login route just to discover a nickname. If the
    currently authorized Drive credentials cannot provide the account name,
    return None; the chat uses its existing signed Guest identity instead.
    """
    try:
        import google.auth
        import requests

        credentials, _ = google.auth.default()
        # Do not refresh credentials or trigger another authorization flow.
        token = getattr(credentials, 'token', None)
        if not token or not getattr(credentials, 'valid', False):
            return None
        response = requests.get(
            'https://www.googleapis.com/drive/v3/about',
            params={'fields': 'user(emailAddress)'},
            headers={'Authorization': 'Bearer ' + token},
            timeout=6,
        )
        if response.status_code != 200:
            return None
        email = (response.json().get('user') or {}).get('emailAddress')
        if not isinstance(email, str) or '@' not in email:
            return None
        local_part = email.rsplit('@', 1)[0]
        candidate = re.sub(r'[^A-Za-z0-9._-]', '', local_part)[:32]
        return candidate or None
    except Exception:
        # An age-restricted or otherwise unavailable Google service should
        # never stop the whole school chat from loading.
        return None


def short_id(pub):
    try:
        return hashlib.sha256(b64decode(pub)).hexdigest()[:12]
    except Exception:
        return 'unknown'


# Never trust a nickname as a role or an identity. Only verified signed
# server grants can produce a badge. All clients apply these same rules.
RESERVED_USERNAMES = {
    'owner', 'mod', 'moderator', 'admin', 'administrator',
    'verified', 'system', 'server', 'staff', 'school chat',
    'schoolchat', 'security', 'support',
}
GENERIC_USERNAMES = {'colabuser', 'user', 'guest'}
# Guests are generated from each signed identity. They cannot be claimed manually.
GUEST_NAME_RE = re.compile(r'(?i)^guest-(?:[0-9]{2,3}|[0-9a-f]{8,64})$')


def valid_username(value):
    """Normalize legitimate names; forbid terminal/control/role-tag spoofing.

    This does not pretend to attest that someone's editable Colab source code
    is genuine. Receiving clients enforce the rules independently.
    """
    if not isinstance(value, str) or not value:
        return None
    clean = unicodedata.normalize('NFKC', value)
    if clean != clean.strip() or not 1 <= len(clean) <= 32:
        return None
    if clean.casefold() in RESERVED_USERNAMES | GENERIC_USERNAMES or GUEST_NAME_RE.fullmatch(clean):
        return None
    # Unicode format chars include invisible / bidirectional control codes.
    if any(unicodedata.category(ch).startswith('C') for ch in clean):
        return None
    if any(not (ch.isalnum() or ch in ' _-.') for ch in clean):
        return None
    if clean.startswith(('.', '-', '_')) or clean.endswith(('.', '-', '_')):
        return None
    if '  ' in clean:
        return None
    return clean


def guest_digest(sender):
    """Full random-identity fingerprint; never derived from IP, Gmail or Colab OS."""
    try:
        return hashlib.sha256(b64decode(sender)).hexdigest()
    except Exception:
        return ''


def fallback_username(sender):
    """Stable, virtually collision-free Guest name from each private Drive identity."""
    digest = guest_digest(sender)
    return 'Guest-' + str(100 + int(digest[:8], 16) % 900) if digest else 'Guest-000'


# Use an already-authorized account prefix only if permitted and available.
# Otherwise preserve the signed, stable Guest-### name. The full email address
# and Google tokens are never written to the shared chat file.
_account_hint = google_account_username()
GOOGLE_ACCOUNT_NAME_AVAILABLE = bool(_account_hint and valid_username(_account_hint))
GOOGLE_ACCOUNT_USERNAME = (_account_hint if GOOGLE_ACCOUNT_NAME_AVAILABLE
                           else fallback_username(identity_public))
username = GOOGLE_ACCOUNT_USERNAME


def canonical(event):
    return json.dumps({k: v for k, v in event.items() if k != 'sig'},
                      sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')


def sign_event(event):
    event['sender_key'] = identity_public
    event['sig'] = b64(identity_private.sign(canonical(event)))
    return event


def valid_signature(event):
    if not isinstance(event, dict) or 'sender_key' not in event or 'sig' not in event:
        return False
    try:
        pub = Ed25519PublicKey.from_public_bytes(b64decode(event['sender_key']))
        pub.verify(b64decode(event['sig']), canonical(event))
        return True
    except Exception:
        return False


def read_messages():
    """Read the ORIGINAL shared Google Drive chat.json; do not create a folder."""
    with file_lock:
        for attempt in range(4):
            try:
                with open(CHAT_FILE_PATH, encoding='utf-8') as fh:
                    events = json.load(fh)
                break
            except (OSError, ValueError) as exc:
                if attempt == 3:
                    raise ChatFileError('Cannot read shared chat.json: ' + str(exc)) from exc
                time.sleep(0.1 * (attempt + 1))
        if not isinstance(events, list):
            raise ChatFileError('The shared chat.json must contain a JSON array.')
        if not any(isinstance(e, dict) and e.get('id') == ORIGINAL_HISTORY_ANCHOR
                   for e in events):
            raise ChatFileError('Wrong chat.json. Use the original shared file shortcut.')
        return events


read_messages()  # Verify the correct shared history before showing the chat.


def write_event(event):
    """Best-effort shared JSON update; the file cannot safely be appended to.

    Writing additional bytes after the final ] would make JSON invalid.
    Re-read, append, write, then check the event was retained. Google Drive
    does not provide a cross-user transaction or atomic append here.
    """
    if not valid_signature(event):
        raise ChatFileError('Cannot save an unsigned chat event.')
    with file_lock:
        for attempt in range(4):
            records = read_messages()
            if any(isinstance(e, dict) and e.get('id') == event.get('id')
                   for e in records):
                return True
            records.append(event)
            try:
                # Never replace a Drive shortcut: update the existing file.
                with open(CHAT_FILE_PATH, 'w', encoding='utf-8') as fh:
                    json.dump(records, fh, ensure_ascii=False, indent=2)
                    fh.flush()
            except OSError as exc:
                raise ChatFileError('Shared chat.json write failed: ' + str(exc)) from exc
            time.sleep(0.15 * (attempt + 1))
            try:
                if any(isinstance(e, dict) and e.get('id') == event.get('id')
                       for e in read_messages()):
                    return True
            except ChatFileError:
                if attempt == 3:
                    raise
        return False


def event_now(kind, **fields):
    if kind in ('message', 'profile') and GOOGLE_ACCOUNT_NAME_AVAILABLE:
        # Only a permitted account prefix; no full email, OAuth token, or ID.
        fields.setdefault('account_username', GOOGLE_ACCOUNT_USERNAME)
    return sign_event(dict(id=str(uuid.uuid4()), kind=kind,
                           issued_at=int(time.time()),
                           issued_at_ns=time.time_ns(),
                           time=time.strftime('%H:%M:%S', time.localtime()),
                           user=username, **fields))


def valid_incoming_message(text):
    """Read-side protection: modified senders cannot bypass basic spam rules."""
    if not isinstance(text, str) or not text.strip() or len(text) > MESSAGE_LIMIT:
        return False
    if any((ord(ch) < 32 and ch not in ('\n', '\t')) or ord(ch) == 127
           for ch in text):
        return False
    if re.search(r'[\u202a-\u202e\u2066-\u2069]', text):
        return False
    return not is_gibberish(text)


def is_gibberish(text):
    """Low-false-positive flood filter, not a dictionary/spelling checker.

    Ordinary slang, multilingual text, links, code, punctuation, emojis, names,
    and even short consonant-heavy words are allowed. Block only obvious
    character/pattern floods; separate cooldown and length limits still apply.
    """
    if not text or not text.strip():
        return False
    # Twelve identical characters/emojis in a row is an obvious flood;
    # five periods, elongated 'noooo', smileys, etc. are fine.
    if re.search(r'(.)\1{11,}', text, re.DOTALL):
        return True
    # Repeated keyboard patterns: e.g. asdfasdfasdf... or abcabcabc...
    # Avoid flagging small, legitimate repeating expressions.
    if len(text) >= 32 and re.search(r'(.{2,8}?)\1{7,}', text, re.DOTALL):
        return True
    # One word pasted 12+ times in succession, including spaces/newlines.
    if re.search(r'(?i)(?<!\w)([\w]{1,24})(?:\s+\1){11,}(?!\w)', text):
        return True
    return False


def analyze(events):
    """Read signed chat identities, messages and self-claimed Verified titles.

    All legacy privileged role records are ignored.
    """
    verified = set()
    identities = {}
    messages = []
    by_number = {}
    seen_ids = set()
    post_number = 0
    reaction_state = {}
    rate_history = {}
    seen_any_signed_event = False
    claim_order = {}  # (identity, normalized name) -> first signed appearance
    seen_aliases = {}  # Signed aliases owned by a stable identity (for diagnostics)
    account_names = {}  # Google-derived account prefixes (locally asserted)
    custom_names = {}   # Signed privileged /rename profile events
    now = time.time()

    for index, e in enumerate(events):
        if not isinstance(e, dict):
            continue
        event_id = str(e.get('id', 'legacy-index-' + str(index)))
        if event_id in seen_ids:
            continue
        seen_ids.add(event_id)
        kind = str(e.get('kind', 'message'))
        signed = valid_signature(e)
        if signed:
            seen_any_signed_event = True
        sender = str(e.get('sender_key', '')) if signed else None
        # A nickname cannot confer a Verified title.
        clean_name = valid_username(e.get('user'))
        account_name = valid_username(e.get('account_username'))
        if signed and sender and kind in ('message', 'reaction', 'profile'):
            identities.setdefault(sender, fallback_username(sender))
            if account_name:
                account_names[sender] = account_name
                seen_aliases.setdefault(sender, set()).add(account_name)
                claim_order.setdefault((sender, account_name.casefold()), index)
            can_rename = sender in verified
            if can_rename and kind in ('message', 'profile') and clean_name:
                # Support older signed profile records and new explicit
                # rename records. New plain messages cannot silently rename.
                if ((kind == 'profile' and
                     (e.get('renamed') is True or not account_name)) or
                    (not account_name and kind == 'message')):
                    custom_names[sender] = clean_name
                    seen_aliases.setdefault(sender, set()).add(clean_name)
                    claim_order.setdefault((sender, clean_name.casefold()), index)
            identities[sender] = custom_names.get(
                sender, account_names.get(sender, fallback_username(sender)))

        if kind == 'role':
            # Only the holder of the signed identity can issue its blue check.
            # All other historical role-grant records are inert.
            if (signed and sender and e.get('role') == 'verified'
                    and e.get('self_verified') is True
                    and e.get('active') is True
                    and e.get('target_key') == sender):
                try:
                    if len(b64decode(sender)) == 32:
                        verified.add(sender)
                        identities.setdefault(sender, fallback_username(sender))
                except (TypeError, ValueError):
                    pass
            continue

        if kind == 'reaction':
            # Only a user with the actual identity private key can change
            # their own reactions. Replays are idempotent by event ID.
            if signed and sender:
                target_id, emoji, active = e.get('message_id'), e.get('emoji'), e.get('active')
                if (isinstance(target_id, str) and isinstance(emoji, str) and
                        1 <= len(emoji) <= 8 and type(active) is bool and
                        is_valid_emoji(emoji)):
                    reaction_state[(target_id, sender, emoji)] = active
            continue
        if kind == 'poll':
            post_number += 1
            continue
        if kind != 'message':
            continue
        # The original pre-identity history is retained. Once signed records
        # begin, new unsigned/invalid messages are ignored, rather than
        # allowing edits to chat.json to impersonate students.
        if not signed and seen_any_signed_event:
            continue
        message_text = e.get('text')
        if signed and not valid_incoming_message(message_text):
            continue
        if signed and sender:
            # Receiver-side limits: ordinary clients filter obvious bot
            # floods even when a modified Colab client skips its own cooldown.
            # Timestamps are client supplied, so this is mitigation, not proof
            # of a human sender or a server-enforced global rate limit.
            stamp = e.get('issued_at')
            if type(stamp) is int and abs(stamp - now) <= 86400:
                history = rate_history.setdefault(sender, [])
                history[:] = [(ts, body) for ts, body in history if stamp - ts <= 60]
                if (sum(1 for ts, _ in history if 0 <= stamp - ts <= 10) >= 7 or
                        sum(1 for ts, _ in history if 0 <= stamp - ts <= 60) >= 30 or
                        sum(1 for ts, body in history if 0 <= stamp - ts <= 30 and
                            body == message_text.casefold().strip()) >= 4):
                    continue
                history.append((stamp, message_text.casefold().strip()))
        post_number += 1
        item = {
            'number': post_number, 'id': event_id,
            'user': clean_name or fallback_username(sender),
            'text': str(e.get('text', '')),
            'time': str(e.get('time', '')),
            'sender_key': sender,
            'signed': signed,
            'reply_to': e.get('reply_to') if signed else None,
            'issued_at': e.get('issued_at') if signed else None,
        }
        messages.append(item)
        by_number[post_number] = item

    # One current display name per signed identity; only one identity can own
    # any nickname (case-insensitive / Unicode-normalized). This is enforced
    # on the receiver, even if a student edits their own Python notebook.
    # First signed claimant wins; no title can steal a student's name.
    # Concurrent Google Drive edits aren't atomic: this provides consistent
    # display, not a tamper-proof server-side name registry.
    name_claims = {}
    for pub, name in identities.items():
        if not GUEST_NAME_RE.fullmatch(name):
            name_claims.setdefault(name.casefold(), []).append(pub)
    name_owners = {
        normalized: min(claimants,
            key=lambda pub: (claim_order.get((pub, normalized), 10**18), pub))
        for normalized, claimants in name_claims.items()
    }
    # Stable, short Guest-123 names. Assign in the order identities first
    # appear in the shared history; collisions probe the next free number.
    # 900 three-digit + 90 two-digit names are available without duplicates.
    guest_names = {}
    occupied_guest_numbers = set()
    for pub in identities:
        digest = guest_digest(pub)
        candidate = 100 + int(digest[:8], 16) % 900
        for _ in range(900):
            if candidate not in occupied_guest_numbers:
                break
            candidate = 100 + (candidate - 99) % 900
        else:
            candidate = 10 + int(digest[8:16], 16) % 90
            for _ in range(90):
                if candidate not in occupied_guest_numbers:
                    break
                candidate = 10 + (candidate - 9) % 90
            else:
                # More than 990 signed identities cannot all have unique
                # two/three digit labels. Never reuse another Guest label.
                guest_names[pub] = 'Guest-' + digest[:16]
                continue
        occupied_guest_numbers.add(candidate)
        guest_names[pub] = f'Guest-{candidate}'

    # Name collisions are resolved with the stable signed identity, not by
    # switching a Google account user back to a random Guest label.
    display_names = {}
    taken_names = set(name_owners)
    for pub, name in identities.items():
        if GUEST_NAME_RE.fullmatch(name):
            display_names[pub] = guest_names[pub]
        elif name_owners.get(name.casefold()) == pub:
            display_names[pub] = name
        else:
            base = account_names.get(pub, name)
            digest = guest_digest(pub)
            chosen = None
            for digits in (4, 6, 8, 12, 16):
                candidate = base[:31-digits] + '-' + digest[:digits]
                if candidate.casefold() not in taken_names:
                    chosen = candidate
                    break
            if not chosen:
                chosen = guest_names[pub]
            display_names[pub] = chosen
            taken_names.add(chosen.casefold())

    # Display CURRENT signed aliases across message history, not a stale
    # 'ColabUser' from a different session. Legacy unsigned history is kept
    # visible, but cannot acquire role badges or impersonate a signed user.
    for msg in messages:
        sender = msg['sender_key']
        if sender:
            msg['user'] = display_names.get(sender, fallback_username(sender))
            msg['name_conflict'] = False
        else:
            original = msg['user']
            if original.casefold() in name_owners:
                msg['user'] = original + ' (legacy)'
            msg['name_conflict'] = False

    reaction_totals = {}
    valid_message_ids = {m['id'] for m in messages}
    for (target, reacting_pub, emoji), active in reaction_state.items():
        if active and target in valid_message_ids:
            counts = reaction_totals.setdefault(target, {})
            counts[emoji] = counts.get(emoji, 0) + 1
    for msg in messages:
        msg['reactions'] = reaction_totals.get(msg['id'], {})
        msg['my_reactions'] = [
            emoji for (target, user, emoji), enabled in reaction_state.items()
            if target == msg['id'] and user == identity_public and enabled
        ]
        sender = msg['sender_key']
        msg['verified'] = bool(sender and sender in verified)
    return dict(messages=messages, by_number=by_number,
                identities=identities, seen_aliases=seen_aliases, display_names=display_names,
                name_owners=name_owners, verified=verified)


def is_valid_emoji(emoji):
    # A small validation rule for typical emoji and multi-codepoint emoji.
    if not 1 <= len(emoji) <= 8 or re.search(r'[\x00-\x1f\x7f]', emoji):
        return False
    return any(ord(c) >= 0x2600 for c in emoji)


def note(value):
    """Insert a PRIVATE system entry inside this person's chat timeline."""
    try:
        anchor = len(latest_analysis()['messages'])
    except Exception:
        anchor = 0
    with state_lock:
        local_messages.append({
            'id': 'local-' + uuid.uuid4().hex,
            'number': None, 'anchor': anchor,
            'time': time.strftime('%H:%M:%S', time.localtime()),
            'user': 'System', 'text': str(value),
            'private': True, 'reactions': {},
        })
        del local_messages[:-40]


def popup(title, content):
    """Queue one modal for this browser only; never broadcast."""
    global next_popup
    next_popup = {'title': title, 'text': content, 'id': uuid.uuid4().hex}



HELP_TEXT = '''CHAT COMMANDS (only shown to you)
/help or /hellp            Open commands in a separate window
/rules                     Chat rules
/whoami or /id             Your signed identity and Verified status
/users                     Unique signed identities and display names
/verified                  List users with the blue checkmark
/mentions                  Find your recent @mentions
/search <filters>          Find by name, date, file or reply
/reply <#> <message>        Reply to a numbered post
/react <#> <emoji>          React to a message (/reacte also works)
/unreact <#> <emoji>        Remove your reaction
/mute <name> /unmute <name> Hide/show someone's posts locally
/muted                     View locally muted names
/last <1-100>              Change visible history length
/clear /reset              Clear your local view / restore it
/me <action>               Post a chat action
/stats                     Chat statistics
/ping                      Check shared file
/exit                      Close the chat

Search: from:Alex after:2026-10-01 before:2026-11-01 has:attachment in:replies
Verified users may use /rename <name> to claim a unique nickname.
The blue checkmark is self-claimed, not an independently verified identity.'''


RULES = '''SCHOOL CHAT RULES
1. No bullying, harassment, threats, or hateful content.
2. Do not post passwords, private addresses, personal contact details or records.
3. No impersonation. Reserved names are rejected; only signed self-claims get checkmarks.
4. Avoid spam and flooding.
5. Report serious issues to school staff. Any Editor can change or erase
   the shared chat.json file, so this is not a fully secure service.'''


def check_upstream_version(force=False):
    """New clients fail closed if a newer GitHub script has been published.

    This is a safety check for v5.16+ only. It CANNOT remotely stop old
    versions that never included a version gate.
    """
    global last_upstream_version_check, update_required, minimum_client_version
    now = time.monotonic()
    if (not force and
            now - last_upstream_version_check < UPSTREAM_VERSION_CHECK_SECONDS):
        return
    last_upstream_version_check = now
    try:
        request = urllib.request.Request(
            GITHUB_CHAT_SOURCE_URL,
            headers={'User-Agent': 'SchoolChat-VersionCheck',
                     'Cache-Control': 'no-cache', 'Range': 'bytes=0-255'})
        with urllib.request.urlopen(request, timeout=5) as response:
            heading = response.read(256).decode('utf-8', errors='replace')
        match = re.search(r'^# @title School ChatRoom v(\d+\.\d+)', heading)
        if not match:
            return
        latest_version = match.group(1)
        latest_tuple = tuple(int(x) for x in latest_version.split('.'))
        current_tuple = tuple(int(x) for x in CLIENT_VERSION.split('.'))
        if latest_tuple > current_tuple:
            update_required = True
            minimum_client_version = latest_version
    except (OSError, ValueError, urllib.error.URLError):
        # Connection failures do not prove a newer version exists.
        pass


def latest_analysis():
    global username
    result = analyze(read_messages())
    # The header and outgoing messages use the same effective shared name.
    username = result['display_names'].get(identity_public, GOOGLE_ACCOUNT_USERNAME)
    return result


def ensure_account_profile():
    """Register each identity once; update an available Google name when permitted.

    When extra account information is restricted, don't write a new profile on
    every restart. A single signed identity record is enough for Guest access.
    """
    records = read_messages()
    for record in reversed(records):
        if not (isinstance(record, dict) and
                record.get('sender_key') == identity_public and
                record.get('kind') in ('profile', 'message') and
                valid_signature(record)):
            continue
        if (not GOOGLE_ACCOUNT_NAME_AVAILABLE or
                record.get('account_username') == GOOGLE_ACCOUNT_USERNAME):
            return
    if not write_event(event_now('profile')):
        raise ChatFileError('Could not register your chat identity in shared '
                            'chat.json. Check Google Drive access and try again.')


ensure_account_profile()


def claim_self_verified():
    """Set one blue checkmark for the calling signed identity, never anyone else."""
    current = latest_analysis()
    if identity_public in current['verified']:
        return True
    event = event_now('role', role='verified', target_key=identity_public,
                      active=True, self_verified=True)
    return bool(write_event(event) and
                identity_public in latest_analysis()['verified'])


# A Google Drive mount is already required by the chat. Never prompt for
# extra school-account authorization to claim the self-verified checkmark.
if os.path.ismount('/content/drive'):
    try:
        claim_self_verified()
    except (ChatFileError, OSError):
        pass


def is_mentioned(text, name):
    if not name or not isinstance(text, str):
        return False
    return bool(re.search(r'(?<![\w@])@' + re.escape(name) + r'(?!\w)', text, flags=re.I))


def search_messages(query, messages):
    """Search by words and optional from:/after:/before:/has:/in: filters."""
    try:
        terms = shlex.split(query)
    except ValueError:
        raise ChatFileError('Unclosed quote in search. Example: /search "math quiz"')
    filters = {}
    words = []
    for term in terms:
        prefix, sep, value = term.partition(':')
        if sep and prefix.casefold() in ('from', 'after', 'before', 'has', 'in'):
            filters[prefix.casefold()] = value
        else:
            words.append(term)
    for field in ('after', 'before'):
        if field in filters:
            try:
                datetime.strptime(filters[field], '%Y-%m-%d')
            except ValueError:
                raise ChatFileError(f'Invalid {field}: date. Use YYYY-MM-DD.')
    if filters.get('has', '').casefold() not in ('', 'attachment', 'reaction'):
        raise ChatFileError('Valid has: filters: attachment or reaction.')
    if filters.get('in', '').casefold() not in ('', 'replies'):
        raise ChatFileError('Valid in: filter: replies.')
    results = []
    for message in messages:
        if 'from' in filters and filters['from'].casefold().lstrip('@') not in (
                message['user'].casefold(), short_id(message.get('sender_key'))):
            continue
        if words and not all(term.casefold() in message['text'].casefold()
                             for term in words):
            continue
        if filters.get('has', '').casefold() == 'attachment' and not message.get('attachment'):
            continue
        if filters.get('has', '').casefold() == 'reaction' and not message.get('reactions'):
            continue
        if filters.get('in', '').casefold() == 'replies' and not message.get('reply_to'):
            continue
        if 'after' in filters or 'before' in filters:
            stamp = message.get('issued_at')
            if not isinstance(stamp, int):
                continue
            date = datetime.fromtimestamp(stamp).strftime('%Y-%m-%d')
            if 'after' in filters and date < filters['after']:
                continue
            if 'before' in filters and date > filters['before']:
                continue
        results.append(message)
    return results[-20:]


def build_threads(messages, count, muted, cleared):
    """Group signed reply events under the parent, keeping recent threads first."""
    by_id = {m['id']: m for m in messages}
    roots = {}
    groups = {}
    for msg in messages:
        current = msg
        seen = set()
        while (current.get('reply_to') in by_id and
               current.get('reply_to') not in seen):
            seen.add(current['id'])
            current = by_id[current['reply_to']]
        root_id = current['id']
        roots[msg['id']] = root_id
        groups.setdefault(root_id, []).append(msg)
    candidates = []
    for root_id, nodes in groups.items():
        parent = by_id[root_id]
        available = [m for m in nodes if m['number'] > cleared and
                     m['user'].casefold() not in muted]
        if not available:
            continue
        replies = [m for m in available if m['id'] != root_id]
        candidates.append((max(m['number'] for m in available), {
            'root': parent, 'replies': replies[-40:],
            'total_replies': len(replies),
        }))
    candidates.sort(key=lambda item: item[0])
    return [group for _, group in candidates[-count:]]


def snapshot():
    global next_popup, running, update_notice_shown
    try:
        check_upstream_version()
        a = latest_analysis()
        if update_required:
            running = False
            if not update_notice_shown:
                update_notice_shown = True
                local_messages.append({
                    'id': 'local-update-required', 'number': None,
                    'anchor': len(a['messages']),
                    'time': time.strftime('%H:%M:%S', time.localtime()),
                    'user': 'System',
                    'text': 'New chat version required: v' + minimum_client_version
                            + '. Stop this cell and rerun the GitHub launcher.',
                    'private': True, 'reactions': {},
                })
        error = ''
    except ChatFileError as exc:
        a = {'messages': [], 'verified': set()}
        error = str(exc)
    with state_lock:
        # Place private system messages at the conversational position where
        # they occurred, not in a detached warning panel at the bottom.
        mixed = []
        for m in a['messages']:
            if m['number'] > clear_through and m['user'].casefold() not in muted_names:
                mixed.append((m['number'], 0, dict(m, mentioned=is_mentioned(m['text'], username))))
        for i, m in enumerate(local_messages):
            if m['anchor'] >= clear_through:
                mixed.append((m['anchor'], i + 1, dict(m)))
        mixed.sort(key=lambda item: (item[0], item[1]))
        shown = [item[2] for item in mixed[-visible_count:]]
        if error:
            shown.append({'id': 'read-error', 'number': None, 'private': True,
                          'time': time.strftime('%H:%M:%S'), 'user': 'System',
                          'text': error, 'reactions': {}})
        threads = build_threads(a['messages'], visible_count,
                                muted_names, clear_through)
        return JSON({'username': username, 'messages': shown,
                     'threads': threads,
                     'popup': next_popup,
                     'running': running,
                     'my_verified': identity_public in a['verified']})




def send_message(body, reply_to=None):
    global last_message_time
    now = time.time()
    if now - last_message_time < COOLDOWN_SECONDS:
        note('Slow down! Wait a moment before sending another message.')
        return False
    if len(body) > MESSAGE_LIMIT:
        note(f'Maximum message length is {MESSAGE_LIMIT} characters.')
        return False
    if is_gibberish(body):
        note('Message blocked: repeated characters/pattern flood. Normal text is allowed.')
        return False
    # Only block a *fifth* duplicate in 15 seconds. A couple of repeated
    # replies such as "hi" are normal and should not be misclassified.
    recent_sent_messages[:] = [(t, value) for t, value in recent_sent_messages
                               if now - t <= 15]
    normalized_body = ' '.join(body.split()).casefold()
    if sum(1 for _, value in recent_sent_messages if value == normalized_body) >= 4:
        note('Repeated-message flood blocked. You can send a different message.')
        return False
    # Refresh before signing: outgoing messages and the header always use
    # exactly the same effective nickname derived from the signed history.
    latest_analysis()
    evt = event_now('message', text=body)
    if reply_to:
        evt['reply_to'] = reply_to
        evt = sign_event({k: v for k, v in evt.items() if k != 'sig'})
    ok = write_event(evt)
    if ok:
        last_message_time = now
        recent_sent_messages.append((now, normalized_body))
    else:
        note('Delivery could not be confirmed. Check history before retrying.')
    return ok


def submit_message(raw):
    global username, running, visible_count, clear_through, last_message_time
    cleaned = str(raw)[:1800].strip()
    with state_lock:
        if not running or not cleaned:
            return snapshot()
        rename = re.fullmatch(r'/rename(?:\s+(.+))?', cleaned, flags=re.I)
        if rename:
            try:
                current = latest_analysis()
            except ChatFileError as exc:
                note('Cannot check role status right now: ' + str(exc))
                return snapshot()
            if identity_public not in current['verified']:
                note('Only Verified users can use /rename.')
                return snapshot()
            new_name = valid_username(rename.group(1))
            if not new_name:
                note('Nickname rejected: use 1–32 letters/numbers, spaces, _ - .; role tags, reserved names and invisible characters are not allowed.')
                return snapshot()
            other_names = {
                pub: name for pub, name in current['display_names'].items()
                if pub != identity_public and name.casefold() == new_name.casefold()
            }
            if other_names:
                note('That username is already taken. Choose a different name.')
                return snapshot()
            # Reserving the name via a signed event lets other clients and
            # fresh Colab sessions see the same username without a public post.
            previous_name = username
            username = new_name
            if not write_event(event_now('profile', renamed=True)):
                username = previous_name
                note('Could not save your new name. Please try again.')
            return snapshot()
        command, _, arg = cleaned.partition(' ')
        command, arg = command.casefold(), arg.strip()

        try:
            if command == '/vf':
                # Hidden from all command/help menus. Only the issuer's own
                # signed identity is eligible; no chat message is broadcast.
                if not claim_self_verified():
                    note('Could not save the checkmark to shared chat.json. Retry.')
                return snapshot()
            if command in ('/help', '/hellp'):
                popup('SCHOOL CHAT COMMANDS', HELP_TEXT)
                return snapshot()
            if command == '/rules':
                note(RULES)
                return snapshot()
            if command == '/ping':
                t = time.monotonic()
                read_messages()
                note(f'Shared Drive file readable. Read: {(time.monotonic() - t) * 1000:.0f} ms. '
                     f'Chat version: {CLIENT_VERSION}.')
                return snapshot()
            if command == '/exit':
                running = False
                return snapshot()

            a = latest_analysis()
            if command in ('/whoami', '/id'):
                roles = ['Verified'] if identity_public in a['verified'] else []
                note('Name: ' + username + '\nYour identity ID: ' + short_id(identity_public) +
                     '\nRoles: ' + (', '.join(roles) or 'Member') +
                     '\nThis ID does not change when you rename yourself. '
                     'Keep your Google Drive .schoolchat/identity.json private and backed up.')
                return snapshot()
            if command in ('/users', '/verified'):
                people = sorted(a['display_names'].items(), key=lambda pair: pair[1].casefold())
                if command == '/verified':
                    people = [(pub, name) for pub, name in people if pub in a['verified']]
                lines = [
                    ('☑ ' if pub in a['verified'] else '') +
                    f'@{name} | ID {short_id(pub)}'
                    for pub, name in people[-50:]
                ]
                note(('VERIFIED IDENTITIES' if command == '/verified' else 'SIGNED IDENTITIES') +
                     '\n' + ('\n'.join(lines) if lines else '(none)'))
                return snapshot()
            if command == '/stats':
                note(f'{len(a["messages"])} messages, '
                     f'{len(a["identities"])} signed identities. Not live user counts.')
                return snapshot()
            if command == '/mentions':
                hits = [m for m in a['messages'] if is_mentioned(m['text'], username)][-10:]
                note('RECENT MENTIONS\n' + ('\n'.join(
                    f'#{m["number"]} {m["user"]}: {m["text"][:100]}' for m in hits) or 'No mentions.'))
                return snapshot()
            if command in ('/search', '/find'):
                if not arg:
                    note('Search examples: /search homework  |  '
                         '/search from:Alex after:2026-10-01  |  '
                         '/search has:attachment  |  /search in:replies "quiz"')
                else:
                    hits = search_messages(arg, a['messages'])
                    note('SEARCH RESULTS (' + str(len(hits)) + ' most recent matches)\n'
                         + ('\n'.join(
                            f'#{m["number"]} [{m["time"]}] {m["user"]}: {m["text"][:110]}'
                            for m in hits) or 'No matches.'))
                return snapshot()
            if command == '/last':
                if arg.isdecimal() and 1 <= int(arg) <= 100:
                    visible_count = int(arg)
                    note('Now showing up to ' + arg + ' posts.')
                else:
                    note('Usage: /last <1-100>')
                return snapshot()
            if command == '/clear':
                clear_through = len(a['messages'])
                note('Your local view was cleared. Shared history was not deleted.')
                return snapshot()
            if command == '/reset':
                visible_count, clear_through = VISIBLE_MESSAGES, 0
                note('Restored your last 15 posts.')
                return snapshot()
            if command in ('/mute', '/unmute'):
                if not arg:
                    note(f'Usage: {command} <display name>')
                elif command == '/mute':
                    muted_names.add(arg.casefold())
                    note('Locally muted: ' + arg)
                else:
                    muted_names.discard(arg.casefold())
                    note('Unmuted: ' + arg)
                return snapshot()
            if command == '/muted':
                note('Locally muted names: ' + (', '.join(sorted(muted_names)) or '(none)'))
                return snapshot()

            if command in ('/react', '/reacte', '/unreact'):
                number, sep, emoji = arg.partition(' ')
                emoji = emoji.strip()
                target = a['by_number'].get(int(number)) if number.isdecimal() else None
                if not sep or not target or not is_valid_emoji(emoji):
                    note('Usage: /react <message#> <emoji>  or  /unreact <message#> <emoji>')
                    return snapshot()
                active = command != '/unreact'
                evt = event_now('reaction', message_id=target['id'], emoji=emoji, active=active)
                ok = write_event(evt)
                if not ok:
                    note('Reaction could not be confirmed. Try again.')
                return snapshot()

            reply_id = None
            if command == '/reply':
                number, sep, body = arg.partition(' ')
                target = a['by_number'].get(int(number)) if number.isdecimal() else None
                if not sep or not body.strip() or not target:
                    note('Usage: /reply <message#> <text>')
                    return snapshot()
                reply_id = target['id']
                body = body.strip()
            elif command == '/me':
                if not arg:
                    note('Usage: /me <action>')
                    return snapshot()
                body = '* ' + username + ' ' + arg
            elif command.startswith('/'):
                note('Unknown command. Use /help for supported chat commands.')
                return snapshot()
            else:
                body = cleaned
            send_message(body, reply_id)
            return snapshot()
        except (ChatFileError, OSError) as exc:
            note('[System]: ' + str(exc))
            return snapshot()
        except Exception as exc:
            note('[System]: Command failed: ' + str(exc))
            return snapshot()


# Per-session callbacks avoid collisions if the notebook is re-run.
SESSION = uuid.uuid4().hex
STATE_CALLBACK = f'school_chat.{SESSION}.state'
SEND_CALLBACK = f'school_chat.{SESSION}.send'
output.register_callback(STATE_CALLBACK, snapshot)
output.register_callback(SEND_CALLBACK, submit_message)

# One permanent chat display + one short input. Neither is cleared on updates.
CHAT_HTML = r'''<div id="school-chat-SESSION_ID" class="school-chat-v18">
<style>
.school-chat-v18 {font:13px/1.5 monospace; color:inherit; width:100%; max-width:100%; box-sizing:border-box}
.school-chat-v18 .chat-toolbar {display:flex; gap:7px; align-items:center; flex-wrap:wrap; padding:4px 0 7px}
.school-chat-v18 button,.school-chat-v18 .attach-label {font:12px monospace; padding:4px 9px; border:1px solid #64748b; border-radius:5px; color:inherit; background:transparent; cursor:pointer}
.school-chat-v18 button:hover,.school-chat-v18 .attach-label:hover {background:rgba(125,125,125,.14)}
.school-chat-v18 .chat-viewport {height:min(66vh,620px); min-height:220px; overflow:auto; overscroll-behavior:contain; padding:8px; border:1px solid #64748b55; border-radius:6px; white-space:pre-wrap; overflow-wrap:anywhere}
.school-chat-v18 .chat-line {padding:2px 0; overflow-wrap:anywhere}
.school-chat-v18 .chat-line.reply {border-left:2px solid #3b82f6; margin-left:12px; padding:3px 7px}
.school-chat-v18 .chat-actions {display:flex; align-items:center; flex-wrap:wrap; gap:4px; margin:3px 0 5px 16px}
.school-chat-v18 .chat-actions button {padding:1px 6px; font-size:11px; border-color:#64748b66}
.school-chat-v18 .chat-actions button.mine {border-color:#3498ff; color:#60a5fa}
.school-chat-v18 .thread-toggle {color:#60a5fa; border:none!important; font-weight:bold}
.school-chat-v18 .picker {display:flex; flex-wrap:wrap; gap:4px; margin:3px 0}
.school-chat-v18 .chat-compose {display:flex; align-items:stretch; width:min(720px,100%); gap:6px; margin-top:7px}
.school-chat-v18 .chat-compose input {flex:1; min-width:0; box-sizing:border-box; color:#111; background:#fff; border:1px solid #94a3b8; border-radius:4px; font:13px monospace; padding:8px; height:36px}
.school-chat-v18 .chat-compose button {min-height:36px; padding:5px 12px; background:#166534; color:#fff}
.school-chat-v18 .reply-hint {display:none; font-size:12px; padding:5px 2px; color:#60a5fa}
.school-chat-v18 .preview {display:block; max-width:min(320px,100%); max-height:220px; margin-top:5px; border-radius:5px; cursor:pointer}
.school-chat-v18 .file-control {display:none}
.school-chat-v18.is-expanded .chat-viewport {height:calc(90vh - 135px); max-height:none}
.school-chat-v18:fullscreen {padding:12px; background:#101820; color:#e5e7eb; height:100vh; overflow:hidden}
.school-chat-v18:fullscreen .chat-viewport {height:calc(100vh - 160px); max-height:none}
@media (max-width:600px) {
 .school-chat-v18 {font-size:12px}
 .school-chat-v18 .chat-viewport {height:60vh; padding:5px; min-height:240px}
 .school-chat-v18 .chat-toolbar {gap:5px}
 .school-chat-v18 button,.school-chat-v18 .attach-label {min-height:32px; padding:5px 7px}
 .school-chat-v18 .chat-compose input {font-size:16px; height:43px}
 .school-chat-v18 .chat-compose button {min-height:43px}
 .school-chat-v18 .chat-line.reply {margin-left:5px}
}
</style>
<div class="chat-toolbar">
  <button type="button" id="chat-expand-SESSION_ID" title="Toggle full-screen view">⛶ Full screen</button>
  <button type="button" id="chat-search-SESSION_ID" title="Advanced search">⌕ Search</button>
  <label class="attach-label" for="chat-file-SESSION_ID" title="Upload image, PDF or text to shared Google Drive">📎 Attach</label>
  <input class="file-control" id="chat-file-SESSION_ID" type="file" accept=".png,.jpg,.jpeg,.gif,.webp,.pdf,.txt,.md,image/png,image/jpeg,image/gif,image/webp,application/pdf,text/plain">
  <span id="chat-status-SESSION_ID" style="font-size:11px;color:#94a3b8">Drive event journal</span>
</div>
<div id="school-display-SESSION_ID" class="chat-viewport" aria-live="polite"></div>
<div id="chat-reply-hint-SESSION_ID" class="reply-hint"></div>
<form class="chat-compose" id="school-form-SESSION_ID" autocomplete="off">
  <input id="school-input-SESSION_ID" type="text" aria-label="Chat message" placeholder="Message or /command..." maxlength="1800" />
  <button type="submit">Send</button>
</form>
<div id="school-closed-SESSION_ID" style="display:none">Chat stopped — rerun the GitHub launcher to reconnect.</div>
<div id="school-help-fallback-SESSION_ID" style="display:none;margin-top:6px">
  <a id="school-help-link-SESSION_ID" href="#" target="_blank" rel="noopener" style="color:#38bdf8">Open chat help in a separate tab</a>
</div>
</div>
<script>
(() => {
  const root = document.getElementById('school-chat-SESSION_ID');
  const area = document.getElementById('school-display-SESSION_ID');
  const form = document.getElementById('school-form-SESSION_ID');
  const input = document.getElementById('school-input-SESSION_ID');
  const closed = document.getElementById('school-closed-SESSION_ID');
  const helpFallback = document.getElementById('school-help-fallback-SESSION_ID');
  const helpLink = document.getElementById('school-help-link-SESSION_ID');
  const replyHint = document.getElementById('chat-reply-hint-SESSION_ID');
  const fileInput = document.getElementById('chat-file-SESSION_ID');
  const status = document.getElementById('chat-status-SESSION_ID');
  const stateCallback = STATE_CALLBACK_JSON;
  const sendCallback = SEND_CALLBACK_JSON;
  const attachCallback = ATTACH_CALLBACK_JSON;
  let stopped = false, busy = false, sending = false, generation = 0;
  let pendingReplyNumber = null, lastPopupId = null, helpWindow = null, lastHelpURL = null;
  const expandedThreads = new Set(), activePickers = new Set();
  const openImagePreviews = new Set();
  const previewCache = new Map(), emojiChoices = ['👍','❤️','😂','🔥','🎉','💀','👀'];
  const colored = (value, color, bold=false) => {
    const x = document.createElement('span');
    x.textContent = String(value ?? '');
    x.style.color = color;
    if (bold) x.style.fontWeight = 'bold';
    return x;
  };
  const newline = (where=area) => where.appendChild(document.createTextNode('\n'));
  const replyData = x => x?.data?.['application/json'] ?? null;
  const makeButton = (label, action, title='') => {
    const b = document.createElement('button');
    b.type = 'button'; b.textContent = label; b.title = title || label;
    b.addEventListener('click', action); return b;
  };
  function createHelpWindow() {
    try {
      const w = window.open('', '_blank', 'width=690,height=640,resizable=yes,scrollbars=yes');
      if (w) {
        helpWindow = w;
        w.document.open();
        w.document.write('<!doctype html><html><head><meta charset="utf-8"><title>Chat commands</title></head>'+
          '<body style="background:#111827;color:#eee;font:14px monospace;padding:24px">'+
          '<h2 id="help-heading">Loading help...</h2><pre id="help-body" style="white-space:pre-wrap"></pre></body></html>');
        w.document.close();
      }
    } catch (_) { helpWindow = null; }
  }
  function showHelp(data) {
    if (helpWindow && !helpWindow.closed) {
      helpWindow.document.getElementById('help-heading').textContent = data.title || 'Commands';
      helpWindow.document.getElementById('help-body').textContent = data.text || '';
      helpWindow.focus(); helpWindow = null; helpFallback.style.display = 'none'; return;
    }
    const esc = s => String(s).replace(/[&<>"']/g, x => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[x]));
    const page = '<html><body style="background:#111827;color:#eee;font:14px monospace;padding:24px"><h2>'+
      esc(data.title || 'Commands')+'</h2><pre style="white-space:pre-wrap">'+
      esc(data.text || '')+'</pre></body></html>';
    if (lastHelpURL) URL.revokeObjectURL(lastHelpURL);
    lastHelpURL = URL.createObjectURL(new Blob([page], {type:'text/html'}));
    helpLink.href = lastHelpURL; helpFallback.style.display = 'block';
  }
  function mentionText(container, raw, myName) {
    const src = String(raw ?? '');
    if (!myName) {container.appendChild(document.createTextNode(src));return;}
    const esc = String(myName).replace(/[.*+?^$()|[\]{}\\]/g, '\\$&');
    const re = new RegExp('(^|[^\\w@])(@' + esc + ')(?!\\w)', 'gi');
    let last = 0, found;
    while ((found = re.exec(src)) !== null) {
      container.appendChild(document.createTextNode(src.slice(last, found.index)+found[1]));
      container.appendChild(colored(found[2], '#FBBF24', true));
      last = found.index + found[0].length;
    }
    container.appendChild(document.createTextNode(src.slice(last)));
  }
  function setReply(number, user) {
    pendingReplyNumber = number;
    replyHint.style.display = 'block';
    replyHint.replaceChildren(
      document.createTextNode('↳ Reply to #' + number + ' @' + user + '  '),
      makeButton('× Cancel', () => {
        pendingReplyNumber = null; replyHint.style.display = 'none'; input.focus();
      })
    );
    input.focus();
  }
  async function sendCommand(command) {
    if (sending || stopped) return;
    sending = true;
    try {
      const gen = ++generation;
      const reply = await google.colab.kernel.invokeFunction(sendCallback, [command], {});
      if (gen === generation) render(replyData(reply));
    } catch (error) {
      status.textContent = 'Drive send failed — retry';
      console.error(error);
    } finally { sending = false; input.focus(); }
  }
  async function openAttachment(msg) {
    let existing = previewCache.get(msg.id);
    if (!existing) {
      status.textContent = 'Opening attachment from Drive...';
      try {
        const result = replyData(await google.colab.kernel.invokeFunction(attachCallback, [msg.id], {}));
        if (!result || result.error) throw Error(result?.error || 'Attachment unavailable');
        const binary = atob(result.base64);
        const bytes = new Uint8Array(binary.length);
        for (let i=0; i<binary.length; i++) bytes[i] = binary.charCodeAt(i);
        const blob = new Blob([bytes], {type: result.mime});
        existing = {url:URL.createObjectURL(blob),name:result.name,mime:result.mime};
        previewCache.set(msg.id, existing);
        status.textContent = 'Attachment ready';
      } catch (error) {
        status.textContent = 'Attachment: ' + String(error.message || error);
        return;
      }
    }
    if (existing.mime.startsWith('image/')) {
      if (openImagePreviews.has(msg.id)) openImagePreviews.delete(msg.id);
      else openImagePreviews.add(msg.id);
      render(lastState);
    } else {
      const a = document.createElement('a');
      a.href = existing.url; a.download = existing.name; a.rel = 'noopener';
      a.click();
    }
  }
  function renderMessage(msg, name, isReply=false) {
    const wrapper = document.createElement('div');
    wrapper.className = 'chat-line' + (isReply ? ' reply' : '');
    wrapper.appendChild(colored('[' + (msg.time || '') + ']', '#22d3ee'));
    if (!msg.private) wrapper.appendChild(colored(' #' + msg.number + ' ', '#94a3b8'));
    else wrapper.appendChild(document.createTextNode(' '));
    if (!msg.private && msg.verified) wrapper.appendChild(colored('☑ ', '#3498FF', true));
    wrapper.appendChild(colored(msg.user || '', msg.private ? '#fb7185' : '#4ade80', true));
    wrapper.appendChild(document.createTextNode(': '));
    if (msg.private) wrapper.appendChild(colored(msg.text || '', '#fb7185'));
    else mentionText(wrapper, msg.text || '', name);
    area.appendChild(wrapper);
    if (msg.private) return;
    const actions = document.createElement('div');
    actions.className = 'chat-actions';
    const reactions = msg.reactions || {};
    for (const [emoji,count] of Object.entries(reactions)) {
      if (count <= 0) continue;
      const mine = (msg.my_reactions || []).includes(emoji);
      const b = makeButton(emoji + ' ' + count,
        () => sendCommand((mine ? '/unreact ' : '/react ') + msg.number + ' ' + emoji),
        mine ? 'Remove your reaction' : 'React');
      if (mine) b.className = 'mine';
      actions.appendChild(b);
    }
    actions.appendChild(makeButton('＋ React', () => {
      if (activePickers.has(msg.id)) activePickers.delete(msg.id);
      else activePickers.add(msg.id);
      render(lastState);
    }));
    actions.appendChild(makeButton('↳ Reply', () => setReply(msg.number,msg.user)));
    if (msg.attachment) {
      actions.dataset.fileId = msg.id;
      actions.appendChild(makeButton('📎 Open ' + String(msg.attachment.name||'file'),
        () => openAttachment(msg), 'Read attachment from shared Drive'));
    }
    area.appendChild(actions);
    if (openImagePreviews.has(msg.id) && previewCache.has(msg.id)) {
      const current = previewCache.get(msg.id);
      const image = document.createElement('img');
      image.className = 'preview'; image.src = current.url; image.alt = current.name;
      image.title = 'Click to close preview';
      image.onclick = () => {openImagePreviews.delete(msg.id);render(lastState);};
      area.appendChild(image);
    }
    if (activePickers.has(msg.id)) {
      const picker = document.createElement('div');picker.className = 'picker';
      picker.style.marginLeft = '16px';
      for (const emoji of emojiChoices) {
        const mine=(msg.my_reactions || []).includes(emoji);
        picker.appendChild(makeButton(emoji, () => {
          activePickers.delete(msg.id);
          sendCommand((mine?'/unreact ':'/react ')+msg.number+' '+emoji);
        }));
      }
      area.appendChild(picker);
    }
  }
  let lastState = null;
  function render(data) {
    if (!data || !root.isConnected) return;
    lastState = data;
    const atBottom = area.scrollTop + area.clientHeight >= area.scrollHeight - 75;
    const previousY = area.scrollTop;
    area.replaceChildren();
    area.appendChild(document.createTextNode('--- Connected as: '));
    if (data.my_verified) area.appendChild(colored('☑ ', '#3498FF', true));
    area.appendChild(colored(data.username||'', '#4ade80', true));
    area.appendChild(document.createTextNode(' ---'));
    newline();area.appendChild(colored('Messages · reactions · threads · Drive attachments','#94a3b8'));
    newline();newline();
    if (Array.isArray(data.threads)) {
      for (const thread of data.threads) {
        renderMessage(thread.root, data.username);
        const replies = Array.isArray(thread.replies)?thread.replies:[];
        if (thread.total_replies > 0) {
          const toggle = makeButton(
            (expandedThreads.has(thread.root.id) ? '▾ Hide' : '▸ Show')+
            ' ' + thread.total_replies + ' repl' + (thread.total_replies===1?'y':'ies'),
            () => {if (expandedThreads.has(thread.root.id)) expandedThreads.delete(thread.root.id);
                   else expandedThreads.add(thread.root.id);render(lastState);});
          toggle.className = 'thread-toggle';
          toggle.style.marginLeft = '12px';area.appendChild(toggle);newline();
          if (expandedThreads.has(thread.root.id)) {
            for (const reply of replies) renderMessage(reply,data.username,true);
          }
        }
      }
      for (const m of (data.messages||[]).filter(x=>x.private)) renderMessage(m,data.username);
    } else {
      for (const m of (data.messages||[])) renderMessage(m,data.username);
    }
    if (data.popup && data.popup.id !== lastPopupId) {
      lastPopupId = data.popup.id;showHelp(data.popup);
    }
    if (atBottom) area.scrollTop = area.scrollHeight;
    else area.scrollTop = previousY;
    if (!data.running) {
      stopped=true;form.style.display='none';closed.style.display='block';
    }
  }
  async function refreshChat() {
    if (busy || sending || stopped || !root.isConnected) return;
    busy=true;
    try {
      const gen=++generation;
      const data=replyData(await google.colab.kernel.invokeFunction(stateCallback, [], {}));
      if (gen===generation) render(data);
    } catch(error) {
      console.debug('Drive refresh pending:',error);
    } finally {busy=false;}
  }
  document.getElementById('chat-expand-SESSION_ID').onclick = async () => {
    if (document.fullscreenElement===root) {
      try {await document.exitFullscreen();return;} catch(_) {}
    }
    try {if (root.requestFullscreen) {await root.requestFullscreen();return;}} catch(_) {}
    root.classList.toggle('is-expanded');
    status.textContent=root.classList.contains('is-expanded')?'Expanded view':'Regular view';
  };
  document.getElementById('chat-search-SESSION_ID').onclick=()=>{
    input.value='/search ';input.focus();input.setSelectionRange(input.value.length,input.value.length);
    status.textContent='Try from:Alex after:2026-10-01 has:attachment in:replies';
  };
  fileInput.addEventListener('change', async () => {
    const file=fileInput.files?.[0];if(!file)return;
    fileInput.value='';
    if(file.size>2*1024*1024 || file.size<=0) {
      status.textContent='File must be 1 byte to 2 MB';return;
    }
    const ext=(file.name.split('.').pop()||'').toLowerCase();
    const types={png:'image/png',jpg:'image/jpeg',jpeg:'image/jpeg',gif:'image/gif',
                 webp:'image/webp',pdf:'application/pdf',txt:'text/plain',md:'text/markdown'};
    const mime=types[ext];
    if(!mime){status.textContent='Only PNG, JPG, WEBP, GIF, PDF, TXT or MD';return;}
    status.textContent='Uploading ' + file.name + ' to Drive...';
    try {
      const base64 = await new Promise((resolve,reject)=>{
        const reader=new FileReader();
        reader.onerror=()=>reject(Error('Cannot read file'));
        reader.onload=()=>resolve(String(reader.result).split(',')[1]||'');
        reader.readAsDataURL(file);
      });
      const reply=replyData(await google.colab.kernel.invokeFunction(
        sendCallback,[{action:'upload_attachment',name:file.name,mime,base64}],{}));
      render(reply);status.textContent='Upload finished (check chat for any error)';
    } catch(error) {
      status.textContent='Upload failed: '+String(error.message||error);
    }
  });
  form.addEventListener('submit',async event=>{
    event.preventDefault();if(stopped||sending)return;
    const typed=input.value.trim();if(!typed)return;
    if(['/help','/hellp'].includes(typed.toLowerCase()))createHelpWindow();
    let outgoing=typed;
    if(pendingReplyNumber!==null && !typed.startsWith('/')) {
      outgoing='/reply '+pendingReplyNumber+' '+typed;
    }
    input.value='';
    await sendCommand(outgoing);
    if(outgoing!==typed) {
      pendingReplyNumber=null;replyHint.style.display='none';
    }
  });
  refreshChat();
  const timer=setInterval(()=>{
    if(!root.isConnected||stopped){clearInterval(timer);return;}
    refreshChat();
  }, REFRESHMS);
  input.focus();
})();
</script>'''
CHAT_HTML = CHAT_HTML.replace('SESSION_ID', SESSION)
CHAT_HTML = CHAT_HTML.replace('STATE_CALLBACK_JSON', json.dumps(STATE_CALLBACK))
CHAT_HTML = CHAT_HTML.replace('SEND_CALLBACK_JSON', json.dumps(SEND_CALLBACK))
CHAT_HTML = CHAT_HTML.replace('WRAPCH', str(WRAP_WIDTH_CH))
CHAT_HTML = CHAT_HTML.replace('INPUTPX', str(INPUT_WIDTH_PX))
CHAT_HTML = CHAT_HTML.replace('REFRESHMS', str(REFRESH_INTERVAL_MS))
display(HTML(CHAT_HTML))
