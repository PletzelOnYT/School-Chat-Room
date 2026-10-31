# Messaging App v5.27

A Google Colab messaging app using **one shared Google Drive `chat.json`** file. The original terminal UI is preserved. No extra shared folder, Vercel, Redis, or external database is required.

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

New messages append in the same multi-line format, using an in-place modification of the JSON array's tail instead of rewriting its entire history.

### New title events

A user can claim the blue ☑ badge with the unlisted `/vf` command. This appends a *separate* simple record to the same JSON file:

```json
{
  "kind": "title",
  "time": "04:15:50",
  "user": "Pletzel",
  "title": "Verified"
}
```

The unlisted `/uvf Pletzel` appends a similar record with `"title": "None"`. Normal messages remain untouched and retain their original four keys. The most recent title record for a display name wins.

### Renaming and reactions

The hidden `/rename Pletzel` command now saves a compact rename event:

```json
{
  "kind": "rename",
  "time": "04:15:51",
  "user": "Guest-626",
  "name": "Pletzel"
}
```

The original `user` key is the person's stable local starting nickname. Renames persist across Colab restarts, and an existing blue ☑ Verified title follows the new nickname. Use `/vf` before renaming.

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
4. If you were using the separate Owner Guardian, stop the old monitor and restart the updated one using `--reset-baseline` **once**. The old guardian database contains removed role/reaction records and must not restore them.

## Supported features and trade-offs

- Green usernames, cyan timestamps, chronological threads, text search, anti-gibberish checks, and queued sends remain in the terminal-style UI.
- Replies store a compact parent marker inside their `text` field so each entry still has exactly four keys.
- **Verified checkmarks are self-claimed and tied to a display name, not a signed person.** `/vf` enables the blue badge, `/uvf <name>` revokes it, and `/verified` lists current badges. There is no Google identity attestation.
- `/rename <name>` persists through a simple record without altering old chat entries; reactions use name-based records and numeric message references. Both are functional but not cryptographically authenticated.
- Four-field messages cannot prove who authored them. Editors of the shared Drive file can impersonate names or change content.
- JSON tail append is **not an atomic multi-user transaction** on Google Drive. Simultaneous writers may still conflict.

## Owner Guardian

The separate [owner_guardian.py](owner_guardian.py) can back up and inspect new simple chat messages, but cannot verify unsigned authorship. See the [owner setup](OWNER_GUARDIAN_SETUP.md) for its one-time migration baseline reset and monitoring limitations.

**Backup of the original raw JSON:** https://drive.google.com/file/d/1h8bexHbQu2WkIVa_M3qfJVlF2qAZelmK/view
