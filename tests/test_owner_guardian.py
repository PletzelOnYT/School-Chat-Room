"""Safe owner-only guardian tests: signed schema, SQLite and file recovery."""
import base64
import importlib.util
import json
from pathlib import Path
import tempfile
import time
import unittest
import uuid

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

PROGRAM = Path(__file__).resolve().parents[1] / "owner_guardian.py"
spec = importlib.util.spec_from_file_location("owner_guardian", PROGRAM)
guardian = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guardian)


def keypair():
    private=Ed25519PrivateKey.generate()
    public=base64.b64encode(private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
    return private,public


def signed(private, sender, kind="message", **attrs):
    data=dict(id=str(uuid.uuid4()),kind=kind,
              sender_key=sender,issued_at=int(time.time()),
              issued_at_ns=time.time_ns(),time="12:00:00",
              user="Alice",**attrs)
    data["sig"]=base64.b64encode(
        private.sign(guardian.canonical(data))).decode()
    return data


class GuardianTests(unittest.TestCase):
    def setUp(self):
        self.t=tempfile.TemporaryDirectory()
        self.addCleanup(self.t.cleanup)
        self.base=Path(self.t.name)
        self.shared=self.base/"chat.json"
        self.private=self.base/"private"
        self.local=self.base/"local"/"owner.sqlite3"
        self.legacy=dict(id=guardian.ANCHOR,kind="message",
                         user="Legacy",text="hello from legacy history")
        self.shared.write_text(json.dumps([self.legacy]))
        self.private_key,self.public=keypair()
        self.other_key,self.other_public=keypair()
        self.mon=guardian.Guardian(self.shared,self.private,self.local)
        self.addCleanup(self.mon.close)

    def event(self,text="normal message"):
        return signed(self.private_key,self.public,text=text)

    def current(self):
        return json.loads(self.shared.read_text())

    def test_bootstrap_creates_private_database(self):
        self.assertEqual(self.mon.process(),"enrolled")
        self.assertEqual(self.mon.db.execute(
            "SELECT COUNT(*) FROM approved").fetchone()[0],1)
        self.assertTrue((self.private/"guardian.sqlite3").is_file())
        self.assertEqual(len(self.current()),1)

    def test_accept_valid_signed_messages_without_rewriting(self):
        self.mon.process()
        e=self.event("Is everyone ready?")
        self.shared.write_text(json.dumps([self.legacy,e]))
        self.assertEqual(self.mon.process(),"healthy")
        self.assertEqual(guardian.count_events(self.mon.db),2)
        self.assertEqual(self.current()[-1]["id"],e["id"])

    def test_reject_fake_signature_and_unknown_request(self):
        self.mon.process()
        evil=self.event("invalid")
        evil["text"]="tampered while signature remained unchanged"
        unexpected=self.event("ping")
        unexpected["kind"]="owner"
        self.shared.write_text(json.dumps([self.legacy,evil,unexpected]))
        self.assertEqual(self.mon.process(),"restored")
        self.assertEqual(self.current(),[self.legacy])
        self.assertGreaterEqual(self.mon.db.execute(
            "SELECT COUNT(*) FROM incidents").fetchone()[0],2)
        incident_files=list((self.private/"incidents").glob("*.bin"))
        self.assertTrue(incident_files)

    def test_recover_approved_message_deleted_from_history(self):
        self.mon.process()
        a=self.event("keep me")
        self.shared.write_text(json.dumps([self.legacy,a]))
        self.mon.process()
        self.shared.write_text(json.dumps([self.legacy]))
        self.assertEqual(self.mon.process(),"restored")
        self.assertEqual([x["id"] for x in self.current()],
                         [self.legacy["id"],a["id"]])

    def test_recover_full_content_deletion_without_forking_file(self):
        self.mon.process()
        original_inode=self.shared.stat().st_ino
        self.shared.write_bytes(b"[]")
        self.assertEqual(self.mon.process(),"restored")
        self.assertEqual(self.current(),[self.legacy])
        self.assertEqual(self.shared.stat().st_ino,original_inode)

    def test_recover_corrupted_json(self):
        self.mon.process()
        self.shared.write_bytes(b"[this is not valid json")
        self.assertEqual(self.mon.process(),"restored")
        self.assertEqual(self.current(),[self.legacy])

    def test_missing_file_does_not_create_new_room(self):
        self.mon.process()
        self.shared.unlink()
        self.assertEqual(self.mon.process(),"missing")
        self.assertFalse(self.shared.exists())
        self.assertTrue((self.private/"guardian.sqlite3").is_file())

    def test_new_file_on_resume_loads_private_checkpoint(self):
        self.mon.process()
        e=self.event("persist this")
        self.shared.write_text(json.dumps([self.legacy,e]))
        self.mon.process()
        self.mon.close()
        self.shared.write_text("[]")
        restarted=guardian.Guardian(
            self.shared,self.private,self.base/"fresh"/"local.sqlite3")
        try:
            self.assertEqual(restarted.process(),"restored")
            self.assertEqual(self.current()[-1]["id"],e["id"])
        finally:
            restarted.close()

    def test_signed_role_claim_and_unverify_schema(self):
        self.mon.process()
        known={self.legacy["id"]:self.legacy}
        good=signed(self.private_key,self.public,kind="role",
                    role="verified",target_key=self.public,
                    active=True,self_verified=True,
                    verification_version=2)
        self.assertIsNone(guardian.check_new_event(good,known,{}))
        self.mon.db.execute("SELECT 1")
        known[good["id"]]=good
        revoke=signed(self.other_key,self.other_public,kind="role",
                      role="verified",target_key=self.public,
                      active=False,unverify_anyone=True,
                      verification_version=2)
        self.assertIsNone(guardian.check_new_event(revoke,known,{}))
        bad=signed(self.other_key,self.other_public,kind="role",
                   role="owner",target_key=self.other_public,active=True)
        self.assertIn("role",guardian.check_new_event(bad,known,{}))

    def test_nickname_change_requires_current_v2_verification(self):
        known={self.legacy["id"]:self.legacy}
        rename=signed(self.private_key,self.public,kind="profile",
                      renamed=True)
        self.assertIn("nickname change",
                      guardian.check_new_event(rename,known,{}))
        claim=signed(self.private_key,self.public,kind="role",
                     role="verified",target_key=self.public,
                     active=True,self_verified=True,
                     verification_version=2)
        known[claim["id"]]=claim
        self.assertIsNone(guardian.check_new_event(rename,known,{}))
        revoke=signed(self.other_key,self.other_public,kind="role",
                      role="verified",target_key=self.public,
                      active=False,unverify_anyone=True,
                      verification_version=2)
        known[revoke["id"]]=revoke
        self.assertIn("nickname change",
                      guardian.check_new_event(rename,known,{}))

    def test_drive_id_recovery_rejects_invalid_ids_without_auth(self):
        self.assertIn("Invalid",guardian.restore_trash_by_id("../dangerous"))
        self.assertIn("No Drive file ID",guardian.restore_trash_by_id(""))

    def test_reject_unapproved_fields_and_invalid_reply_targets(self):
        known={self.legacy["id"]:self.legacy}
        invalid=signed(self.private_key,self.public,text="hello",
                       execute="delete_everything")
        self.assertIn("fields",guardian.check_new_event(invalid,known,{}))
        orphan=signed(self.private_key,self.public,text="hi",
                      reply_to=str(uuid.uuid4()))
        self.assertIn("reply target",guardian.check_new_event(orphan,known,{}))

    def test_detect_history_reordering(self):
        self.mon.process()
        first=self.event("first")
        second=self.event("second")
        self.shared.write_text(json.dumps([self.legacy,first,second]))
        self.mon.process()
        self.shared.write_text(json.dumps([self.legacy,second,first]))
        self.assertEqual(self.mon.process(),"restored")
        self.assertEqual([e["id"] for e in self.current()],
                         [self.legacy["id"],first["id"],second["id"]])

    def test_accept_and_restore_four_field_messages_without_ids(self):
        self.mon.process()
        simple={"kind":"message","time":"04:15:50",
                "user":"Pletzel","text":"fdg"}
        self.assertTrue(guardian.simple_message(simple))
        self.assertIsNone(guardian.check_new_event(simple,{},{}))
        self.shared.write_text(json.dumps([self.legacy,simple]))
        self.assertEqual(self.mon.process(),"healthy")
        self.assertEqual(guardian.count_events(self.mon.db),2)
        self.assertEqual(json.loads(self.shared.read_text())[-1],simple)
        self.shared.write_text(json.dumps([self.legacy]))
        self.assertEqual(self.mon.process(),"restored")
        self.assertEqual(json.loads(self.shared.read_text())[-1],simple)

    def test_accept_restore_title_badges_without_message_ids(self):
        self.mon.process()
        title={"kind":"title","time":"04:15:50","user":"Pletzel",
               "title":"Verified"}
        self.assertTrue(guardian.simple_title(title))
        self.assertIsNone(guardian.check_new_event(title,{},{}))
        self.shared.write_text(json.dumps([self.legacy,title]))
        self.assertEqual(self.mon.process(),"healthy")
        self.assertEqual(guardian.count_events(self.mon.db),2)
        self.shared.write_text(json.dumps([self.legacy]))
        self.assertEqual(self.mon.process(),"restored")
        self.assertEqual(json.loads(self.shared.read_text())[-1],title)

    def test_guardian_rejects_fake_owner_title(self):
        invalid={"kind":"title","time":"04:15:50","user":"Pletzel",
                 "title":"Owner"}
        self.assertFalse(guardian.simple_title(invalid))
        self.assertIn("unknown",guardian.check_new_event(invalid,{},{}))

    def test_accept_and_restore_simple_rename(self):
        self.mon.process()
        rename={"kind":"rename","time":"04:16:00",
                "user":"Guest-626","name":"Pletzel"}
        self.assertTrue(guardian.simple_rename(rename))
        self.assertIsNone(guardian.check_new_event(rename,{},{}))
        self.shared.write_text(json.dumps([self.legacy,rename]))
        self.assertEqual(self.mon.process(),"healthy")
        self.assertEqual(guardian.count_events(self.mon.db),2)
        self.shared.write_text(json.dumps([self.legacy]))
        self.assertEqual(self.mon.process(),"restored")
        self.assertEqual(json.loads(self.shared.read_text())[-1],rename)

    def test_reject_malicious_rename_schema(self):
        invalid={"kind":"rename","time":"04:16:00",
                 "user":"Guest-626","name":"Owner"}
        self.assertFalse(guardian.simple_rename(invalid))
        self.assertIn("unknown",guardian.check_new_event(invalid,{},{}))

    def test_tampered_approved_text_restored(self):
        self.mon.process()
        e=self.event("safely signed text")
        self.shared.write_text(json.dumps([self.legacy,e]))
        self.mon.process()
        bad=dict(e,text="REPLACED WITHOUT KEY")
        self.shared.write_text(json.dumps([self.legacy,bad]))
        self.assertEqual(self.mon.process(),"restored")
        self.assertEqual(self.current()[-1]["text"],"safely signed text")


if __name__=="__main__":
    unittest.main()
