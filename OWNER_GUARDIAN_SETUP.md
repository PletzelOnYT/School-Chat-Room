# Messaging App — Owner Guardian v2.3

The owner runs a **separate Python program** (`owner_guardian.py`) in their own Google Colab notebook. The chat itself, including the original terminal UI and `chat.json`, remains unchanged. No Vercel, Redis, or additional shared folder is required.

## Important v5.30 append-only format change

**Stop any older Guardian before launching v5.30 chat clients.** Prior
Guardian versions assume `chat.json` is one JSON array and may rewrite
appended records as though the file were corrupt.

The updated Guardian reads both the original JSON array and the extra
record-separated JSON events appended after it. Appended frames are not
one valid JSON document, so normal `json.load()` will not parse the
entire file. A partially synchronized append will be flagged for
inspection rather than triggering an automatic overwrite.

Use the newest `owner_guardian.py`, preferably test with `--audit-only`
first. Keep your existing healthy SQLite backup; **do not reset it** just
because the app switched to append-only writes.

## What it monitors

- Every 5 seconds, checks the **original** shared `chat.json`.
- Checks **new four-field plain messages** for structure, length, spam and a valid display name. Other internal `profile`, `reaction` and `role` records still require signed identities and strict schemas. Four-field messages contain no ID/signature, so they **cannot be authenticated or attributed securely**.
- Detects invalid event types, fake owner/mod grants, replayed IDs, suspicious repeats, and abnormal sending rates.
- Saves every accepted event in SQLite, with backups at **`MyDrive/.messaging_guardian/guardian.sqlite3`** in the owner's **private** Drive folder. Incidents are logged in SQLite; suspect file bytes are preserved under `.messaging_guardian/incidents/`.
- If messages disappear, are modified, or are reordered, repairs the existing shared file using the approved history.
- If the file is empty, corrupted, or truncated, attempts to restore the approved history **in place**.
- If the original Drive object is in Trash, it optionally attempts to **untrash the original file ID** using a Google token already available in the session.

## Owner Guardian v2.3: Google Colab startup fix

The launcher now prevents Jupyter's `-f ...kernel.json` argument from
reaching Guardian's command-line parser. To use the fix, rerun the same
one-cell launcher shown below. **Do not add `--reset-baseline`** for this fix.
Existing private backups and `chat.json` are preserved.

## Automatic setup in the owner's Google Colab

**Only the owner should run Guardian.** All participants run `school_chat.py` /
`messaging_app.py`, but running multiple Guardians against a single shared file
can result in competing recovery writes. Do not add the Guardian launcher to
every student's chat notebook.

Guardian v2.2 now automatically:
1. Mounts the owner's Google Drive when a Colab mount is not already present (Google may ask for permission).
2. Checks `My Drive/chat.json` and reuses it when already available.
3. If missing, attempts to create a **shortcut** (not a new chat copy) that points
   to the exact original Drive file ID
   `1CysiJWMvyW6AOYXtkAnf99JSIhAQHrMd`, using previously authorized Drive API credentials.
4. Keeps its SQLite database, incident logs, and clock audit private in
   `My Drive/.messaging_guardian/` — **never** in the shared chat file/folder.
5. Resumes using the previously approved backup when it exists and monitors
   every five seconds while the owner's Colab session remains active.

You can launch the latest Guardian automatically from **one cell in the
owner's own Colab notebook**:

```python
import urllib.request
url = "https://raw.githubusercontent.com/PletzelOnYT/School-Chat-Room/main/guardian_launcher.py"
exec(compile(urllib.request.urlopen(url).read().decode("utf-8"), url, "exec"))
```

This downloads the latest launcher and starts the current Guardian program.
No manually entered file ID, manual Drive mount, copy of `chat.json`, or manual
`--file` argument is needed.

**Before the Guardian's first-ever run**, inspect and save a copy of the original
chat history privately; enrollment treats the existing history as the trusted
baseline, even if the content had already been modified. Existing Guardian owners
should keep their healthy `.messaging_guardian/guardian.sqlite3` database.
**Do not reset the baseline merely because you upgraded**; that discards the
approved recovery history. Use `--reset-baseline` only to deliberately migrate
an incompatible older checkpoint after verifying and backing up the current chat.

If the school's Google account restricts Drive API authorization, the Guardian
will display an actionable error instead of trying to bypass restrictions.
In that situation, open the original shared file with the same Google account,
choose *Organize → Add shortcut*, put the shortcut at the root of *My Drive*,
and restart the Guardian. If the mount does not immediately show the shortcut,
remount Google Drive and retry. The Guardian **never changes sharing permissions**
or creates another `chat.json` copy.

For a non-destructive, one-pass safety inspection using the original script:

```python
# Once owner_guardian.py is imported as the module "guardian":
guardian.main(["--audit-only", "--once"])
```

For advanced use, explicit `--file` and `--private` paths are still supported
and skip automatic shortcut enrollment. The existing `--interval`, `--once`,
`--audit-only`, and optional original-file recovery ID flags are preserved.

**This is not an always-on service.** The program monitors and repairs only
while the owner's Colab runtime remains connected and the Guardian cell is
running. It does not start in the background on every student's behalf.

## Important limits

The guardian is a **watchdog/repairer**, not an access-control server. Since all participants still have Editor permission on `chat.json`, it cannot stop their unauthorized writes *before* they reach the file. Google Drive's Colab mount also does not provide an atomic multi-user transaction: a repair can race with a student's message. Keep backups.

**Deleting all contents** while the original shared file still exists can be automatically repaired. **Moving the original to Trash** may be recoverable by original file ID if Drive permissions allow it. **Permanently deleting** the original file cannot be undone while preserving the same original Drive object ID, and a missing shortcut alone does not authorize creating a different replacement file. The guardian deliberately avoids forking the room.

It works **only while its Colab runtime is connected**; it will not continue after the notebook stops. The private database and incident files contain copies of chat history: keep the `.messaging_guardian` folder **private** and inform chat participants about backup retention.

A local monitor cannot guarantee that all illegal behavior is prevented. Its "invalid requests" policy is about unsupported event formats, invalid cryptographic signatures, abnormal rates and message integrity; it is not a complete legal/content moderation system.
