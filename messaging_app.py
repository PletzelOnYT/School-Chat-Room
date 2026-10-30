# @title Messaging App v5.25 — Google Drive Messenger
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
from collections import Counter
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
COOLDOWN_SECONDS = 0.65
MESSAGE_LIMIT = 1000
CLIENT_VERSION = '5.25'
UPSTREAM_VERSION_CHECK_SECONDS = 25.0
GITHUB_CHAT_SOURCE_URL = (
    'https://raw.githubusercontent.com/PletzelOnYT/'
    'School-Chat-Room/main/messaging_app.py')
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
PRIVATE_OUTBOX_PATH = os.path.join(PRIVATE_FOLDER, 'message_outbox.json')


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
last_outbox_check = 0.0

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


PLAIN_MESSAGE_FIELDS = frozenset(('kind','time','user','text'))
REPLY_TEXT_RE = re.compile(r'^↳ \[reply:#(\d+)\] (.+)$', re.DOTALL)


def is_plain_message(event):
    """Four simple JSON fields, including converted historical messages."""
    return (isinstance(event, dict)
            and set(event) == PLAIN_MESSAGE_FIELDS
            and event.get('kind') == 'message'
            and isinstance(event.get('time'), str)
            and re.fullmatch(r'\d{2}:\d{2}:\d{2}', event['time']) is not None
            and isinstance(event.get('user'), str)
            and 0 < len(event['user']) <= 128
            and isinstance(event.get('text'), str)
            and len(event['text']) <= 5000)


def plain_message(text):
    return {'kind':'message',
            'time':time.strftime('%H:%M:%S',time.localtime()),
            'user':username,'text':text}


def display_only_message_id(event,position):
    """Local reaction/thread reference, not part of JSON message."""
    raw=json.dumps(event,sort_keys=True,ensure_ascii=False,
                   separators=(',',':')).encode('utf-8')
    return 'plain-%d-%s' % (position,hashlib.sha256(raw).hexdigest()[:16])


def write_plain_message(event):
    """Append only a four-field entry to the JSON array's existing tail."""
    if not is_plain_message(event):
        raise ChatFileError('Plain message must contain exactly four valid fields.')
    with file_lock:
        prior=sum(e==event for e in read_messages())
        for attempt in range(4):
            if attempt:
                time.sleep(0.12*attempt)
            try:
                append_event_in_place(event)
                time.sleep(0.15)
                if sum(e==event for e in read_messages())>prior:
                    return True
            except OSError as exc:
                if attempt==3:
                    raise ChatFileError('Drive append failed: '+str(exc)) from exc
            except ChatFileError:
                raise
        return False


def read_messages():
    """Read only the simplified, prettified Google Drive chat.json."""
    with file_lock:
        for attempt in range(4):
            try:
                with open(CHAT_FILE_PATH, encoding='utf-8') as fh:
                    events = json.load(fh)
                break
            except (OSError, ValueError) as exc:
                if attempt == 3:
                    raise ChatFileError('Cannot read chat.json: '+str(exc)) from exc
                time.sleep(0.1*(attempt+1))
        if not isinstance(events, list):
            raise ChatFileError('chat.json must be a JSON array.')
        if any(not is_plain_message(e) for e in events):
            raise ChatFileError('chat.json contains old or invalid records. '
                                'Run the owner four-field migration and restart clients.')
        return events


read_messages()  # Verify the correct shared history before showing the chat.


def append_event_in_place(event):
    """Append a fully indented object to the existing pretty JSON array.

    Only the final tail changes; concurrent Drive edits are still NOT atomic.
    """
    if not is_plain_message(event):
        raise ChatFileError('Only four-field message records are supported.')
    body = json.dumps(event, indent=2, ensure_ascii=False).replace('\n', '\n  ')
    rendered = ('  '+body).encode('utf-8')
    with open(CHAT_FILE_PATH, 'r+b') as fh:
        fh.seek(0, os.SEEK_END)
        size=fh.tell()
        scan=min(size,8192)
        fh.seek(size-scan)
        tail=fh.read(scan)
        close=len(tail.rstrip())-1
        if close < 0 or tail[close:close+1] != b']':
            raise ChatFileError('chat.json is not a closed JSON array.')
        before=tail[:close].rstrip()
        if not before or before[-1:] not in (b'}', b'['):
            raise ChatFileError('chat.json has an invalid closing element.')
        empty=before[-1:] == b'['
        trailer=tail[close+1:]
        fh.seek(size-scan+len(before))
        fh.write((b'' if empty else b',')+b'\n'+rendered+b'\n]'+trailer)
        fh.truncate()
        fh.flush()



def load_outbox():
    """Only this person's private Drive contains their recovery receipts."""
    try:
        with open(PRIVATE_OUTBOX_PATH, encoding='utf-8') as fh:
            records = json.load(fh)
        if isinstance(records, list):
            return [e for e in records if isinstance(e, dict) and
                    valid_signature(e) and e.get('sender_key') == identity_public][-50:]
    except (OSError, ValueError):
        pass
    return []


def save_outbox(events):
    try:
        os.makedirs(PRIVATE_FOLDER, exist_ok=True)
        with open(PRIVATE_OUTBOX_PATH, 'w', encoding='utf-8') as fh:
            json.dump(events[-50:], fh, ensure_ascii=False, separators=(',', ':'))
            fh.flush()
    except OSError as exc:
        raise ChatFileError('Cannot save your private retry outbox: ' + str(exc)) from exc


def remember_outgoing(event):
    with file_lock:
        events = load_outbox()
        if not any(e.get('id') == event.get('id') for e in events):
            events.append(event)
            save_outbox(events)


def write_event(event, remember=True):
    """Small tail append + verify, with durable retry receipt per sender.

    A sender's event ID is stable across retries. A corrupted or unreadable
    shared file is NEVER overwritten; ask its owner to restore a Drive version.
    """
    if is_plain_message(event):
        return write_plain_message(event)
    # Do not allow hidden role/reaction/profile metadata to pollute the
    # intentionally simplified public chat.json.
    return False
    if not valid_signature(event):
        raise ChatFileError('Cannot save unsigned internal metadata.')
    if remember:
        remember_outgoing(event)
    with file_lock:
        for attempt in range(7):
            existing = read_messages()
            if any(isinstance(e, dict) and e.get('id') == event.get('id')
                   for e in existing):
                return True
            # Spread concurrent clients' writes to reduce collisions.
            if attempt:
                time.sleep(0.07 * attempt + (int(uuid.uuid4().hex[:4], 16) % 80)/1000)
                existing = read_messages()
                if any(isinstance(e, dict) and e.get('id') == event.get('id')
                       for e in existing):
                    return True
            try:
                append_event_in_place(event)
            except OSError as exc:
                if attempt == 6:
                    raise ChatFileError('Google Drive tail append failed: ' + str(exc)) from exc
                continue
            time.sleep(0.16 + attempt * 0.06)
            try:
                if any(e.get('id') == event.get('id') for e in read_messages()
                       if isinstance(e, dict)):
                    return True
            except ChatFileError:
                # A concurrent, incomplete write may be syncing. Retry
                # parsing, but do not rewrite the damaged contents.
                if attempt == 6:
                    raise
        return False


def reconcile_outbox():
    """Restore one recently lost signed event per refresh interval.

    Never auto-rewrite damaged JSON. This reduces message loss after a
    completed Google Drive write is later overwritten by another client.
    """
    global last_outbox_check
    now = time.monotonic()
    if now - last_outbox_check < 7.0:
        return
    last_outbox_check = now
    records = load_outbox()
    if not records:
        return
    present = {e.get('id') for e in read_messages() if isinstance(e, dict)}
    for event in records:
        stamp = event.get('issued_at')
        if type(stamp) is not int or abs(time.time() - stamp) > 86400:
            continue
        if event.get('id') not in present:
            try:
                write_event(event, remember=False)
            except ChatFileError:
                pass
            break


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


def gibberish_reason(text):
    """Detect sustained keyboard mashing without punishing ordinary conversation.

    Rules are intentionally conservative on short text, slang, multilingual
    writing, natural repeated emphasis, links, code snippets, and emoji. Long
    repeated nonsense and alphabetic keysmashes use several independent
    signals rather than a brittle spelling dictionary.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    value = unicodedata.normalize('NFKC', text)
    raw = value.casefold()
    if re.search(r'(.)\1{11,}', raw, re.DOTALL):
        return 'Excessive repeated characters or emoji'
    if len(raw) >= 24 and re.search(r'(.{2,10}?)\1{5,}', raw, re.DOTALL):
        return 'Repeated character pattern'
    # Very long uninterrupted keyboard-row runs are almost never prose.
    if re.search(r'(?:qwertyuiop|asdfghjkl|zxcvbnm){2,}', raw):
        return 'Keyboard-row flood'
    # Ignore punctuation between repeats (asdasd-asdasd-asdasd...).
    compact = re.sub(r'[\s\W_]+', '', raw, flags=re.UNICODE)
    if len(compact) >= 30 and (
            re.search(r'([a-z]{2,9}?)\1{4,}', compact) or
            (len(compact) >= 40 and re.search(r'([a-z]{8,24}?)\1{2,}', compact))):
        return 'Repeated keyboard pattern'
    tokens = re.findall(r"[\w']+", raw, flags=re.UNICODE)
    if len(tokens) >= 9:
        counts = Counter(tokens)
        common, repeats = counts.most_common(1)[0]
        if repeats >= 8 and repeats / len(tokens) >= 0.75 and len(common) <= 24:
            return 'Repeated word flood'

    # Do not treat non-Latin writing as English keyboard noise.
    ascii_tokens = re.findall(r'[a-z]+', raw)
    if not ascii_tokens:
        return None
    # For ordinary phrases, analyze only long continuous alphabetic runs.
    # URLs, pasted code and file paths have different text statistics.
    links_or_code = bool(re.search(
        r'https?://|www\.|(?:[a-z]\w*\.){2,}|[{}\[\]<>]|[/\\]|[_=]', raw))
    long_tokens = [word for word in ascii_tokens if len(word) >= 16]
    # Include letter/digit keysmashes such as q3jhfgg..., but ignore tokens
    # that are predominantly numeric IDs or code-looking expressions.
    for candidate in re.findall(r'(?<![a-z0-9])[a-z0-9]{22,}(?![a-z0-9])', raw):
        if candidate.isalpha() or candidate.isdigit():
            continue
        letters = re.sub(r'[^a-z]', '', candidate)
        if len(letters) >= 18 and len(letters) / len(candidate) >= 0.72:
            long_tokens.append(letters)
    vowels = set('aeiouy')
    common_pairs = {
        'th','he','in','er','an','re','on','at','en','nd','ti','es','or','te',
        'of','ed','is','it','al','ar','st','to','nt','ng','se','ha','as','ou',
        'io','le','ve','co','me','de','hi','ri','ro','ic','ne','ea','ra','ce',
        'li','ch','ll','be','ma','si','om','ur','ca','el','ta','la','ns','di',
        'fo','ho','pe','ec','pr','no','ct','us','ac','ot','il','tr','ly','nc',
        'et','ut','ss','so','rs','un','lo','wa','ge','ee','wi','em','ad','ol',
        'rt','po','we','na','ul','ie','ow','id','od','ba','ke','gh','wh','sh',
        'ck','oo','qu','bu','fi','sp','su','pl','cl','cr','bl','br','gr','fr',
        'dr','fl','sw','sk','sn','sm','tw','gl','sl','sc'
    }
    if not links_or_code:
        for word in long_tokens:
            if len(word) < 18:
                continue
            vratio = sum(c in vowels for c in word) / len(word)
            cmax = max((len(x) for x in re.findall(r'[bcdfghjklmnpqrstvwxz]+',
                                                     word)), default=0)
            pairs = [word[i:i+2] for i in range(len(word)-1)]
            unlikely = sum(pair not in common_pairs for pair in pairs) / len(pairs)
            # Strong signals must AGREE. Normal long technical words can be
            # consonant-rich or contain rare pairs, but rarely both.
            if cmax >= 8 and (vratio < 0.30 or unlikely > 0.57):
                return 'Unlikely long keyboard-mash word'
            if len(word) >= 24 and unlikely > 0.65 and vratio < 0.37:
                return 'Unlikely random letter sequence'
            if len(word) >= 24 and vratio < 0.12 and cmax >= 6:
                return 'Very long consonant-only sequence'

        # Multiple shorter mashed words are common in chat spam.
        if len(ascii_tokens) >= 5 and len(raw) >= 25:
            alpha = [w for w in ascii_tokens if len(w) >= 3]
            if len(alpha) >= 5:
                no_vowels = sum(not any(c in vowels for c in w) for w in alpha)
                unlikely_short = sum(
                    len(w) >= 4 and
                    (sum(w[i:i+2] not in common_pairs for i in range(len(w)-1))
                     / (len(w)-1) > 0.75)
                    for w in alpha)
                if (no_vowels / len(alpha) >= 0.80 and
                        unlikely_short / len(alpha) >= 0.55):
                    return 'Repeated unlikely keyboard-mash words'

    # A flood of repeated punctuation/symbol runs; allow expressive emoji.
    punctuation = re.sub(r'[\w\s]', '', raw, flags=re.UNICODE)
    if (len(raw) >= 35 and len(punctuation) >= 28 and
            len(set(punctuation)) <= 3 and
            len(punctuation) / len(raw) > 0.80):
        return 'Repeated symbol flood'
    return None


def is_gibberish(text):
    """Compatibility predicate for all send-side and read-side filters."""
    return gibberish_reason(text) is not None


def analyze(events):
    """Read signed chat identities, messages and self-claimed Verified titles.

    All legacy privileged role records are ignored.
    """
    verified = set()
    # Old auto-grants had no separate marker from /vf. They cannot establish
    # a badge in v5.23; people must explicitly claim with the new format.
    legacy_claimed = set()
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
        plain = is_plain_message(e)
        event_id = (display_only_message_id(e,index) if plain else
                    str(e.get('id','legacy-index-'+str(index))))
        if event_id in seen_ids:
            continue
        seen_ids.add(event_id)
        kind = str(e.get('kind','message'))
        signed = valid_signature(e)
        if signed:
            seen_any_signed_event = True
        sender = str(e.get('sender_key', '')) if signed else None
        # A nickname cannot confer a Verified title.
        clean_name = (e.get('user') if plain else valid_username(e.get('user')))
        account_name = valid_username(e.get('account_username'))
        if signed and sender and kind in ('message', 'reaction', 'profile'):
            identities.setdefault(sender, fallback_username(sender))
            if account_name:
                account_names[sender] = account_name
                seen_aliases.setdefault(sender, set()).add(account_name)
                claim_order.setdefault((sender, account_name.casefold()), index)
            # Previously saved nicknames remain displayed after migration,
            # but only a currently verified user may issue NEW rename events.
            can_rename = sender in verified or (
                kind == 'profile' and e.get('renamed') is True and
                sender in legacy_claimed and
                type(e.get('issued_at')) is int and
                e.get('issued_at') < 1793404800
            )
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
            # One title only: blue checkmark. New claims require the explicit
            # v2 marker, so historical Drive-mount auto-grants are ignored.
            # Any signed chat participant may unverify any target identity.
            if not signed or e.get('role') != 'verified':
                continue
            target = e.get('target_key')
            try:
                valid_target = isinstance(target, str) and len(b64decode(target)) == 32
            except (TypeError, ValueError):
                valid_target = False
            if not valid_target:
                continue
            if (e.get('active') is True and target == sender and
                    e.get('self_verified') is True):
                if e.get('verification_version') == 2:
                    verified.add(sender)
                else:
                    legacy_claimed.add(sender)
                identities.setdefault(sender, fallback_username(sender))
            elif (e.get('active') is False and
                    e.get('unverify_anyone') is True and
                    e.get('verification_version') == 2):
                verified.discard(target)
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
        if not signed and not plain and seen_any_signed_event:
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
                if (sum(1 for ts, _ in history if 0 <= stamp - ts <= 10) >= 12 or
                        sum(1 for ts, _ in history if 0 <= stamp - ts <= 60) >= 65 or
                        sum(1 for ts, body in history if 0 <= stamp - ts <= 30 and
                            body == message_text.casefold().strip()) >= 4):
                    continue
                history.append((stamp, message_text.casefold().strip()))
        post_number += 1
        display_text = str(message_text)
        parent_id = e.get('reply_to') if signed else None
        if plain:
            marker = REPLY_TEXT_RE.fullmatch(display_text)
            if marker:
                parent = by_number.get(int(marker.group(1)))
                if parent and parent['number'] < post_number:
                    parent_id = parent['id']
                    display_text = marker.group(2)
        item = {
            'number': post_number, 'id': event_id,
            'user': clean_name or fallback_username(sender),
            'text': display_text,
            'time': str(e.get('time', '')),
            'sender_key': sender,
            'signed': signed,
            'plain': plain,
            'reply_to': parent_id,
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
            if original.casefold() in name_owners and not msg.get('plain'):
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
/search <filters>          Find by name, date, reactions or replies
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

Search: from:Alex after:2026-10-01 before:2026-11-01 has:reaction in:replies
Verified users may use /rename <name> to claim a unique nickname.
The blue checkmark is self-claimed, not an independently verified identity.'''


RULES = '''MESSAGING APP RULES
1. No bullying, harassment, threats, or hateful content.
2. Do not post passwords, private addresses, personal contact details or records.
3. No impersonation. Reserved names are rejected; only signed self-claims get checkmarks.
4. Avoid spam and flooding.
5. Report abuse to someone you trust. Editors can change or erase the
   shared chat.json file, so this is not a fully secure messaging service.'''


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
            headers={'User-Agent': 'MessagingApp-VersionCheck',
                     'Cache-Control': 'no-cache', 'Range': 'bytes=0-255'})
        with urllib.request.urlopen(request, timeout=5) as response:
            heading = response.read(256).decode('utf-8', errors='replace')
        match = re.search(r'^# @title Messaging App v(\d+\.\d+)', heading)
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


 # No automatic profile record: chat.json contains messages ONLY.


def resolve_verified_target(query, state):
    """Match a unique display name, @name, or 12-character chat identity ID.

    Never resolve from untrusted text messages or partial nickname substrings.
    """
    if not isinstance(query, str):
        return None
    target = query.strip()
    if target.startswith('@'):
        target = target[1:]
    if not target:
        return None
    folded = target.casefold()
    matches = [
        key for key, display in state['display_names'].items()
        if display.casefold() == folded or short_id(key).casefold() == folded
    ]
    return matches[0] if len(matches) == 1 else None


def claim_self_verified():
    """Set one blue checkmark for the calling signed identity, never anyone else."""
    current = latest_analysis()
    if identity_public in current['verified']:
        return True
    event = event_now('role', role='verified', target_key=identity_public,
                      active=True, self_verified=True, verification_version=2)
    return bool(write_event(event) and
                identity_public in latest_analysis()['verified'])


# Mounting Drive is necessary to load chat history, but it is NOT evidence
# of successful independent Google identity verification. No automatic badge.


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
    if filters.get('has', '').casefold() not in ('', 'reaction'):
        raise ChatFileError('Valid has: filter: reaction.')
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
    """Group the latest N posts beneath their original parent messages.

    Include older ancestors for context, without dumping 40 stale replies
    into the display. A reply to a reply remains nested beneath that reply.
    """
    visible = [m for m in messages if m['number'] > cleared and
               m['user'].casefold() not in muted]
    selected = visible[-count:]
    by_id = {m['id']: m for m in messages}
    groups = {}
    for msg in selected:
        chain = []
        current = msg
        seen = set()
        while current and current['id'] not in seen:
            seen.add(current['id'])
            chain.append(current)
            parent_id = current.get('reply_to')
            current = by_id.get(parent_id) if parent_id else None
        if not chain:
            continue
        root = chain[-1]
        group = groups.setdefault(root['id'], {
            'root': root, 'replies': {}, 'newest': 0,
        })
        group['newest'] = max(group['newest'], msg['number'])
        for part in chain[:-1]:
            group['replies'][part['id']] = part

    result = []
    for group in sorted(groups.values(), key=lambda g: g['newest']):
        replies = sorted(group['replies'].values(), key=lambda m: m['number'])
        result.append({
            'root': group['root'], 'replies': replies,
            'total_replies': len(replies),
        })
    return result


def snapshot():
    global next_popup, running, update_notice_shown
    try:
        check_upstream_version()
        # Retry an event after a competing Drive save removed it. No server.
        reconcile_outbox()
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
    spam_reason = gibberish_reason(body)
    if spam_reason:
        note('Message blocked: ' + spam_reason + '.')
        return False
    # Only block a *fifth* duplicate in 15 seconds. A couple of repeated
    # replies such as "hi" are normal and should not be misclassified.
    recent_sent_messages[:] = [(t, value) for t, value in recent_sent_messages
                               if now - t <= 15]
    normalized_body = ' '.join(body.split()).casefold()
    if sum(1 for _, value in recent_sent_messages if value == normalized_body) >= 4:
        note('Repeated-message flood blocked. You can send a different message.')
        return False
    # Refresh the current chat before sending a simple JSON record.
    latest_analysis()
    # Replies encode the target in text, so even reply records have 4 fields.
    if reply_to:
        target = next((m for m in latest_analysis()['messages']
                       if m['id'] == reply_to), None)
        if target is None:
            note('Cannot reply: original message no longer exists.')
            return False
        body = '↳ [reply:#%d] %s' % (target['number'],body)
    evt = plain_message(body)
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
                # Explicit local self-claim. Drive mounting alone is never proof.
                if not claim_self_verified():
                    note('Could not save the checkmark to shared chat.json. Retry.')
                return snapshot()
            if command == '/uvf':
                # Intentionally unlisted in all help/command menus. Any
                # authenticated chat identity can revoke anyone's checkmark.
                # The target can later use /vf to claim it again.
                current = latest_analysis()
                target_key = resolve_verified_target(arg, current)
                if target_key is None:
                    note('User not found or ambiguous. Use the exact @name '
                         'or a 12-character identity ID from /users.')
                    return snapshot()
                target_name = current['display_names'].get(target_key, arg)
                if target_key not in current['verified']:
                    note(target_name + ' does not currently have a checkmark.')
                    return snapshot()
                evt = event_now('role', role='verified',
                                target_key=target_key, active=False,
                                unverify_anyone=True, verification_version=2)
                if write_event(evt):
                    result = latest_analysis()
                    if target_key not in result['verified']:
                        note('Blue checkmark removed from ' + target_name + '.')
                    else:
                        note('Revocation saved, but Drive has not yet '
                             'confirmed the new badge state.')
                else:
                    note('Could not save the checkmark revocation. Retry.')
                return snapshot()
            if command in ('/help', '/hellp'):
                popup('MESSAGING APP COMMANDS', HELP_TEXT)
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
                         '/search has:reaction  |  /search in:replies "quiz"')
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
STATE_CALLBACK = f'messaging_app.{SESSION}.state'
SEND_CALLBACK = f'messaging_app.{SESSION}.send'
output.register_callback(STATE_CALLBACK, snapshot)
output.register_callback(SEND_CALLBACK, submit_message)

# One permanent chat display + one short input. Neither is cleared on updates.
CHAT_HTML = r'''<div id="messaging-app-SESSION_ID" style="font-family:monospace;font-size:13px;color:inherit;position:relative;">
  <div id="messaging-display-SESSION_ID" style="white-space:pre-wrap;overflow-wrap:anywhere;max-width:WRAPCHch;line-height:normal;margin:0;"></div>
  <form id="messaging-form-SESSION_ID" autocomplete="off" style="margin:0;padding:0;">
    <input id="messaging-input-SESSION_ID" type="text" aria-label="Chat message"
      style="display:block;box-sizing:border-box;width:min(INPUTPXpx,100%);height:19px;margin:0;padding:1px 2px;
             background:white;border:1px solid #999;border-radius:0;color:black;
             font-family:monospace;font-size:13px;outline:auto;outline-width:0;" />
  </form>
  <div id="messaging-closed-SESSION_ID" style="display:none;white-space:pre-wrap;">--- Chat closed ---</div>
  <div id="messaging-help-fallback-SESSION_ID" style="display:none;margin-top:4px;">
    <a id="messaging-help-link-SESSION_ID" href="#" target="_blank" rel="noopener" style="color:#00BFFF;text-decoration:underline;">Open help in new window</a>
    <span style="font-size:11px;color:gray;"> (Click if Chrome blocked the popup.)</span>
  </div>
</div>
<script>
(() => {
  const root = document.getElementById('messaging-app-SESSION_ID');
  const area = document.getElementById('messaging-display-SESSION_ID');
  const form = document.getElementById('messaging-form-SESSION_ID');
  const input = document.getElementById('messaging-input-SESSION_ID');
  const closed = document.getElementById('messaging-closed-SESSION_ID');
  const helpFallback = document.getElementById('messaging-help-fallback-SESSION_ID');
  const helpLink = document.getElementById('messaging-help-link-SESSION_ID');
  const stateCallback = STATE_CALLBACK_JSON;
  const sendCallback = SEND_CALLBACK_JSON;
  let busy = false;
  let stopped = false;
  let generation = 0;
  let sending = false;
  const sendQueue = [];
  let lastQueuedDispatch = 0;
  let lastPopupId = null;
  let pendingHelpWindow = null;
  let lastHelpURL = null;

  function helpDocument(title, body) {
    // The text is displayed as textContent, never interpreted as HTML.
    return '<!doctype html><html><head><meta charset="utf-8"><title>Messaging App Commands</title>' +
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
    const page = '<!doctype html><html><head><meta charset="utf-8"><title>Messaging App Help</title>'+
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

  function renderTextWithMentions(text, currentUsername, target=area) {
    const s = String(text ?? '');
    const escaped = currentUsername.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const regex = currentUsername ? new RegExp('(^|[^\\w@])(@' + escaped + ')(?!\\w)', 'gi') : null;
    if (!regex) { target.appendChild(document.createTextNode(s)); return; }
    let last = 0, m;
    while ((m = regex.exec(s)) !== null) {
      target.appendChild(document.createTextNode(s.slice(last, m.index) + m[1]));
      target.appendChild(colored(m[2], '#FFB900', true));
      last = m.index + m[0].length;
    }
    target.appendChild(document.createTextNode(s.slice(last)));
  }

  function appendVerifiedTitle(isVerified, target=area) {
    if (isVerified) target.appendChild(colored('☑ ', '#3498FF', true));
  }

  // All extra controls are tiny inline text, not panels or large buttons.
  const expandedThreads = new Set();
  const activePickers = new Set();
  let latestState = null;
  let selectedMessageId = null;
  let showSelectedThread = false;
  let selectedEmojiPicker = false;
  const emojiChoices = ['👍','❤️','😂','🔥','🎉','💀','👀'];
  function textControl(text, action, color = '#999999') {
    const span = colored(text, color);
    span.setAttribute('role','button');
    span.tabIndex = 0;
    span.style.cursor = 'pointer';
    span.title = text;
    span.addEventListener('click', action);
    span.addEventListener('keydown', e => {
      if (e.key === 'Enter' || e.key === ' ') {e.preventDefault();action();}
    });
    return span;
  }
  async function drainSendQueue() {
    if (sending || stopped) return;
    sending = true;
    try {
      while (sendQueue.length && !stopped) {
        // The backend's legitimate-user cooldown is 0.65 seconds.
        const wait = Math.max(0, 750 - (Date.now() - lastQueuedDispatch));
        if (wait) await new Promise(resolve => setTimeout(resolve, wait));
        const command = sendQueue[0];
        lastQueuedDispatch = Date.now();
        try {
          const current = ++generation;
          const reply = await google.colab.kernel.invokeFunction(sendCallback, [command], {});
          if (current === generation) render(replyData(reply));
          sendQueue.shift();
        } catch(error) {
          console.error('Message could not be submitted:', error);
          // Preserve the next unsent text in the input if possible.
          if (!input.value.trim() && !command.startsWith('/')) input.value = command;
          sendQueue.shift();
        }
      }
    } finally {
      sending = false;
      if (!stopped) input.focus();
    }
  }
  function sendInline(command) {
    if (stopped) return;
    sendQueue.push(command);
    drainSendQueue();
  }
  function threadFor(messageId) {
    return (latestState?.threads || []).find(group =>
      group.root?.id === messageId ||
      (group.replies || []).some(reply => reply.id === messageId)
    ) || null;
  }

  function renderLine(msg, currentUsername, nested=false, interactive=true) {
    // The normal view prints exactly one row per message. Action controls
    // only appear when that message is selected; none are appended per row.
    const line = document.createElement('span');
    if (nested) line.appendChild(document.createTextNode(
      '  '.repeat(Math.min(4, Number(nested) || 1)) + '↳ '));
    line.appendChild(colored('[' + (msg.time || '') + ']', '#00FFFF'));
    if (!msg.private) line.appendChild(colored(' #' + msg.number, '#999999'));
    line.appendChild(document.createTextNode(' '));
    if (!msg.private) appendVerifiedTitle(msg.verified, line);
    line.appendChild(colored(msg.user || '', msg.private ? '#FF5555' : 'green', true));
    line.appendChild(document.createTextNode(': '));
    if (msg.private) {
      line.appendChild(colored(msg.text || '', '#FF5555'));
    } else {
      renderTextWithMentions(msg.text || '', currentUsername, line);
      const active = Object.entries(msg.reactions || {}).filter(pair => pair[1] > 0);
      if (active.length) {
        line.appendChild(colored('  [' +
          active.map(pair => pair[0] + ' ' + pair[1]).join('  ') + ']', '#bbbbbb'));
      }
    }
    if (!msg.private && interactive) {
      line.style.cursor = 'pointer';
      line.title = 'Click for reactions, reply and thread';
      line.addEventListener('click', () => {
        selectedMessageId = selectedMessageId === msg.id ? null : msg.id;
        selectedEmojiPicker = false;
        showSelectedThread = false;
        render(latestState);
      });
    }
    area.appendChild(line);
    newline();

    if (!msg.private && interactive && selectedMessageId === msg.id) {
      const group = threadFor(msg.id);
      area.appendChild(document.createTextNode('   '));
      area.appendChild(textControl('[React]', () => {
        selectedEmojiPicker = !selectedEmojiPicker;
        render(latestState);
      }, '#3498FF'));
      area.appendChild(document.createTextNode('  '));
      area.appendChild(textControl('[Reply]', () => {
        input.value = '/reply ' + msg.number + ' ';
        input.focus();
        input.setSelectionRange(input.value.length, input.value.length);
      }, '#3498FF'));
      area.appendChild(document.createTextNode('  '));
      area.appendChild(textControl('[Close]', () => {
        selectedMessageId = null; selectedEmojiPicker = false;
        showSelectedThread = false; render(latestState);
      }));
      newline();
      if (selectedEmojiPicker) {
        area.appendChild(document.createTextNode('   '));
        for (const emoji of emojiChoices) {
          const mine = (msg.my_reactions || []).includes(emoji);
          area.appendChild(textControl(emoji + ' ', () => {
            selectedEmojiPicker = false;
            sendInline((mine ? '/unreact ' : '/react ') + msg.number + ' ' + emoji);
          }, mine ? '#3498FF' : '#999999'));
        }
        newline();
      }
    }
  }

  function render(state) {
    if (!state || !root.isConnected) return;
    latestState = state;
    if (selectedMessageId && !(state.messages || []).some(m => m.id === selectedMessageId)) {
      selectedMessageId = null;
      selectedEmojiPicker = false;
      showSelectedThread = false;
    }
    area.replaceChildren();
    const header = document.createElement('span');
    header.appendChild(document.createTextNode('--- Connected as: '));
    appendVerifiedTitle(state.my_verified, header);
    header.appendChild(colored(state.username, 'green', true));
    header.appendChild(document.createTextNode(' ---'));
    // Fullscreen is still available without adding any icons to the classic UI.
    header.title = 'Double-click to toggle fullscreen';
    header.addEventListener('dblclick', () => {
      try {
        if (document.fullscreenElement) document.exitFullscreen();
        else if (root.requestFullscreen) root.requestFullscreen();
      } catch(error) { console.debug('Fullscreen unavailable:', error); }
    });
    area.appendChild(header);
    newline();
    area.appendChild(document.createTextNode(
      "--- Type your message and press Enter. Type '/exit' to quit. ---"));
    newline(); newline();

    // Threaded view: replies are ALWAYS immediately below their parent,
    // including nested replies. No "View thread" action is necessary.
    const groups = Array.isArray(state.threads) ? state.threads : [];
    if (groups.length) {
      const printed = new Set();
      for (const group of groups) {
        if (!group?.root?.id) continue;
        const children = new Map();
        for (const reply of group.replies || []) {
          const parent = reply.reply_to || group.root.id;
          if (!children.has(parent)) children.set(parent, []);
          children.get(parent).push(reply);
        }
        for (const childList of children.values()) {
          childList.sort((x,y) => (x.number||0)-(y.number||0));
        }
        function renderDescendant(node, depth) {
          if (!node?.id || printed.has(node.id)) return;
          printed.add(node.id);
          renderLine(node, state.username || '', depth);
          for (const child of children.get(node.id) || []) {
            renderDescendant(child, Math.min(depth+1,4));
          }
        }
        renderDescendant(group.root, 0);
      }
    } else {
      for (const msg of (state.messages || []).filter(m => !m.private)) {
        renderLine(msg, state.username || '');
      }
    }
    // Private system notices remain local and are never written to Drive.
    for (const msg of (state.messages || []).filter(m => m.private)) {
      renderLine(msg, state.username || '');
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

  form.addEventListener('submit', event => {
    event.preventDefault();
    if (stopped) return;
    const value = input.value;
    if (!value.trim()) return;
    if (['/help', '/hellp'].includes(value.trim().toLowerCase())) openHelpWindow();
    input.value = '';
    // Do not disable the text input just because another message is pending.
    // This client's messages are queued and sent in submission order.
    sendInline(value);
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
