# Messaging App v5.33

A Google Colab messaging app using **one shared Google Drive `chat.json`** file. The original terminal UI is preserved. No extra shared folder, Vercel, Redis, or external database is required.


## v5.33: Hardened Owner Guardian v2.1 and historical timestamp recovery

The Colab client automatically reconstructs **estimated** local display times for older date-less messages by walking the archived chat order backwards from newer absolute UTC timestamps. It accounts for midnight rollovers. An inferred time displays with a leading `~` (approximate), whereas signed or clock-metadata-backed timestamps have no marker. **This never changes or sorts stored messages**: message numbers, reply targets and reaction references stay intact. Unknown gaps, older client timezones, and missing dates cannot be recovered with certainty.

Owner Guardian v2.1 adds a persistent rolling spam-rate window (including across process restarts), a room-wide flood limiter to reduce nickname-rotation attacks, rejection of suspicious name-format control characters, stronger clock/message correspondence checking, and tamper recovery from its approved private database. Owner Guardian continuously writes `.messaging_guardian/historical_clock_audit.json` in the owner's private Drive: each message is marked `clock_metadata`/`stored_epoch` (absolute), `inferred_utc_not_verified` (approximate), or `unknown`. The Guardian **does not** rewrite historic timestamps without evidence or rearrange the shared file; it preserves a forensic record of what can and cannot be inferred.

These controls are **best-effort, not identity verification or DDoS protection**. Shared `chat.json` remains unsigned for plain messages. People with write access can spoof display names or deliberately send new records; rate limits can also affect legitimate bursts. For stronger security, a single authenticated server and atomic per-message storage are needed.

To activate: stop the prior Owner Guardian and Colab chat cells, restart the **current** `owner_guardian.py` first, then rerun your existing GitHub chat launcher. No `chat.json` deletion or baseline reset is required. Type `/version` in chat to verify **v5.33**. The Guardian prints **v2.1** on startup.

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
2. Keep the original `chat.json` shortcut in the root of each participant's My Drive.
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

The separate [owner_guardian.py](owner_guardian.py) can back up and inspect new simple chat messages, but cannot verify unsigned authorship. See the [owner setup](OWNER_GUARDIAN_SETUP.md) for its one-time migration baseline reset and monitoring limitations.

**Backup of the original raw JSON:** https://drive.google.com/file/d/1h8bexHbQu2WkIVa_M3qfJVlF2qAZelmK/view
