# Messaging App

A lightweight Google Colab messaging app. Your existing shared Google Drive **`chat.json`** is the only shared storage required. No extra event folder, image attachment folder, Vercel, Redis, or other chat hosting services.

## Features

- Fast terminal-inspired interface: green names, cyan timestamps, one blue ☑ self-claimed Verified badge
- Clickable emoji reactions and removable reactions
- Replies shown directly beneath their parent message, including nested replies
- Advanced search by name, date, reactions and replies
- Mobile-friendly input, scrollable history, and expanded/full-screen controls
- Signed private identity persisted across restarts in the original `.schoolchat/identity.json` (path retained to **preserve existing IDs**)
- Google account username prefix used when already available without extra school-restricted login; otherwise a short unique Guest label
- `/rename` only when Verified; private `/vf` self-check command, not advertised in help
- **No automatic Verified badge on Drive mount or account-prefix lookup.** Legacy auto-claims are ignored; everyone must explicitly claim again after updating.
- Much stronger multi-signal keysmash/repetition filtering, plus duplicate and rate checks
- Rapid sends queued in order instead of disabling the text box
- Byte-minimal in-place JSON array tail writes and a private sender retry outbox
- **New normal messages use exactly four JSON fields**: `kind`, `time`, `user`, and `text` (no message ID or signature stored)

## How to run

1. Make sure the existing **original shared** `chat.json` shortcut is in the **root of My Drive** for each user.
2. Stop any earlier Colab chat cells (especially ones that still write to an event folder).
3. Run your **existing launcher** as before. Its old GitHub URL `school_chat.py` is now a small compatibility wrapper that loads the renamed **`messaging_app.py`**.
4. Only the original shared `chat.json` is read and written by this app. There is **no requirement to create or use SchoolChatEvents**.
5. To get the new source directly, use `https://raw.githubusercontent.com/PletzelOnYT/School-Chat-Room/main/messaging_app.py`.

## v5.24 — simple message format

New normal chat messages are stored exactly like this, with no ID:

~~~json
{
  "kind": "message",
  "time": "04:15:50",
  "user": "Pletzel",
  "text": "fdg"
}
~~~

The existing 229 historical records and their existing signed reaction/profile/role events are **not rewritten**. Those metadata events remain structured to support click reactions, Verified checkmarks and old history. No additional shared files/folders are required. Reply messages also have four fields; a short parent-reference marker is stored **inside their text** and removed from the displayed text by the app.

**Trade-offs:** A record without a sender key, signature or ID can be inserted by any editor and **cannot cryptographically prove who sent it**. New plain messages therefore do not receive an authenticated per-message Verified badge. Reactions use a computed in-memory reference and may lose their target if earlier history is deleted/reordered. Date-based search cannot determine the calendar date of new four-field messages, which store only an HH:MM:SS clock time. The owner guardian recognizes these records but cannot authenticate their author.

## Search examples

- `/search from:Alex math`
- `/search after:2026-10-01 before:2026-11-01`
- `/search has:reaction`
- `/search in:replies "homework"`

## Important storage limitation

`chat.json` is a JSON array. A raw append after its closing `]` would make the file invalid. In v5.22, the client instead opens the **existing file in read/write binary mode**, locates its final closing bracket, and inserts only the new signed JSON event plus a new closing bracket. **It does not reserialize the whole message history.**

This is **not an atomic multi-user append**. Colab's Google Drive mount does not provide cross-user transactions or distributed locking. If two sessions write simultaneously, Drive may still drop updates or produce a partial/conflicting version. The client verifies saved event IDs and keeps a limited recent sender outbox at `.schoolchat/message_outbox.json` in **that user's private My Drive**. During active sessions it retries lost signed events. This reduces losses but does not guarantee delivery, especially after another user writes concurrently or if Drive is offline.

If `chat.json` becomes invalid JSON, the app refuses to overwrite it automatically. Restore a prior revision with **Google Drive's version history** rather than letting multiple clients overwrite broken data. Keep your own backup of important messages.

Do not use this app to exchange confidential information. Anyone who has Editor access to the shared file can modify or erase it. The blue checkmark is self-claimed, not verified Google identity.

## Migration note

The previous v5.18 folder-based event system is disabled. Nothing in `SchoolChatEvents` is accessed, changed or deleted by this app. If you used that version, messages saved **only** to its event folder are **not automatically merged** into `chat.json`. Keep a backup of that folder before deleting it.

The GitHub repository's original name and the `school_chat.py` compatibility filename remain solely to avoid breaking existing notebook launchers. The actual application is **Messaging App**, in **`messaging_app.py`**.

## Optional owner guardian

The owner can separately run [owner_guardian.py](owner_guardian.py) to monitor
new chat events, validate their Ed25519 signatures and allowed formats,
keep a **private SQLite backup**, and repair erased or corrupted history.
It does **not** alter the Messaging App UI or require any new shared folder.

See [Owner Guardian setup](OWNER_GUARDIAN_SETUP.md). This is a best-effort
watchdog while members have direct Editor access, not a preventive write gate.

