# @title School ChatRoom v5.3 — School terminal chat with Vercel owner login
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
REFRESH_INTERVAL_MS = 1500
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

try:
    import getpass
    username = getpass.getuser()
    if not username or username == 'root':
        username = 'ColabUser'
except Exception:
    username = 'User'

file_lock = threading.RLock()
state_lock = threading.RLock()
started_at = time.monotonic()
last_message_time = 0.0
running = True
owner_access_key = None  # Only in memory; never written to chat.json or private Drive.
local_messages = []  # Private, inline System messages, NEVER sent to Drive.
next_popup = None  # One-shot private /help or /modhelp popup
visible_count = VISIBLE_MESSAGES
muted_names = set()
clear_through = 0


class ChatFileError(Exception):
    pass


def b64(raw):
    return base64.b64encode(raw).decode('ascii')


def b64decode(value):
    return base64.b64decode(value, validate=True)


def load_identity():
    """A locally generated, persistent private key, stored in each student's OWN Drive."""
    os.makedirs(PRIVATE_FOLDER, exist_ok=True)
    if os.path.exists(PRIVATE_IDENTITY_PATH):
        with open(PRIVATE_IDENTITY_PATH, encoding='utf-8') as f:
            obj = json.load(f)
        key = Ed25519PrivateKey.from_private_bytes(b64decode(obj['private_key']))
    else:
        key = Ed25519PrivateKey.generate()
        raw = key.private_bytes(serialization.Encoding.Raw,
                                serialization.PrivateFormat.Raw,
                                serialization.NoEncryption())
        pub = key.public_key().public_bytes(serialization.Encoding.Raw,
                                             serialization.PublicFormat.Raw)
        # Exclusive-create only; don't accidentally overwrite a private identity.
        with open(PRIVATE_IDENTITY_PATH, 'x', encoding='utf-8') as f:
            json.dump({'type': 'school-chat-identity-v1',
                       'private_key': b64(raw), 'public_key': b64(pub)}, f, indent=2)
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
    if clean.casefold() in RESERVED_USERNAMES:
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


def fallback_username(sender):
    """Cannot impersonate a known name using malformed/untrusted metadata."""
    return 'Guest-' + short_id(sender)[:8] if sender else 'LegacyUser'


username = valid_username(username) or 'ColabUser'


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
    """Never auto-replace an unreadable shared chat with a blank history."""
    with file_lock:
        try:
            with open(CHAT_FILE_PATH, encoding='utf-8') as f:
                events = json.load(f)
        except (OSError, ValueError) as exc:
            raise ChatFileError('Cannot read shared chat.json: ' + str(exc)) from exc
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
                           time=time.strftime('%H:%M:%S', time.localtime()),
                           user=username, **fields))


def is_gibberish(text):
    """Preserve original anti-spam filtering for regular chat."""
    if not text or not text.strip():
        return False
    text_len = len(text)
    clean = re.sub(r'[^a-zA-Z]', '', text).lower()
    length = len(clean)
    if text_len >= 6 and (text_len - length) / text_len > 0.45:
        return True
    if text_len < 5:
        return False
    if re.search(r'(.)\1{3,}', text):
        return True
    if length >= 8 and len(set(clean)) / length < 0.35:
        return True
    if length >= 5:
        vowels = sum(ch in 'aeiouy' for ch in clean)
        if not vowels or vowels / length < 0.15 or vowels / length > 0.85:
            return True
        if re.search(r'[^aeiouy]{4,}', clean):
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
    identities = {}
    messages = []
    by_number = {}
    seen_ids = set()
    post_number = 0
    reaction_state = {}
    first_seen = {}
    name_claims = {}
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
        sender = str(e.get('sender_key', '')) if signed else None
        from_server = bool(SERVER_PUBLIC_KEY) and sender == SERVER_PUBLIC_KEY and signed
        # Identity aliases come only from valid, SIGNED user events, not from
        # arbitrary JSON or a forged role grant.
        clean_name = valid_username(e.get('user'))
        if signed and not from_server and clean_name and kind in ('message', 'reaction'):
            identities[sender] = clean_name
            if sender not in first_seen:
                first_seen[sender] = index
            name_claims.setdefault(clean_name.casefold(), set()).add(sender)

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

    # Protect registered names on the receiving side, even when someone
    # modifies their personal notebook to bypass the /rename checks.
    # A signed OWNER always wins their current nickname; then moderator,
    # verified, and finally earliest signed claimant. This is a client-side
    # display defense, not an authoritative name registry (Drive is editable).
    name_owners = {}
    for lower, claimants in name_claims.items():
        if len(claimants) < 2:
            continue
        if lower in GENERIC_USERNAMES and not any(
                pub in owners or pub in moderators or pub in verified
                for pub in claimants):
            continue
        name_owners[lower] = min(
            claimants,
            key=lambda pub: (
                0 if pub in owners else 1 if pub in moderators else
                2 if pub in verified else 3,
                first_seen.get(pub, 10**18), pub
            )
        )

    # In addition reserve every privileged user's current name, even if an
    # imitator has not posted that name until later.
    for pub, name in identities.items():
        if pub not in owners and pub not in moderators and pub not in verified:
            continue
        lower = name.casefold()
        previous = name_owners.get(lower)
        if previous is None or (pub in owners and previous not in owners) or (
                pub in moderators and previous not in owners and previous not in moderators):
            name_owners[lower] = pub

    for msg in messages:
        owner_of_name = name_owners.get(msg['user'].casefold())
        if owner_of_name is not None and owner_of_name != msg['sender_key']:
            msg['user'] = fallback_username(msg['sender_key'])
            msg['name_conflict'] = True
        else:
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
                identities=identities, owners=owners,
                moderators=moderators, verified=verified)


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


if SERVER_KEY_ERROR:
    note('Owner tags unavailable: ' + SERVER_KEY_ERROR)


def popup(title, content):
    """Queue one modal for this browser only; never broadcast."""
    global next_popup
    next_popup = {'title': title, 'text': content, 'id': uuid.uuid4().hex}



HELP_TEXT = '''CHAT COMMANDS (only shown to you)
/help or /hellp            Open the command popup
/rules                     Chat rules
/whoami                    Your name, identity ID and roles
/users                     Students seen posting signed messages
/verified                  List verified identities
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
/owner                     Owner login (asks for private key)
/exit                      Close the chat

@mention a username in a normal message to highlight it for that person.
Numbers (#) appear next to chat messages. Moderator role changes are saved
into the SAME shared chat.json.'''

MOD_HELP = '''MODERATOR COMMANDS (signed changes, visible to everyone)
/verify <name or ID>        Assign Verified badge to a signed identity
/unverify <name or ID>      Revoke Verified badge
/addmod <name or ID>        Owner only: assign Moderator role
/removemod <name or ID>     Owner only: revoke Moderator role
/owner                     Privately authenticate as Owner

For ambiguous nicknames, use the unique ID from /users. Roles are tied
to a private identity key, never to a changeable display name.'''

RULES = '''SCHOOL CHAT RULES
1. No bullying, harassment, threats, or hateful content.
2. Do not post passwords, private addresses, personal contact details or records.
3. No impersonation. Reserved role-tag names are rejected; only signed roles get badges.
4. Avoid spam and flooding.
5. Report serious issues to school staff. The shared JSON can be edited or
   erased by anybody who has Editor access; moderation isn't tamper-proof.'''


def latest_analysis():
    return analyze(read_messages())


def resolve_target(arg, a):
    """Find a signed identity by stable 12-digit ID or unique current nickname."""
    needle = arg.strip().lstrip('@').casefold()
    if not needle:
        return None, 'Provide a username or ID. Use /users to find it.'
    ids = [(pub, name) for pub, name in a['identities'].items() if short_id(pub) == needle]
    if not ids:
        ids = [(pub, name) for pub, name in a['identities'].items()
               if name.casefold() == needle]
    if not ids:
        return None, 'No signed identity found. Ask them to send a new message, then use /users.'
    if len(ids) > 1:
        return None, 'Several students use that name. Choose their unique ID from /users.'
    return ids[0][0], None


def is_mentioned(text, name):
    if not name or not isinstance(text, str):
        return False
    return bool(re.search(r'(?<![\w@])@' + re.escape(name) + r'(?!\w)', text, flags=re.I))


def snapshot():
    global next_popup
    try:
        a = latest_analysis()
        error = ''
    except ChatFileError as exc:
        a = {'messages': [], 'moderators': set(), 'owners': set(), 'verified': set()}
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
                     'my_verified': identity_public in a['verified']})




def send_message(body, reply_to=None):
    global last_message_time
    if time.time() - last_message_time < COOLDOWN_SECONDS:
        note('[System]: Slow down! You are typing too fast.')
        return False
    if len(body) > MESSAGE_LIMIT:
        note(f'[System]: Maximum message length is {MESSAGE_LIMIT} characters.')
        return False
    if is_gibberish(body):
        note('[System]: Message blocked. Gibberish/Spam detected.')
        return False
    evt = event_now('message', text=body)
    if reply_to:
        evt['reply_to'] = reply_to
        evt = sign_event({k: v for k, v in evt.items() if k != 'sig'})
    ok = write_event(evt)
    last_message_time = time.time()
    if not ok:
        note('[System]: Delivery could not be confirmed. Check history before retrying.')
    return ok


def submit_message(raw):
    global username, running, visible_count, clear_through, last_message_time, owner_access_key
    cleaned = str(raw)[:1800].strip()
    with state_lock:
        if not running or not cleaned:
            return snapshot()
        rename = re.fullmatch(r'/rename(?:\s+(.+))?', cleaned, flags=re.I)
        if rename:
            new_name = valid_username(rename.group(1))
            if not new_name:
                note('Nickname rejected: use 1–32 letters/numbers, spaces, _ - .; role tags, reserved names and invisible characters are not allowed.')
                return snapshot()
            try:
                current = latest_analysis()
            except ChatFileError as exc:
                note('Cannot check nickname right now: ' + str(exc))
                return snapshot()
            other_names = {
                pub: name for pub, name in current['identities'].items()
                if pub != identity_public and name.casefold() == new_name.casefold()
            }
            if other_names and new_name.casefold() not in GENERIC_USERNAMES:
                note('Nickname already claimed by another signed identity. Choose another name.')
                return snapshot()
            username = new_name
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
            if command == '/whoami':
                roles = ((['Owner'] if me_owner else []) +
                         (['Moderator'] if me_mod and not me_owner else []) +
                         (['Verified'] if identity_public in a['verified'] else []))
                note('Name: ' + username + '\nYour identity ID: ' + short_id(identity_public) +
                     '\nRoles: ' + (', '.join(roles) or 'Member') +
                     '\nYour private key is in your own Google Drive.')
                return snapshot()
            if command in ('/users', '/verified'):
                people = sorted(a['identities'].items(), key=lambda pair: pair[1].casefold())
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
            if command in ('/verify', '/unverify', '/addmod', '/removemod'):
                is_owner_command = command in ('/addmod', '/removemod')
                if (is_owner_command and not (me_owner and owner_access_key)) or (not is_owner_command and not me_mod):
                    note('Insufficient permissions. Owner: type /owner to sign in first.')
                    return snapshot()
                target, error = resolve_target(arg, a)
                if error:
                    note(error)
                    return snapshot()
                role = 'moderator' if is_owner_command else 'verified'
                active = command in ('/verify', '/addmod')
                # Server grants all owner actions, so public Colab code cannot
                # simply forge a moderator by returning status=granted.
                if me_owner and owner_access_key:
                    grant = call_owner_server('role', owner_access_key,
                                               target_key=target, role=role, active=active)
                else:
                    grant = event_now('role', role=role, target_key=target, active=active)
                ok = write_event(grant)
                note(f'{role.title()} {"assigned" if active else "revoked"} for ID {short_id(target)}.'
                     if ok else 'Role change could not be confirmed.')
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
  <div id="school-help-overlay-SESSION_ID" style="display:none;position:absolute;top:0;left:0;z-index:20;margin:0;width:min(620px,96%);box-sizing:border-box;background:#151515;color:#eaeaea;border:1px solid #18a058;padding:12px;box-shadow:0 4px 12px #0005;">
    <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;">
      <strong id="school-help-heading-SESSION_ID" style="font-size:13px;color:#00FFFF;">COMMANDS</strong>
      <button type="button" id="school-help-close-SESSION_ID" aria-label="Close help" style="font:inherit;font-weight:bold;color:white;background:transparent;border:1px solid #666;padding:1px 8px;cursor:pointer;">X</button>
    </div>
    <pre id="school-help-text-SESSION_ID" style="font:inherit;line-height:1.5;max-height:320px;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere;margin:8px 0 0;"></pre>
  </div>
</div>
<script>
(() => {
  const root = document.getElementById('school-chat-SESSION_ID');
  const area = document.getElementById('school-display-SESSION_ID');
  const form = document.getElementById('school-form-SESSION_ID');
  const input = document.getElementById('school-input-SESSION_ID');
  const closed = document.getElementById('school-closed-SESSION_ID');
  const help = document.getElementById('school-help-overlay-SESSION_ID');
  const helpHeading = document.getElementById('school-help-heading-SESSION_ID');
  const helpText = document.getElementById('school-help-text-SESSION_ID');
  const helpClose = document.getElementById('school-help-close-SESSION_ID');
  const stateCallback = STATE_CALLBACK_JSON;
  const sendCallback = SEND_CALLBACK_JSON;
  let busy = false;
  let stopped = false;
  let generation = 0;
  let sending = false;
  let awaitingOwnerKey = false;
  let lastPopupId = null;

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
      helpHeading.textContent = state.popup.title || 'COMMANDS';
      helpText.textContent = state.popup.text || '';
      help.style.display = 'block';
      helpClose.focus();
      try { google.colab.output.setIframeHeight(Math.max(450, document.documentElement.scrollHeight), true); } catch (_) {}
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
      console.error('Could not send message:', error);
    } finally {
      sending = false;
      input.disabled = false;
      if (!stopped) input.focus();
    }
  });

  helpClose.addEventListener('click', () => { help.style.display = 'none'; input.focus(); });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && help.style.display !== 'none') {
      help.style.display = 'none';
      input.focus();
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
