# Messaging App — Owner Guardian

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

## Setup in the owner's Colab notebook

**Before first run**, make a backup and confirm that the current shared `chat.json` is the correct history. On its first run, the guardian treats that history as the trusted baseline; it cannot establish whether older events were already modified.

Run the following in a separate Colab notebook:

```python
from google.colab import drive
drive.mount('/content/drive')

import importlib.util
import urllib.request

url = "https://raw.githubusercontent.com/PletzelOnYT/School-Chat-Room/main/owner_guardian.py"
path = "/content/owner_guardian.py"
urllib.request.urlretrieve(url, path)

spec = importlib.util.spec_from_file_location("owner_guardian", path)
guardian = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guardian)

guardian.main([
    "--interval", "5",
    "--file-id", "PASTE_ORIGINAL_DRIVE_FILE_ID_HERE"
])
```

To find the **original file ID**, open the shared file in Google Drive and copy the ID between `/d/` and `/view` in its URL. Use the ID of the real original file, **not** a shortcut or copy. Replace the placeholder; if unavailable, remove both `"--file-id"` and its placeholder. The guardian remembers an entered ID in your private Drive folder.

**Migration requirement:** The live `chat.json` was converted to 196 four-field messages. Stop any guardian process from before v5.25, then on your first start with the updated guardian add `"--reset-baseline"` to the argument list, e.g.:

```python
guardian.main(["--interval", "5", "--reset-baseline"])
```

This archives the previous private SQLite checkpoint before enrolling the new simplified history. Run this option only once, and only after confirming the correct migrated shared file. Running an older guardian without the new migration handling could restore the deleted old metadata records.

Keep this cell running. It will print alerts and actions. Later launches reload the private SQLite backup rather than blindly trusting the shared file again.

To make a one-time check without writing repairs, use `guardian.main(["--audit-only", "--once"])` after downloading/importing the script.

## Important limits

The guardian is a **watchdog/repairer**, not an access-control server. Since all participants still have Editor permission on `chat.json`, it cannot stop their unauthorized writes *before* they reach the file. Google Drive's Colab mount also does not provide an atomic multi-user transaction: a repair can race with a student's message. Keep backups.

**Deleting all contents** while the original shared file still exists can be automatically repaired. **Moving the original to Trash** may be recoverable by original file ID if Drive permissions allow it. **Permanently deleting** the original file cannot be undone while preserving the same original Drive object ID, and a missing shortcut alone does not authorize creating a different replacement file. The guardian deliberately avoids forking the room.

It works **only while its Colab runtime is connected**; it will not continue after the notebook stops. The private database and incident files contain copies of chat history: keep the `.messaging_guardian` folder **private** and inform chat participants about backup retention.

A local monitor cannot guarantee that all illegal behavior is prevented. Its "invalid requests" policy is about unsupported event formats, invalid cryptographic signatures, abnormal rates and message integrity; it is not a complete legal/content moderation system.
