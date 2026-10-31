# Messaging App v5.36

## Repository structure

The main implementations are grouped by purpose. The old root-level
`*.py` URLs are lightweight **compatibility entry points**, so existing
Google Colab notebooks and shared links still work without updating them.

```text
School-Chat-Room/
├── README.md                       Main guide and quick start
├── app/
│   └── messaging_app.py            Complete chat application
├── guardian/
│   ├── owner_guardian.py           Owner-only monitor and recovery engine
│   └── guardian_launcher.py        Current Guardian Colab launcher
├── tools/
│   └── guardian_stress_lab.py      Isolated security stress tests + rollback
├── docs/
│   └── OWNER_GUARDIAN_SETUP.md     Owner Guardian setup instructions
├── tests/                          Automated Python/browser regression tests
├── .github/workflows/             Continuous integration
├── school_chat.py                  Public Colab chat launcher (legacy URL)
├── messaging_app.py                Compatibility loader for app/
├── guardian_launcher.py           Compatibility loader for guardian/
├── owner_guardian.py              Compatibility loader for guardian/
├── guardian_stress_lab.py          Compatibility loader for tools/
└── OWNER_GUARDIAN_SETUP.md         Documentation redirect for old links
```

**Important:** Run one owner Guardian in its separate Colab notebook. Do not
run the Guardian or test attacks in every participant's chat session.
Neither reorganization nor compatibility loading changes the original
shared Drive file, the private Guardian database, or any account permissions.


A Google Colab messaging app using **one shared Google Drive `chat.json`** file. The original terminal UI is preserved. No extra shared folder, Vercel, Redis, or external database is required.


## Stress Lab Colab loader fix (Guardian version)

If the stress panel previously crashed with
`RuntimeError: Guardian v2.7 or later is required for this lab`, an outdated
local `owner_guardian.py` could be selected instead of the latest one.
The lab now chooses a current Guardian, ignores stray local copies in Colab,
and falls back to GitHub's Contents API if its raw file download is stale.
It prints the actual Guardian version and origin before opening the panel.
Rerun the original cache-busted one-cell lab launcher below; restarting or
resetting the real Guardian database is **not** required. Only isolated
temporary test copies are written.

## Live-attached mod menu (safe mode)

The Stress Lab now provides a **MOD MENU — attach to actual chat** checkbox.
When enabled in the owner's Colab, it reads the currently mounted original
`MyDrive/chat.json`, takes a private snapshot in the notebook's temporary
directory, and shows the **real current messages** alongside a modded local
view. The interface includes a selection of visual hacks (rainbow nickname,
mock owner/verified badges, name replacement, local fake reaction, mock
timestamp, ghost message, message highlighting, hiding, and order changes).
**Apply to live view** modifies only what this notebook displays.
**Refresh real chat** rereads recent actual messages without changing them.

The red **REVERT ALL TEST CHANGES** button clears these local hacks,
re-reads the current original file, and deletes the isolated stress-test
attack copies. This protects new messages from other users; **it does not
replace or rewind the original shared chat**. The snapshot is a private
rollback reference for the local overlay. Disconnecting the live view
destroys its temporary snapshot. No persistent room copy is uploaded.

Why live destructive attacks aren't in this mode: full-file rollback on a
shared Anyone-Editor Google Drive file would remove messages appended by
other users after the backup, and may conflict with the owner's Guardian
approval ledger. A reliable destructive live stress test requires an
exclusive testing room/maintenance window and a transactional server-side
authorization/restore scheme. Continue using the full isolated attack
suite for malicious role/identity, spam, history corruption and flooding.

## Interactive stress lab: chat preview and 59 attack scenarios

The owner-only `tools/guardian_stress_lab.py` panel now shows a three-column
**read-only chat preview** for each simulation:

- **BEFORE:** The unmodified sandbox history.
- **ATTACK:** What the modified client actually wrote into its temporary copy,
  including spoofed titles/names, chat text, malformed records, and colors.
- **AFTER:** The cloned chat after the real Guardian's `process()` scan.

The preview is intentionally *terminal-like* instead of running the full
student chat client: it escapes all user-supplied HTML, CSS, and script tags,
so injection tests cannot execute in the notebook preview. A category filter
groups **59 scenarios** across identity and badges, formatting and colors,
spam and rate limits, clock and reactions, and file integrity. Run one case,
a category, or all cases, then choose any completed case in **Inspect** to
compare the snapshots. The red **REVERT ALL TEST CHANGES** control still
restores and deletes the isolated test files; it never changes live Drive.

Outcomes distinguish **BLOCKED** (automatically restored), **DETECTED ONLY**
(flagged but not restored), **SECURITY GAP** (unauthorized/untrusted accepted
or otherwise unprotected), **ALLOWED TEXT** (ordinary text is legal; not
evidence of script execution), and **PASS** (valid-message positive control).
For example, the chat's unsigned title/rename/reaction records are still
not strong identity proofs; the lab reports the limitation rather than
claiming prevention.

To run the current panel in Colab, use the same supported public root URL
(compatibility launcher) with a cache-busting query:

```python
import time, urllib.request
url = "https://raw.githubusercontent.com/PletzelOnYT/School-Chat-Room/main/guardian_stress_lab.py"
exec(compile(
    urllib.request.urlopen(url + "?v=" + str(time.time_ns()))
        .read().decode("utf-8"), url, "exec"
))
```

Select **Use real chat as a read-only reference** only if the owner wants
the preview to show copied recent chat contents; the test never injects into
that original. A local simulation tests Guardian's parser and recovery
logic, **not** true cross-user Google Drive synchronization, server-side
permissions, or resistance to copying the public client source.

## Owner-only modified-client attack stress lab (reversible)

`guardian_stress_lab.py` is a **separate red-team test panel**. It imports
the production `owner_guardian.py` and simulates a modified client that
writes forbidden JSON records **only to private, temporary test copies** of
`chat.json`. It never uploads attack payloads, writes to your real shared
Drive file, changes sharing permissions, or resets the live Guardian.

To open the test panel inside **the owner's Colab notebook**, run:

```python
import time, urllib.request
url = "https://raw.githubusercontent.com/PletzelOnYT/School-Chat-Room/main/guardian_stress_lab.py"
exec(compile(urllib.request.urlopen(url + "?v=" + str(time.time_ns())).read().decode("utf-8"), url, "exec"))
```

The panel provides a choice of attacks, **Run full stress test**, and a large
red **REVERT ALL TEST CHANGES** button. The red button restores the bytes of
every test copy to its initial snapshot and then deletes temporary scenarios
and their test-only incident logs. Even if a simulated attack successfully
bypasses the Guardian, the **original shared chat never changes**.

It exercises direct unauthorized title and nickname claims, owner-role
spooﬁng, impersonation, ANSI/HTML/CSS/bidirectional Unicode color tricks,
invalid timestamps and sidecars, extra fields, invalid reactions, anti-spam
limits, historical edits, deletion, reordering and JSON corruption. The
positive control is one legitimate four-field message. A separate checkbox
can **read** the actual mounted `MyDrive/chat.json` once to seed a local
sandbox; it never writes to that file. No one needs to run the attack suite
in every student's Colab.

**Important security limitation:** The chat's current four-field message,
title and rename events have no cryptographic user identity, and the
original Drive file permits Editors to modify its contents. A watchdog that
checks every few seconds cannot prevent all unauthorized writes or prove
who made one, and cannot stop someone copying code from a public GitHub
repository. The panel reports these cases as **SECURITY GAP**, not as
successes. Reliable prevention requires a server-side identity and
authorization boundary or restricted Drive write permissions, not just
obfuscated client commands.

Run local automated tests with `python tools/guardian_stress_lab.py --cli`.
For inspection without writing any local attack payloads to Drive, leave the
live-file checkbox off (the default).

## v5.36 / Guardian v2.7: Live timestamp ordering and monitoring

**Confirmed in the original shared chat file:** two messages with absolute
UTC metadata are stored in reversed append order: 02:57:37 was appended before
02:57:35. The file also contains older messages 02:09:28 and 02:09:25 in
reversed order without absolute metadata. **These are not necessarily damaged
timestamps; concurrent Google Drive writers can reach the file in a different
order than they sent messages.**

Chat v5.36 automatically corrects nearby (up to 120 seconds) time inversions
**in its display**, including precisely dated messages and plausible short
legacy inversions. It adjusts old date inference to avoid treating a 2- or
3-second reversal as an entire day. Reply threads retain their parent/child
relationships, and all `#number` identifiers remain unchanged. It does not
physically reorder or overwrite `chat.json`—that could corrupt numeric
reply targets and reaction references. Longer gaps and missing historical
dates cannot be reliably corrected without external evidence.

Guardian v2.7 continues to monitor the original file approximately every
five seconds while its **separate owner Colab runtime is connected**. It
prints a `LIVE MONITOR` heartbeat every 30 seconds with scan status, shared
file byte count, event count, and its private approved checkpoint count.
The private historical timestamp audit also flags nearby inversions and their
confidence level. Guardian repairs **actual tampering** from its approved
history but does not rewrite valid timestamps or reorder old messages.
A healthy Guardian does not mean that display ordering was already corrected
in old client sessions.

**To activate both sides:** Stop and restart the owner-only Guardian launcher
(to load v2.7) and restart **every participating chat client** (to load
v5.36). Old client cells will continue to render old ordering. Run
`/version` in the chat and look for `v5.36`. Stop old Guardian cells
before restarting; never start two repair processes against the same room.
Do not reset the Guardian baseline or replace the original file.

## Owner Guardian launcher: prevent GitHub raw cache staleness

The Guardian launcher now adds a unique cache-busting query when it fetches
`owner_guardian.py`, prints the fetched version, and refuses to execute
outdated Guardian versions below v2.6. To ensure **the launcher itself** is
fresh too, use this owner-only Colab cell:

```python
import time, urllib.request
source = "https://raw.githubusercontent.com/PletzelOnYT/School-Chat-Room/main/guardian_launcher.py"
url = source + "?cache_bust=" + str(time.time_ns())
exec(compile(urllib.request.urlopen(url).read().decode("utf-8"), source, "exec"))
```

This resolves cases where the notebook prints an old version such as v2.5
even though GitHub has v2.6. It does **not** establish that the owner's
actual Google Workspace account grants Drive REST API permissions; testing
that requires the owner's live Colab authorization.

## Guardian v2.6: Correct handling of Colab user credentials

In some Colab runtimes, Google's **ephemeral user authorization**
(`USE_AUTH_EPHEM=1`) is represented by the same Python credential class as
Compute Engine credentials. The prior Guardian rejected that class after
successful user consent. v2.6 accepts this specific Colab configuration
**only when the credential can refresh successfully**, and continues to
reject ordinary unauthenticated VM and service-account credentials.

In standard Colab user authorization, an authorized credential file can be
stored as `/content/.adc/adc.json`. If the default lookup still returns a
VM identity, Guardian checks that Colab-managed user file and validates its
OAuth credentials instead. Credentials and tokens are never printed or
placed in the chat log, GitHub, or shared Drive. A school policy may still
block authorization, in which case add a shortcut to the original file
manually using the same Google account; no policy is bypassed.

## Guardian v2.5: Fix recurring Colab VM credential RefreshError

Some Colab sessions keep exposing Compute Engine ADC even when Drive is
mounted. Guardian now runs Google's standard `auth.authenticate_user()`
user-consent check **before** initializing any Drive API session. If the
owner already granted consent, Colab reuses it; otherwise the owner must
approve Google's prompt. The launcher prints the fetched Guardian version
*before* the OAuth step, making stale notebook sessions easier to identify.

If Google's school/Workspace policy does not allow Drive API access, Guardian
cannot grant that permission automatically. Open the original shared
`chat.json` with the same account and add a shortcut to the root of
`My Drive`, then restart the owner-only Guardian. **Never upload a copy,
reset the guardian baseline, or share private backup files.**

## Owner Guardian v2.4: Google Drive REST API authorization

Google Drive mounting and Drive API OAuth are separate. If Colab's default
credentials point to its VM metadata service and refresh fails with HTTP 404,
Guardian now asks for the owner's Google user authorization using Colab's
supported interactive sign-in. Existing valid user credentials are reused
without prompting. This fixes the `RefreshError` on automatic shortcut setup.
Google Workspace/education restrictions are respected: authorization can be
denied and the original shortcut may then need to be added manually. It never
creates a chat copy or modifies permissions.

## Owner Guardian v2.3: Colab kernel argument fix

Jupyter and Google Colab automatically add a kernel connection option such as
`-f /root/.local/share/jupyter/runtime/kernel-....json`. The previous
owner-only launcher ran the Guardian as `__main__`, causing `argparse`
to reject this unrelated option. The launcher now starts the Guardian
using explicit default arguments, and direct calls of `guardian.main()`
from notebook kernels also ignore the injected connection option.
Normal command-line and explicitly provided Guardian flags remain supported.

## Owner Guardian v2.2: Automatic Drive shortcut and one-cell start

The **owner-only** Guardian now also mounts Drive in Colab and creates/reuses
the same `My Drive/chat.json` shortcut to the **original shared file** if
needed. No one has to manually paste the original Drive file ID into the
Guardian configuration. Its approved-history ledger, incident archives and
clock audit remain private inside the owner's
`My Drive/.messaging_guardian/` folder; it does not copy the chat to a new
shared file or alter link-sharing permissions.

To launch from one Colab cell **in the owner's account only**:

```python
import urllib.request
url = "https://raw.githubusercontent.com/PletzelOnYT/School-Chat-Room/main/guardian_launcher.py"
exec(compile(urllib.request.urlopen(url).read().decode("utf-8"), url, "exec"))
```

The launcher obtains the current `owner_guardian.py` from GitHub and starts
monitoring. The owner must keep their Colab runtime running; no cloud monitor
continues after disconnection. Do not run multiple Guardians on different
students' accounts: they could conflict while repairing the same chat.

If account restrictions prevent automatic Drive shortcut creation, use the
manual *Add shortcut* action with the original shared file; the Guardian
never bypasses school-account permissions. Existing approved Guardian
backups are preserved—**do not reset the baseline** for this upgrade.
See [Owner Guardian setup](docs/OWNER_GUARDIAN_SETUP.md).

## v5.35: Automatically join the original shared Drive chat

When a student opens the existing GitHub Colab launcher, the app mounts their Google Drive as usual. If `My Drive/chat.json` does **not** already exist, the client tries to use the **currently authorized** Google Drive API session to create a Google Drive shortcut named `chat.json` in that account's My Drive root, pointing to the single original file `1CysiJWMvyW6AOYXtkAnf99JSIhAQHrMd`.

- **No data copy and no room split:** A shortcut references the original file; the app never uploads the attached snapshot or creates another chat log.
- **No sharing changes:** The owner already shared the file via its link. Automatic shortcut creation does not grant others permissions or change the existing permissions.
- **Idempotent and cautious:** Existing mounted `chat.json` is reused; an existing matching shortcut is not recreated. A different item named `chat.json` blocks setup rather than being overwritten.
- **Permission-aware:** The launcher uses existing Google credentials; it never forces an additional Google sign-in, bypasses a school-account restriction, or obtains new OAuth scopes without approval. If Google refuses the API call, users must open the shared link in their own account and use *Organize → Add shortcut* manually, placing it at the root of My Drive.
- **Drive mount synchronization:** A newly created shortcut may not immediately appear in Colab's mounted filesystem. If the launcher asks, remount Drive and rerun the launcher. Keep the same original shortcut, not a downloaded copy.

**Important security trade-off:** The original file currently allows anyone with its link to edit its contents, including existing chat history. Neither a shortcut nor Owner Guardian can make that fully safe. A safer chat requires authenticated per-message server writes and read-only history access for ordinary participants.

## v5.34: Clean timestamp display

Message timestamps appear as plain `[HH:MM:SS]` without a `~` marker. New messages with valid absolute timestamps show accurate local time. Older records containing only `HH:MM:SS` still use inferred dates internally, so their displayed times cannot be guaranteed exact. The Guardian continues to mark timestamp confidence in its private audit file. This release changes no stored records, numbering, order, reactions, or chat styling.

## v5.33: Hardened Owner Guardian v2.1 and historical timestamp recovery

The Colab client automatically reconstructs **estimated** local display times for older date-less messages by walking the archived chat order backwards from newer absolute UTC timestamps. It accounts for midnight rollovers. Inferred timestamps display in the same plain clock format as verified timestamps; their uncertainty remains recorded in Guardian's private audit. **This never changes or sorts stored messages**: message numbers, reply targets and reaction references stay intact. Unknown gaps, older client timezones, and missing dates cannot be recovered with certainty.

Owner Guardian v2.1 adds a persistent rolling spam-rate window (including across process restarts), a room-wide flood limiter to reduce nickname-rotation attacks, rejection of suspicious name-format control characters, stronger clock/message correspondence checking, and tamper recovery from its approved private database. Owner Guardian continuously writes `.messaging_guardian/historical_clock_audit.json` in the owner's private Drive: each message is marked `clock_metadata`/`stored_epoch` (absolute), `inferred_utc_not_verified` (approximate), or `unknown`. The Guardian **does not** rewrite historic timestamps without evidence or rearrange the shared file; it preserves a forensic record of what can and cannot be inferred.

These controls are **best-effort, not identity verification or DDoS protection**. Shared `chat.json` remains unsigned for plain messages. People with write access can spoof display names or deliberately send new records; rate limits can also affect legitimate bursts. For stronger security, a single authenticated server and atomic per-message storage are needed.

To activate: stop the prior Owner Guardian and Colab chat cells, restart the **current** `owner_guardian.py` first, then rerun your existing GitHub chat launcher. No `chat.json` deletion or baseline reset is required. Type `/version` in chat to verify **v5.34**. The Guardian prints **v2.1** on startup.

## v5.32: Clock validation, browser-local time, and /version

Run `/version` in the Colab chat to see the current script version. The launcher still fetches `messaging_app.py` from the GitHub `main` branch on restart.

New messages keep **exactly** the four message fields `kind`, `time`, `user`, `text`. The client writes each message time in UTC and appends a separate `kind: "clock"` metadata record with an absolute Unix timestamp and SHA-256 digest of the message. This lets viewers display the correct local time, even across dates and daylight-saving transitions, without breaking the original message schema or rewriting the shared history.

Owner Guardian validates the UTC clock against the absolute timestamp, checks that the digest points to an existing message, quarantines malformed records, and restores a previously approved clock record if tampered with. This is **integrity monitoring, not trusted timestamp attestation**: shared-file editors can still forge unsigned new records. Older messages that contain only `HH:MM:SS` cannot be perfectly repaired; they have no date/timezone evidence, and the Guardian intentionally does not guess and rewrite them.

**Restart both the chat Colab cell and the separate Owner Guardian process** to load the new versions. If an old Guardian process remains running, it can reject the new `clock` metadata. Do not reset the Guardian baseline or delete `chat.json`.

## v5.31: Local chat timestamps

The displayed clock is now converted from the Google Colab runtime's UTC time into each viewer's browser-local time, without changing the terminal-style UI. New records still contain the same **four message fields** (`kind`, `time`, `user`, `text`) and are written with UTC `HH:MM:SS`; the existing shared Drive history remains untouched. Old time-only records cannot recover their original date or historical daylight-saving offset; they are displayed using a recent UTC-day assumption. Signed historical records with `issued_at` use their absolute timestamp when available.

## Live file migration completed

On October 30, 2026, the original shared Google Drive file ID
`1CysiJWMvyW6AOYXtkAnf99JSIhAQHrMd` was converted to **196 prettified message records**. All old messages were kept; 34 reaction, profile and role records were deliberately excluded. A complete 230-record backup was saved separately in Drive.

Every **normal message** still uses exactly four fields, in this order:

```json
{
  "kind": "message",
  "time": "04:15:50",
  "user": "Pletzel",
  "text": "fdg"
}
```

**v5.30 changes storage to an append-only journal.** The original formatted JSON array is left byte-for-byte unchanged; each new message, reaction, rename, or title is appended as a framed JSON object *after* that array. No older bytes are rewritten or truncated.

This means the complete `chat.json` file **is no longer one valid JSON document** after the first new message. Only the updated Messaging App and updated Owner Guardian know how to read the original array plus appended records. Raw `json.load()` or an ordinary JSON validator will reject the whole file; the individual records are still JSON objects. The app uses a record-separator marker before each new object.

### New title events

A blue ☑ badge is represented by a separate simple title record in the same JSON file:

```json
{
  "kind": "title",
  "time": "04:15:50",
  "user": "Pletzel",
  "title": "Verified"
}
```

A title revocation appends a similar record with `"title": "None"`. Normal messages remain untouched and retain their original four keys. The most recent title record for a display name wins.

### Renaming and reactions

Nickname changes save a compact rename event:

```json
{
  "kind": "rename",
  "time": "04:15:51",
  "user": "Guest-626",
  "name": "Pletzel"
}
```

The original `user` key is the person's stable local starting nickname. Renames persist across Colab restarts, and an existing blue ☑ Verified title follows the new nickname.

Emoji reactions also work with small metadata records:

```json
{
  "kind": "reaction",
  "time": "04:15:52",
  "user": "Pletzel",
  "target": 183,
  "emoji": "❤️",
  "active": true
}
```

`/unreact` writes `"active": false`. Message records **still have only four fields**; all metadata uses separate entries.

## Starting the app

1. Stop all older Colab chat cells; older versions can write incompatible records.
2. The launcher automatically attempts to add a shortcut to the original shared `chat.json` in each participant's My Drive root. Existing correct shortcuts continue working; if the school account restricts the Drive API, use the link and add the original shortcut manually.
3. Rerun the existing GitHub launcher (old `school_chat.py` path still loads `messaging_app.py`).
4. **Stop any older Owner Guardian before anyone starts v5.32.** An older Guardian will treat the appended records as broken JSON and could overwrite them. Download the latest `owner_guardian.py` and restart it. **Do not reset an existing healthy Guardian database** solely for this upgrade; a reset would discard the prior verified backup history. Run a temporary `--audit-only` check first.

## Supported features and trade-offs

- Green usernames, cyan timestamps, chronological threads, text search, anti-gibberish checks, and queued sends remain in the terminal-style UI.
- Replies store a compact parent marker inside their `text` field so each entry still has exactly four keys.
- **Verified checkmarks are self-claimed and tied to a display name, not a signed person.** A separate title record enables or revokes the blue badge. There is no Google identity attestation.
- `/rename <name>` persists through a simple record without altering old chat entries; reactions use name-based records and numeric message references. Both are functional but not cryptographically authenticated.
- Four-field messages cannot prove who authored them. Editors of the shared Drive file can impersonate names or change content.
- The app opens the existing shared file in **append-only mode** and never rewrites older chat bytes during normal sends.
- A private, per-user `.schoolchat/message_outbox.json` holds delivery receipts. If a competing Drive save erases a recently appended event, an active or restarted client can reappend it.
- **Google Drive Colab mounts still do not guarantee cross-user atomic append.** Private recovery receipts reduce loss, but delivery and ordering are not guaranteed when many students send simultaneously. A single-writer server or shared per-message files would be necessary for stronger guarantees.

## Owner Guardian

The separate [owner_guardian.py](guardian/owner_guardian.py) can back up and inspect new simple chat messages, but cannot verify unsigned authorship. See the [owner setup](docs/OWNER_GUARDIAN_SETUP.md) for its one-time migration baseline reset and monitoring limitations.

**Backup of the original raw JSON:** https://drive.google.com/file/d/1h8bexHbQu2WkIVa_M3qfJVlF2qAZelmK/view
