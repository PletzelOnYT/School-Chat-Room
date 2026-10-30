# @title School ChatRoom v5.11 — School terminal chat with Vercel owner login
"""Google Colab school chat — the same shared Google Drive chat.json.

The ONLY hosted API is for checking the owner's key and signing role grants.
Ordinary chat messages, mentions, replies and reads never go to Vercel.

Owner setup: deploy the included updated Vercel function with two PRIVATE environment
variables. The client securely fetches the PUBLIC verification key automatically. Run chat.
Type /owner, enter your key in the password-style input. No file uploads needed.

The shared Drive file must still be a SHORTCUT to the same original chat.json.
Everyone needs this v5 client for the updated server-backed badges/roles.
Shared Drive Editor access remains a security limitation for history integrity.
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
# Only PUBLIC configuration goes in the distributed notebook.
# Never put CHAT_OWNER_KEY or CHAT_SIGNING_SEED here!
OWNER_AUTH_URL = 'https://chat-room-owner-api-xi.vercel.app/api/owner'
# Optional public key pin. Leave blank to fetch it from your Vercel server.
# All students can use the same notebook with no manual public-key setup.
OWNER_SERVER_PUBLIC_KEY = ''

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
ROLE_RECOVERY_PATH = os.path.join(PRIVATE_FOLDER, 'role_recovery.json')

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
owner_access_key = None  # Only in memory; never written to chat.json or private Drive.
local_messages = []  # Private, inline System messages, NEVER sent to Drive.
next_popup = None  # One-shot private /help or /modhelp popup
visible_count = VISIBLE_MESSAGES
muted_names = set()
clear_through = 0
last_recovery_check = 0.0


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


def load_server_public_key():
    """Download the public verification key from our HTTPS Owner API.

    Only authentication/role verification uses Vercel; chat messages remain
    exclusively in the shared Google Drive chat.json file. An optional pinned
    public key is honored and checked against the server's advertised key.
    """
    pinned = OWNER_SERVER_PUBLIC_KEY.strip()
    if pinned.startswith('PASTE_PUBLIC_KEY'):
        pinned = ''
    if pinned:
        try:
            if len(base64.b64decode(pinned, validate=True)) != 32:
                raise ValueError('must be 32 bytes')
        except (ValueError, TypeError) as exc:
            raise ChatFileError('Invalid configured Owner public key.') from exc
    if not OWNER_AUTH_URL.startswith('https://'):
        raise ChatFileError('Owner API must use HTTPS.')
    try:
        request = urllib.request.Request(OWNER_AUTH_URL, headers={'Accept': 'application/json'})
        with urllib.request.urlopen(request, timeout=10) as response:
            details = json.load(response)
        if not isinstance(details, dict) or details.get('status') != 'online':
            raise ChatFileError('Owner API health check did not return online.')
        remote = details.get('public_key', '')
        if remote:
            if not isinstance(remote, str) or len(b64decode(remote)) != 32:
                raise ChatFileError('Owner API returned an invalid public key.')
            if pinned and remote != pinned:
                raise ChatFileError('Configured Owner public key does not match the Vercel server.')
            return remote
        # Compatibility with the original API until it is redeployed.
        if pinned:
            return pinned
        raise ChatFileError('Owner API needs the v5.2 update to publish its PUBLIC verification key.')
    except (OSError, ValueError, urllib.error.URLError) as exc:
        if pinned:
            return pinned
        raise ChatFileError('Could not fetch Owner verification key from Vercel: ' + str(exc)) from exc


# Fail closed for roles if the API is offline; ordinary chat still works.
SERVER_PUBLIC_KEY = ''
SERVER_KEY_ERROR = ''
try:
    SERVER_PUBLIC_KEY = load_server_public_key()
except ChatFileError as exc:
    SERVER_KEY_ERROR = str(exc)


identity_private, identity_public = load_identity()


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


# Use ONE effective nickname for the chat header and outgoing messages.
# A new identity always begins with a random-looking unique Guest fingerprint.
username = fallback_username(identity_public)


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


def trusted_server_event(event):
    """A server response alone is not enough: verify its Ed25519 signature."""
    return (isinstance(event, dict) and
            bool(SERVER_PUBLIC_KEY) and event.get('sender_key') == SERVER_PUBLIC_KEY and
            valid_signature(event))


def call_owner_server(action, key, **data):
    """Only moderator/owner commands use Vercel. Chat itself stays Drive-only."""
    if not SERVER_PUBLIC_KEY:
        raise ChatFileError(SERVER_KEY_ERROR or 'Owner public key is unavailable. Restart this Colab cell.')
    if not OWNER_AUTH_URL.startswith('https://'):
        raise ChatFileError('Owner authorization must use an HTTPS URL.')
    payload = json.dumps(dict(action=action, key=key, **data)).encode('utf-8')
    req = urllib.request.Request(OWNER_AUTH_URL, payload,
                                 headers={'Content-Type': 'application/json'},
                                 method='POST')
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            obj = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            raise ChatFileError('Access DENIED: incorrect owner key.') from exc
        raise ChatFileError(f'Owner server returned HTTP {exc.code}.') from exc
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise ChatFileError('Could not contact the owner server securely: ' + str(exc)) from exc
    event = obj.get('event') if isinstance(obj, dict) else None
    if not isinstance(obj, dict) or obj.get('status') != 'granted' or not trusted_server_event(event):
        raise ChatFileError('Owner server did not return a valid signed grant.')
    if event.get('kind') != 'role' or event.get('target_key') != data.get('target_key'):
        raise ChatFileError('Owner server grant does not match requested identity.')
    if action == 'login':
        issued, expiry = event.get('issued_at'), event.get('expires_at')
        now = time.time()
        if (event.get('role') != 'owner' or event.get('active') is not True or
                not isinstance(issued, int) or not isinstance(expiry, int) or
                abs(now - issued) > 300 or expiry <= now or expiry > now + 9*3600):
            raise ChatFileError('Invalid owner session grant.')
    else:
        if event.get('role') != data.get('role') or event.get('active') != data.get('active'):
            raise ChatFileError('Owner server grant does not match the role action.')
    return event


def read_messages():
    """Retry transient partial reads; NEVER replace an unreadable shared file."""
    with file_lock:
        for attempt in range(3):
            try:
                with open(CHAT_FILE_PATH, encoding='utf-8') as f:
                    events = json.load(f)
                break
            except (OSError, ValueError) as exc:
                if attempt == 2:
                    raise ChatFileError('Cannot read shared chat.json: ' + str(exc)) from exc
                time.sleep(0.06 * (attempt + 1))
        if not isinstance(events, list):
            raise ChatFileError('Shared chat.json must contain a JSON array.')
        if not any(isinstance(e, dict) and e.get('id') == ORIGINAL_HISTORY_ANCHOR
                   for e in events):
            raise ChatFileError('Wrong chat.json. Use a shortcut to the ORIGINAL shared file.')
        return events


read_messages()  # Fail before opening the chat if the wrong file is selected.


def write_event(event):
    """Best-effort append; other Colab runtimes can still race on Google Drive."""
    with file_lock:
        for attempt in range(3):
            entries = read_messages()
            if not any(isinstance(e, dict) and e.get('id') == event['id'] for e in entries):
                entries.append(event)
                try:
                    # Never os.replace(): it could replace a Drive shortcut itself.
                    with open(CHAT_FILE_PATH, 'w', encoding='utf-8') as f:
                        json.dump(entries, f, indent=2, ensure_ascii=False)
                        f.flush()
                except OSError as exc:
                    raise ChatFileError('Shared file write failed: ' + str(exc)) from exc
            try:
                if any(isinstance(e, dict) and e.get('id') == event['id']
                       for e in read_messages()):
                    return True
            except ChatFileError:
                if attempt == 2:
                    raise
            time.sleep(0.15 * (attempt + 1))
        return False


def event_now(kind, **fields):
    return sign_event(dict(id=str(uuid.uuid4()), kind=kind,
                           issued_at=int(time.time()),
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
    """Verify identities and signed permission records, and render regular chat.

    The owner role can be claimed by knowing the server-side secret, but only
    the SERVER can sign owner/mod role records. Mods can sign verified records.
    Old poll records remain ignored; signed reactions are supported.
    """
    owners = set()
    moderators = set()
    verified = set()
    banned = set()
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
        from_server = bool(SERVER_PUBLIC_KEY) and sender == SERVER_PUBLIC_KEY and signed
        # Signed people are identified by keys, not Colab usernames.
        # Only someone who is VERIFIED at this point in the signed event
        # history may claim/update a custom nickname. Checking here as well
        # as in /rename prevents modified clients from bypassing the rule
        # by writing a forged nickname to a signed message or profile event.
        clean_name = valid_username(e.get('user'))
        if signed and not from_server and kind in ('message', 'reaction', 'profile'):
            identities.setdefault(sender, fallback_username(sender))
            if kind in ('message', 'profile') and sender in verified and sender not in banned and clean_name:
                identities[sender] = clean_name
                seen_aliases.setdefault(sender, set()).add(clean_name)
                claim_order.setdefault((sender, clean_name.casefold()), index)

        if kind == 'role':
            if not signed:
                continue
            target = e.get('target_key')
            role = e.get('role')
            active = e.get('active')
            if not isinstance(target, str) or type(active) is not bool:
                continue
            try:
                if len(b64decode(target)) != 32:
                    continue
            except Exception:
                continue
            if from_server:
                if role == 'owner':
                    # Owner badge expires, so rotating the key can end access.
                    issued, expiry = e.get('issued_at'), e.get('expires_at')
                    if (active and isinstance(issued, int) and
                            isinstance(expiry, int) and issued <= now + 300 and
                            expiry > now and expiry - issued <= 9*3600):
                        owners.add(target)
                elif role == 'moderator':
                    if active:
                        moderators.add(target)
                    else:
                        moderators.discard(target)
                elif role == 'verified':
                    if active:
                        verified.add(target)
                    else:
                        verified.discard(target)
                elif role == 'banned':
                    if active:
                        banned.add(target)
                    else:
                        banned.discard(target)
            elif role == 'verified' and sender in moderators:
                if active:
                    verified.add(target)
                else:
                    verified.discard(target)
            continue

        if kind == 'reaction':
            # Only a user with the actual identity private key can change
            # their own reactions. Replays are idempotent by event ID.
            if signed and sender and not from_server:
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
        if signed and sender in banned:
            continue  # Globally hide posts sent while banned
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
            'sender_key': sender if not from_server else None,
            'signed': signed,
            'reply_to': e.get('reply_to') if signed else None
        }
        messages.append(item)
        by_number[post_number] = item

    # One current display name per signed identity; only one identity can own
    # any nickname (case-insensitive / Unicode-normalized). This is enforced
    # on the receiver, even if a student edits their own Python notebook.
    # First signed claimant wins; OWNER status does not steal a student's name.
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

    display_names = {}
    for pub, name in identities.items():
        # The default Guest label is not a user-claimed nickname. Treat it as
        # a valid displayed name, not as an invalid/reserved custom nickname.
        if GUEST_NAME_RE.fullmatch(name):
            display_names[pub] = guest_names[pub]
        else:
            display_names[pub] = (name if name_owners.get(name.casefold()) == pub
                                  else guest_names[pub])

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
        sender = msg['sender_key']
        msg['verified'] = bool(sender and sender in verified)
        msg['owner'] = bool(sender and sender in owners)
        msg['moderator'] = bool(sender and sender in moderators)
    return dict(messages=messages, by_number=by_number,
                identities=identities, seen_aliases=seen_aliases, display_names=display_names,
                name_owners=name_owners, owners=owners,
                moderators=moderators, verified=verified, banned=banned)


def is_valid_emoji(emoji):
    # A small validation rule for typical emoji and multi-codepoint emoji.
    if not 1 <= len(emoji) <= 8 or re.search(r'[\x00-\x1f\x7f]', emoji):
        return False
    return any(ord(c) >= 0x2600 for c in emoji)


def load_role_recovery():
    try:
        with open(ROLE_RECOVERY_PATH, encoding='utf-8') as fh:
            entries = json.load(fh)
        if isinstance(entries, list):
            return [item for item in entries if isinstance(item, dict) and
                    isinstance(item.get('event'), dict)]
    except (OSError, ValueError):
        pass
    return []


def persist_role_recovery(entries):
    """Atomic replacement is safe inside OWN private Drive, not chat.json."""
    os.makedirs(PRIVATE_FOLDER, exist_ok=True)
    temporary = ROLE_RECOVERY_PATH + '.tmp'
    with open(temporary, 'w', encoding='utf-8') as fh:
        json.dump(entries[-30:], fh, ensure_ascii=False, indent=2)
    os.replace(temporary, ROLE_RECOVERY_PATH)


def queue_role_recovery(grant):
    if not isinstance(grant, dict) or grant.get('kind') != 'role' or not valid_signature(grant):
        raise ChatFileError('Cannot save an unsigned role grant.')
    with file_lock:
        existing = load_role_recovery()
        existing = [x for x in existing if (x.get('event', {}).get('role'),
                                             x.get('event', {}).get('target_key')) !=
                                            (grant.get('role'), grant.get('target_key'))]
        existing.append({'event': grant, 'saved_at': time.time()})
        persist_role_recovery(existing)


def reconcile_role_recovery(force=False):
    """Best-effort self-healing for role events lost to other Colab writers.

    While this issuer's Colab runs, the latest signed grant is repaired if it
    disappears. A newer role decision prevents replaying an older grant.
    The queue expires after 24h, and never contains an Owner password.
    """
    global last_recovery_check
    now_mono = time.monotonic()
    if not force and now_mono - last_recovery_check < 4:
        return
    last_recovery_check = now_mono
    with file_lock:
        queue = load_role_recovery()
        if not queue:
            return
        events = read_messages()
        present = {str(e.get('id')) for e in events if isinstance(e, dict)}
        keep = []
        for item in queue[-30:]:
            event = item['event']
            age = time.time() - float(item.get('saved_at', 0))
            if age < 0 or age > 86400 or not valid_signature(event):
                continue
            if event.get('kind') != 'role' or event.get('role') not in ('moderator', 'verified', 'banned'):
                continue
            # A later signed event for this role and target supersedes the
            # grant. Never resurrect an old grant after someone revoked it.
            competing = [e for e in events if isinstance(e, dict) and
                         e.get('kind') == 'role' and
                         e.get('target_key') == event.get('target_key') and
                         e.get('role') == event.get('role') and
                         e.get('id') != event.get('id') and valid_signature(e)]
            if any(int(e.get('issued_at', 0)) > int(event.get('issued_at', 0))
                   for e in competing):
                continue
            keep.append(item)
            if str(event.get('id')) not in present:
                # Only restore exactly the existing cryptographically signed
                # event. Never issue new authority or forge a badge.
                if write_event(event):
                    present.add(str(event.get('id')))
        if len(keep) != len(queue):
            persist_role_recovery(keep)


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


if SERVER_KEY_ERROR:
    note('Owner tags unavailable: ' + SERVER_KEY_ERROR)


def popup(title, content):
    """Queue one modal for this browser only; never broadcast."""
    global next_popup
    next_popup = {'title': title, 'text': content, 'id': uuid.uuid4().hex}



HELP_TEXT = '''CHAT COMMANDS (only shown to you)
/help or /hellp            Open commands in a separate window
/rules                     Chat rules
/whoami or /id             Your permanent signed identity ID and roles
/users                     Unique student identities and display names
/verified                  List verified identities
/rolecheck <name or ID>    Check a student's effective shared roles
/mentions                  Find your recent @mentions
/search <text>             Search chat (/find also works)
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
/modhelp                   Moderator commands (mods only)
/banlist                   View banned accounts
/owner                     Owner login (asks for private key)
/exit                      Close the chat

Commands and role tags cannot be nicknames.
@mention a username in a normal message to highlight it for that person.
Numbers (#) appear next to chat messages. Moderator role changes are saved
into the SAME shared chat.json.'''

MOD_HELP = '''MODERATOR COMMANDS (signed changes, visible to everyone)
/verify <name or ID>        Assign Verified badge to a signed identity
/unverify <name or ID>      Revoke Verified badge
/addmod <name or ID>        Owner only: assign Moderator role
/removemod <name or ID>     Owner only: revoke Moderator role
/rolecheck <name or ID>    Check the exact effective roles for an identity
/ban <name or ID>          Owner only: block posts from a signed identity
/unban <name or ID>        Owner only: remove a ban
/banlist                   List blocked signed identities
/owner                     Privately authenticate as Owner

Use /whoami on the OTHER student's Colab to confirm their ID before you grant a role.
Use the unique ID from /users to avoid verifying the wrong student. Roles are tied
to a private identity key, never to a changeable display name.'''

RULES = '''SCHOOL CHAT RULES
1. No bullying, harassment, threats, or hateful content.
2. Do not post passwords, private addresses, personal contact details or records.
3. No impersonation. Reserved role-tag names are rejected; only signed roles get badges.
4. Avoid spam and flooding.
5. Report serious issues to school staff. The shared JSON can be edited or
   erased by anybody who has Editor access; moderation isn't tamper-proof.'''


def latest_analysis():
    global username
    result = analyze(read_messages())
    # Keep the input/header nickname equal to the receiver-verified nickname.
    # If a competing student already owns that name, both the header and all
    # future messages use this identity's unique Guest ID instead.
    username = result['display_names'].get(identity_public)
    if not username:
        # A first-time student has not sent a signed event yet; choose a
        # Guest number absent from the current shared history as well.
        reserved = {name.casefold() for name in result['display_names'].values()}
        number = int(fallback_username(identity_public).split('-')[1])
        for _ in range(900):
            candidate = f'Guest-{number}'
            if candidate.casefold() not in reserved:
                username = candidate
                break
            number = 100 + (number - 99) % 900
        if not username:
            number = 10 + int(guest_digest(identity_public)[8:16], 16) % 90
            for _ in range(90):
                candidate = f'Guest-{number}'
                if candidate.casefold() not in reserved:
                    username = candidate
                    break
                number = 10 + (number - 9) % 90
        if not username:
            username = 'Guest-' + guest_digest(identity_public)[:16]
    if username.casefold() in GENERIC_USERNAMES:
        username = fallback_username(identity_public)
    return result


def resolve_target(arg, a):
    """Resolve a stable identity, never infer moderator rights from a name.

    Accept /users' 12-character hex ID (or a unique >=8-character prefix),
    the current unique nickname, or a Guest-<ID>. Refuse ambiguous names and
    require a signed identity; profile renames never change the target key.
    """
    needle = arg.strip()
    if needle.startswith('@'):
        needle = needle[1:]
    needle_lower = needle.casefold()
    if not needle_lower:
        return None, 'Provide a username or ID. Use /users to find it.'
    people = a['display_names']
    # An ID is much safer than a nickname, and the name can later change.
    id_needle = needle_lower.removeprefix('guest-')
    if re.fullmatch(r'[0-9a-f]{8,12}', id_needle):
        ids = [pub for pub in people if short_id(pub).startswith(id_needle)]
        if len(ids) == 1:
            return ids[0], None
        if len(ids) > 1:
            return None, 'That ID prefix matches multiple students. Use the full 12-character ID.'
    matches = [pub for pub, name in people.items()
               if name.casefold() == needle_lower]
    if len(matches) == 1:
        return matches[0], None
    if len(matches) > 1:
        return None, 'Several signed students match that name. Use the ID from /users.'
    return None, ('No signed student found for that name/ID. Ask them to open '
                  'the chat, send one message, and copy their ID from /whoami.')


def is_mentioned(text, name):
    if not name or not isinstance(text, str):
        return False
    return bool(re.search(r'(?<![\w@])@' + re.escape(name) + r'(?!\w)', text, flags=re.I))


def snapshot():
    global next_popup
    try:
        # Reconcile queued signed grants that another Drive writer may have
        # overwritten. No secret owner password is saved to disk.
        reconcile_role_recovery()
        a = latest_analysis()
        error = ''
    except ChatFileError as exc:
        a = {'messages': [], 'moderators': set(), 'owners': set(), 'verified': set(), 'banned': set()}
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
        return JSON({'username': username, 'messages': shown,
                     'popup': next_popup,
                     'running': running,
                     'my_owner': identity_public in a['owners'],
                     'my_mod': identity_public in a['moderators'],
                     'my_verified': identity_public in a['verified'],
                     'my_banned': identity_public in a['banned']})




def send_message(body, reply_to=None):
    global last_message_time
    now = time.time()
    if identity_public in latest_analysis()['banned']:
        note('This account is banned from posting. Contact the Owner if this is a mistake.')
        return False
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
    global username, running, visible_count, clear_through, last_message_time, owner_access_key
    cleaned = str(raw)[:1800].strip()
    with state_lock:
        if not running or not cleaned:
            return snapshot()
        rename = re.fullmatch(r'/rename(?:\s+(.+))?', cleaned, flags=re.I)
        if rename:
            try:
                current = latest_analysis()
            except ChatFileError as exc:
                note('Cannot check verification status right now: ' + str(exc))
                return snapshot()
            if identity_public not in current['verified']:
                note('Only verified users can change their nickname. Ask a moderator to verify your ID.')
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
            if not write_event(event_now('profile')):
                username = previous_name
                note('Could not save your new name. Please try again.')
            return snapshot()
        command, _, arg = cleaned.partition(' ')
        command, arg = command.casefold(), arg.strip()

        try:
            if command == '/owner':
                if not arg:
                    note('Type /owner then enter your key in the password field (it will not be sent to chat).')
                    return snapshot()
                grant = call_owner_server('login', arg, target_key=identity_public)
                if not write_event(grant):
                    note('Owner identity granted, but failed to save to shared Google Drive. Retry login.')
                    return snapshot()
                owner_access_key = arg
                note('Access GRANTED. Owner badge activated for 8 hours. You can now use /addmod and /removemod.')
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
                note(f'Shared Drive file readable. Read: {(time.monotonic() - t) * 1000:.0f} ms.')
                return snapshot()
            if command == '/exit':
                running = False
                return snapshot()

            a = latest_analysis()
            me_owner = identity_public in a['owners']
            me_mod = identity_public in a['moderators'] or me_owner
            if command in ('/whoami', '/id'):
                roles = ((['Owner'] if me_owner else []) +
                         (['Moderator'] if me_mod and not me_owner else []) +
                         (['Verified'] if identity_public in a['verified'] else []))
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
                    ('[OWNER] ' if pub in a['owners'] else
                     '[MOD] ' if pub in a['moderators'] else '') +
                    ('[VERIFIED] ' if pub in a['verified'] else '') +
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
            if command == '/modhelp':
                if me_mod:
                    popup('MODERATOR COMMANDS', MOD_HELP)
                else:
                    note('Moderator permissions required.')
                return snapshot()
            if command == '/banlist':
                if not me_mod:
                    note('Moderator permission required.')
                else:
                    names = [f'@{a["display_names"].get(pub, fallback_username(pub))} | ID {short_id(pub)}'
                             for pub in sorted(a['banned'])]
                    note('BANNED IDENTITIES\n' + ('\n'.join(names) or '(none)'))
                return snapshot()
            if command in ('/verify', '/unverify', '/addmod', '/removemod', '/ban', '/unban'):
                is_owner_command = command in ('/addmod', '/removemod', '/ban', '/unban')
                if is_owner_command and not (me_owner and owner_access_key):
                    note('Owner login needed. Enter /owner and your private key first, '
                         'then try this command again.')
                    return snapshot()
                if not is_owner_command and not me_mod:
                    note('Moderator permission required. Owner: use /owner first.')
                    return snapshot()
                target, error = resolve_target(arg, a)
                if error:
                    note(error)
                    return snapshot()
                role = ('banned' if command in ('/ban', '/unban') else
                        'moderator' if command in ('/addmod', '/removemod') else 'verified')
                active = command in ('/verify', '/addmod', '/ban')
                target_name = a['display_names'].get(target, fallback_username(target))
                if command == '/ban' and (target == identity_public or target in a['owners']):
                    note('You cannot ban yourself or an active Owner.')
                    return snapshot()
                # An OWNER authorization is signed by Vercel; regular moderators
                # can sign verified grants themselves with persistent identities.
                if me_owner and owner_access_key:
                    grant = call_owner_server('role', owner_access_key,
                                              target_key=target, role=role, active=active)
                else:
                    grant = event_now('role', role=role, target_key=target, active=active)
                # Keep the signed grant in the issuer's PRIVATE Drive as
                # recovery data, not as an owner password. Background refresh
                # will restore the grant if a concurrent save removes it.
                queue_role_recovery(grant)
                # Google Drive mount updates are not atomic across students.
                # Another student's save can overwrite our role grant almost
                # immediately. Reinsert the SAME signed grant if it disappears,
                # and verify the effective role before declaring success.
                effective = False
                for attempt in range(6):
                    if not write_event(grant):
                        continue
                    updated = latest_analysis()
                    current_role_set = updated[{'moderator': 'moderators',
                                            'verified': 'verified', 'banned': 'banned'}[role]]
                    effective = (target in current_role_set) == active
                    if effective:
                        # Confirm once more after a short pause to catch an
                        # overlapping write before showing success.
                        time.sleep(0.3)
                        check = latest_analysis()
                        actual = check[{'moderator': 'moderators',
                                        'verified': 'verified', 'banned': 'banned'}[role]]
                        effective = (target in actual) == active
                    if effective:
                        break
                    current_entries = read_messages()
                    if any(isinstance(e, dict) and e.get('id') == grant['id']
                           for e in current_entries):
                        # The event is present but ineffective: don't claim a
                        # successful grant or repeatedly append a bad record.
                        break
                    time.sleep(min(0.4 * (attempt + 1), 1.5))
                if not effective:
                    note('Role grant could not be retained/verified in shared chat.json. '
                         'Concurrent Google Drive writes may be overwriting it. '
                         f'Target: {target_name} (ID {short_id(target)}). '
                         'Try again when fewer users are writing.')
                else:
                    note(f'{role.title()} {"assigned" if active else "revoked"} '
                         f'for {target_name} (ID {short_id(target)}). '
                         'The recipient must run the latest GitHub chat code '
                         'on THEIR own Google Drive identity.')
                return snapshot()
            if command == '/rolecheck':
                target, error = resolve_target(arg, a)
                if error:
                    note(error)
                    return snapshot()
                name = a['display_names'].get(target, fallback_username(target))
                labels = (['Owner'] if target in a['owners'] else []) + \
                         (['Moderator'] if target in a['moderators'] else []) + \
                         (['Verified'] if target in a['verified'] else []) + \
                      (['Banned'] if target in a['banned'] else [])
                note(f'{name} | ID {short_id(target)} | Roles: '
                     + (', '.join(labels) or 'Member')
                     + '\nNames are not identities: have this student compare /whoami ID.')
                return snapshot()
            if command == '/mentions':
                hits = [m for m in a['messages'] if is_mentioned(m['text'], username)][-10:]
                note('RECENT MENTIONS\n' + ('\n'.join(
                    f'#{m["number"]} {m["user"]}: {m["text"][:100]}' for m in hits) or 'No mentions.'))
                return snapshot()
            if command in ('/search', '/find'):
                if not arg:
                    note('Usage: /search <word or phrase>')
                else:
                    hits = [m for m in a['messages'] if arg.casefold() in m['text'].casefold()][-10:]
                    note('SEARCH RESULTS\n' + ('\n'.join(
                        f'#{m["number"]} [{m["time"]}] {m["user"]}: {m["text"][:90]}'
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
                body = '↳ #' + number + ' @' + target['user'] + ': ' + body.strip()
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
CHAT_HTML = r'''<div id="school-chat-SESSION_ID" style="font-family:monospace;font-size:13px;color:inherit;position:relative;">
  <div id="school-display-SESSION_ID" style="white-space:pre-wrap;overflow-wrap:anywhere;max-width:WRAPCHch;line-height:normal;margin:0;"></div>
  <form id="school-form-SESSION_ID" autocomplete="off" style="margin:0;padding:0;">
    <input id="school-input-SESSION_ID" type="text" aria-label="Chat message"
      style="display:block;box-sizing:border-box;width:min(INPUTPXpx,100%);height:19px;margin:0;padding:1px 2px;
             background:white;border:1px solid #999;border-radius:0;color:black;
             font-family:monospace;font-size:13px;outline:auto;outline-width:0;" />
  </form>
  <div id="school-closed-SESSION_ID" style="display:none;white-space:pre-wrap;">--- Chat closed ---</div>
  <div id="school-help-fallback-SESSION_ID" style="display:none;margin-top:4px;">
    <a id="school-help-link-SESSION_ID" href="#" target="_blank" rel="noopener" style="color:#00BFFF;text-decoration:underline;">Open help in new window</a>
    <span style="font-size:11px;color:gray;"> (Click if Chrome blocked the popup.)</span>
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
  const stateCallback = STATE_CALLBACK_JSON;
  const sendCallback = SEND_CALLBACK_JSON;
  let busy = false;
  let stopped = false;
  let generation = 0;
  let sending = false;
  let awaitingOwnerKey = false;
  let lastPopupId = null;
  let pendingHelpWindow = null;
  let lastHelpURL = null;

  function helpDocument(title, body) {
    // The text is displayed as textContent, never interpreted as HTML.
    return '<!doctype html><html><head><meta charset="utf-8"><title>School Chat Commands</title>' +
      '<style>body{background:#151515;color:#eaeaea;font:13px monospace;margin:24px;}' +
      'h2{color:#00ffff;font-size:17px;}pre{white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.7;}' +
      'button{background:#242424;color:#fff;border:1px solid #777;padding:6px 12px;cursor:pointer;}' +
      '</style></head><body><h2 id="heading"></h2><pre id="commands"></pre>' +
      '<button onclick="window.close()">Close</button></body></html>';
  }
  function openHelpWindow() {
    // Must run synchronously in the user submit event, before any await.
    try {
      const win = window.open('', '_blank', 'width=710,height=650,resizable=yes,scrollbars=yes');
      if (win) {
        pendingHelpWindow = win;
        win.document.open();
        win.document.write(helpDocument());
        win.document.close();
        win.document.getElementById('heading').textContent = 'Loading commands...';
        helpFallback.style.display = 'none';
        return;
      }
    } catch (error) { console.debug('Separate help window blocked:', error); }
    pendingHelpWindow = null;
  }
  function showHelpPopup(popup) {
    if (!popup) return;
    const title = String(popup.title || 'CHAT COMMANDS');
    const body = String(popup.text || '');
    if (pendingHelpWindow && !pendingHelpWindow.closed) {
      try {
        const doc = pendingHelpWindow.document;
        doc.getElementById('heading').textContent = title;
        doc.getElementById('commands').textContent = body;
        pendingHelpWindow.focus();
        pendingHelpWindow = null;
        helpFallback.style.display = 'none';
        return;
      } catch (error) { console.debug('Help window could not be updated:', error); }
    }
    // Sandbox/popup-blocker fallback: a REAL clickable link that opens a new tab.
    // No in-chat modal is created.
    const escapeHTML = text => text.replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
    const page = '<!doctype html><html><head><meta charset="utf-8"><title>School Chat Help</title>'+
      '<style>body{background:#151515;color:#eee;font:13px monospace;padding:22px}h2{color:cyan}pre{white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.7}</style></head>'+
      '<body><h2>'+escapeHTML(title)+'</h2><pre>'+escapeHTML(body)+'</pre></body></html>';
    if (lastHelpURL) URL.revokeObjectURL(lastHelpURL);
    lastHelpURL = URL.createObjectURL(new Blob([page], {type:'text/html'}));
    helpLink.href = lastHelpURL;
    helpFallback.style.display = 'block';
    pendingHelpWindow = null;
  }

  function colored(text, color, bold = false) {
    const span = document.createElement('span');
    span.style.color = color;
    if (bold) span.style.fontWeight = 'bold';
    span.textContent = String(text ?? '');
    return span;
  }
  const newline = () => area.appendChild(document.createTextNode('\n'));
  function replyData(reply) { return reply?.data?.['application/json'] ?? null; }

  function renderTextWithMentions(text, currentUsername) {
    const s = String(text ?? '');
    const escaped = currentUsername.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const regex = currentUsername ? new RegExp('(^|[^\\w@])(@' + escaped + ')(?!\\w)', 'gi') : null;
    if (!regex) { area.appendChild(document.createTextNode(s)); return; }
    let last = 0, m;
    while ((m = regex.exec(s)) !== null) {
      area.appendChild(document.createTextNode(s.slice(last, m.index) + m[1]));
      area.appendChild(colored(m[2], '#FFB900', true));
      last = m.index + m[0].length;
    }
    area.appendChild(document.createTextNode(s.slice(last)));
  }

  function appendRolePrefixes(isOwner, isMod, isVerified) {
    if (isOwner) area.appendChild(colored('[OWNER] ', '#00FFFF', true));
    else if (isMod) area.appendChild(colored('[MOD] ', '#00FFFF', true));
    if (isVerified) area.appendChild(colored('[VERIFIED] ', '#00FFFF', true));
  }

  function render(state) {
    if (!state || !root.isConnected) return;
    area.replaceChildren();
    area.appendChild(document.createTextNode('--- Connected as: '));
    appendRolePrefixes(state.my_owner, state.my_mod, state.my_verified);
    area.appendChild(colored(state.username, 'green', true));
    area.appendChild(document.createTextNode(' ---'));
    newline();
    area.appendChild(document.createTextNode(
      "--- Type your message and press Enter. Type '/exit' to quit. ---"));
    newline(); newline();

    for (const msg of Array.isArray(state.messages) ? state.messages : []) {
      area.appendChild(colored('[' + (msg.time || '') + ']', '#00FFFF'));
      if (!msg.private) area.appendChild(colored(' #' + msg.number, '#999999'));
      area.appendChild(document.createTextNode(' '));
      if (!msg.private) appendRolePrefixes(msg.owner, msg.moderator, msg.verified);
      area.appendChild(colored(msg.user || '', msg.private ? '#FF5555' : 'green', true));
      area.appendChild(document.createTextNode(': '));
      if (msg.private) area.appendChild(colored(msg.text || '', '#FF5555'));
      else renderTextWithMentions(msg.text || '', state.username || '');
      const reactions = msg.reactions || {};
      const active = Object.entries(reactions).filter(pair => pair[1] > 0);
      if (active.length) area.appendChild(colored('  [' + active.map(pair => pair[0] + ' ' + pair[1]).join('  ') + ']', '#bbbbbb'));
      newline();
    }
    if (state.popup && state.popup.id !== lastPopupId) {
      lastPopupId = state.popup.id;
      showHelpPopup(state.popup);
    }
    if (!state.running) {
      stopped = true;
      form.style.display = 'none';
      closed.style.display = 'block';
    }
  }

  async function refreshChat() {
    if (busy || sending || stopped || !root.isConnected) return;
    busy = true;
    try {
      const current = ++generation;
      const reply = await google.colab.kernel.invokeFunction(stateCallback, [], {});
      if (current === generation) render(replyData(reply));
    } catch (error) {
      console.debug('Chat refresh temporarily unavailable:', error);
    } finally {
      busy = false;
    }
  }

  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (stopped || sending) return;
    let value = input.value;
    if (!value.trim()) return;
    if (!awaitingOwnerKey && value.trim().toLowerCase() === '/owner') {
      awaitingOwnerKey = true;
      input.value = '';
      input.type = 'password';
      input.placeholder = 'Owner key (private)';
      input.focus();
      return;
    }
    const isHelp = !awaitingOwnerKey && /^\/(help|hellp|modhelp)$/i.test(value.trim());
    if (isHelp) openHelpWindow();
    const wasOwnerPassword = awaitingOwnerKey;
    if (awaitingOwnerKey) {
      value = '/owner ' + value;
      awaitingOwnerKey = false;
      input.type = 'text';
      input.placeholder = '';
    }
    sending = true;
    input.value = '';
    input.disabled = true;
    ++generation;
    try {
      const reply = await google.colab.kernel.invokeFunction(sendCallback, [value], {});
      render(replyData(reply));
    } catch (error) {
      // Never put an owner secret back into a visible text field.
      input.value = wasOwnerPassword ? '' : value;
      if (pendingHelpWindow && !pendingHelpWindow.closed) {
        pendingHelpWindow.document.getElementById('heading').textContent = 'Unable to load commands';
        pendingHelpWindow = null;
      }
      console.error('Could not send message:', error);
    } finally {
      sending = false;
      input.disabled = false;
      if (!stopped) input.focus();
    }
  });

  input.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && awaitingOwnerKey) {
      awaitingOwnerKey = false;
      input.value = '';
      input.type = 'text';
      input.placeholder = '';
    }
  });
  refreshChat();
  const timer = setInterval(() => {
    if (!root.isConnected || stopped) { clearInterval(timer); return; }
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
