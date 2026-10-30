# School ChatRoom v5.18 — Drive-only event journal setup

**You must do this one time before launching v5.18.** It is free with Google Drive and does not use Vercel, Redis, Firebase, or another external chat host.

## Why a separate shared folder?

The original `chat.json` is a JSON **array**. Adding data to it requires changing its existing bytes; a plain append after the closing `]` creates invalid JSON. Google Drive shortcuts and the Colab Drive mount do not provide reliable, atomic multi-user append to a shared JSON file.

Instead, v5.18 reads the **existing** `chat.json` as *legacy history* and writes each new chat event to its own immutable file in a **shared** Drive folder. Two students never need to edit the same event file.

## Owner setup (in Google Drive, not Colab)

1. In **your Google Drive → My Drive**, create a folder named **`SchoolChatEvents`** (exact spelling).
2. Right-click the folder → **Share**. Invite every chat participant as **Editor**. Keep general access restricted and use specific school accounts when possible. Anyone with Editor rights can still delete shared files; this is not a secure database.
3. Make sure the original shared `chat.json` still exists. Do **not** delete, move, or replace it.
4. Run your usual Colab GitHub launcher. The chat will automatically create:
   - `SchoolChatEvents/room.json` — a marker for your existing room
   - `SchoolChatEvents/events/` — one small signed JSON file per message, reply, reaction or profile update
   - `SchoolChatEvents/attachments/` — uploaded images/PDFs/text files (maximum **2 MB** each)
5. Keep the folder shared and tell all students about the update.

## Each student's setup

1. Open the **same shared SchoolChatEvents folder** in Google Drive using the owner's invitation.
2. In Drive, choose **Organize → Add shortcut to Drive → My Drive**. The folder shortcut must appear in the **root** of their My Drive as `SchoolChatEvents`. Don't make a copy or create a separate folder of the same name.
3. Keep the **original shared `chat.json` shortcut** in their My Drive root too.
4. Stop any previous Colab chat cells, then rerun the existing GitHub launcher to load v5.18.
5. Try a normal message and check the shared folder's `events` subfolder. A new uniquely named `.json` file should appear. The original `chat.json` should remain unchanged.

**If the folder cannot be shared or mounted by a school-managed account**, that school's policy may prevent this setup. The new code deliberately refuses to revert to overwriting `chat.json`; ask the administrator what shared Drive storage is permitted.

## What's new

- **#07 Clickable reactions:** Click a reaction or `＋ React`; your own selection can be toggled off
- **#04 Reply threads:** Click `↳ Reply`; replies are organized under their original thread and expandable
- **#15 Filtered search:** Use `/search from:Alex math`, `/search after:2026-10-01 before:2026-11-01`, `/search has:attachment`, or `/search in:replies "quiz"`
- **#20 Mobile layout/full-screen:** Touch-friendly controls, compact mobile layout, scrollable conversation and a Full screen button (browser permission permitting)
- **#23 Attachments:** Use **📎 Attach** for PNG, JPEG, WebP, GIF, PDF, TXT or Markdown up to **2 MB**. They are stored in the shared Drive folder and opened only when clicked. Don't upload private information, executable files or anything sensitive.
- **#25 Write reliability:** One file per action; new clients do not rewrite legacy `chat.json`

## Remaining limitations

- Google Drive synchronization is not instant, and directory listings may lag for a few seconds.
- Editors can remove/alter the event files, and the Drive mount cannot guarantee transactions; this is *more resistant to lost simultaneous writes*, not tamper-proof.
- Thousands of individual event files may eventually slow folder listings. A safe archival/compaction strategy would be needed for a large or long-lived chat.
- Older Colab clients can still overwrite the old `chat.json` until everyone stops and restarts.
- The single blue ☑ Verified checkmark remains a **self-claimed chat badge**, not proof of Google account ownership.
- Shared attachments are visible to everyone with access to the folder. The file-type and size checks are not an antivirus scan.

[The GitHub-hosted Python script](https://github.com/PletzelOnYT/School-Chat-Room/blob/main/school_chat.py) is the only application file the launcher needs.
