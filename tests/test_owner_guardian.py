"""Safe owner-only guardian tests: signed schema, SQLite and file recovery."""
import base64
import importlib.util
import json
from pathlib import Path
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

PROGRAM = Path(__file__).resolve().parents[1] / "guardian" / "owner_guardian.py"
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

    def test_overdrive_integrity_check_blocks_tampered_checkpoint_and_repair(self):
        self.mon.process()
        backup=self.private/"guardian.sqlite3"
        protected=backup.read_bytes()
        self.mon.db.execute(
            "UPDATE approved SET json=? WHERE position=1",
            ('{"kind":"message","text":"FORGED"}',))
        self.mon.db.commit()
        self.shared.write_bytes(b"[]")
        before=self.shared.read_bytes()
        with self.assertRaises(guardian.GuardianLedgerError):
            self.mon.process()
        self.assertEqual(self.shared.read_bytes(),before)
        with self.assertRaises(guardian.GuardianLedgerError):
            self.mon.backup()
        self.assertEqual(backup.read_bytes(),protected)

    def test_overdrive_rejects_ledger_id_or_digest_mismatch(self):
        self.mon.process()
        self.mon.db.execute(
            "UPDATE approved SET event_id='forged-id' WHERE position=1")
        self.mon.db.commit()
        with self.assertRaises(guardian.GuardianLedgerError):
            guardian.db_events(self.mon.db)

    def test_overdrive_prevents_stale_snapshot_rewind(self):
        self.mon.process()
        before=self.shared.read_bytes()
        self.shared.write_bytes(before+b" ")
        other_user_bytes=self.shared.read_bytes()
        with self.assertRaises(guardian.ConcurrentChatChange):
            guardian.recover_existing(
                self.shared,[self.legacy],self.private,expected_raw=before)
        self.assertEqual(self.shared.read_bytes(),other_user_bytes)
        self.assertFalse((self.private/"incidents").exists())

    def test_overdrive_does_not_wipe_new_messages_during_repair(self):
        self.mon.process()
        self.shared.write_bytes(b"[]")
        newly_written=json.dumps([self.legacy,self.event("new student message")]).encode()

        def race(_path,_events,_private,expected_raw=None):
            self.assertEqual(expected_raw,b"[]")
            self.shared.write_bytes(newly_written)
            raise guardian.ConcurrentChatChange("New concurrent append")
        with patch.object(guardian,"recover_existing",side_effect=race):
            self.assertEqual(self.mon.process(),"rescan")
        self.assertEqual(self.shared.read_bytes(),newly_written)
        self.assertEqual(self.mon.process(),"healthy")

    def test_overdrive_owner_private_health_status_is_atomic_and_minimal(self):
        report=guardian.write_health_status(
            self.private,"healthy",started_at="2026-10-30T00:00:00+00:00",
            scans=42,errors=0,last_success="2026-10-30T00:05:00+00:00",
            approved=3,bytes_on_disk=1234,running=True)
        self.assertEqual(report,self.private/"guardian_health.json")
        data=json.loads(report.read_text())
        self.assertEqual(data["guardian_version"],"3.2")
        self.assertEqual(data["scans"],42)
        self.assertEqual(data["approved_records"],3)
        self.assertTrue(data["running"])
        self.assertNotIn("text",data)
        self.assertFalse((self.private/"guardian_health.pending.json").exists())

    def test_overdrive_verified_backup_preserves_prior_backup_on_failure(self):
        self.mon.process()
        checkpoint=self.private/"guardian.sqlite3"
        frozen=checkpoint.read_bytes()
        with patch.object(guardian, "db_events",side_effect=guardian.GuardianLedgerError("damaged")):
            with self.assertRaises(guardian.GuardianLedgerError):
                self.mon.backup()
        self.assertEqual(checkpoint.read_bytes(),frozen)

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

    def test_whisper_is_approved_but_not_a_public_chat_message(self):
        self.assertEqual(self.mon.process(),"enrolled")
        whisper={"kind":"whisper","time":"12:00:00","user":"Alice",
                 "to":"Emma","text":"hello privately in client UI"}
        self.assertTrue(guardian.simple_whisper(whisper))
        self.assertIsNone(guardian.check_new_event(whisper,{},{}))
        self.shared.write_text(json.dumps([self.legacy,whisper]))
        self.assertEqual(self.mon.process(),"healthy")
        self.assertEqual(guardian.count_events(self.mon.db),2)
        self.shared.write_text(json.dumps([self.legacy]))
        self.assertEqual(self.mon.process(),"restored")
        self.assertEqual(self.current()[-1],whisper)
        self.assertFalse(guardian.simple_whisper(dict(whisper,to="Owner")))
        self.assertFalse(guardian.simple_whisper(dict(whisper,extra="no")))

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


    def test_guardian_accepts_valid_clock_sidecar_and_repairs_tampering(self):
        self.assertEqual(self.mon.process(),"enrolled")
        stamp=int(time.time())
        message={"kind":"message",
                 "time":time.strftime("%H:%M:%S",time.gmtime(stamp)),
                 "user":"Pletzel","text":"date-corrected message"}
        clock={"kind":"clock","time":message["time"],"user":"Pletzel",
               "epoch":stamp,"message_hash":guardian.plain_message_hash(message)}
        self.assertTrue(guardian.simple_clock(clock))
        self.assertIsNone(guardian.check_new_event(clock,{"m":message},{}))
        self.shared.write_text(json.dumps([self.legacy,message,clock]))
        self.assertEqual(self.mon.process(),"healthy")
        self.assertEqual(guardian.count_events(self.mon.db),3)
        corrupt=dict(clock,epoch=stamp+3600)
        self.assertFalse(guardian.simple_clock(corrupt))
        self.shared.write_text(json.dumps([self.legacy,message,corrupt]))
        self.assertEqual(self.mon.process(),"restored")
        self.assertEqual(self.current()[-1],clock)

    def test_guardian_rejects_unanchored_clock(self):
        stamp=int(time.time())
        clock={"kind":"clock","time":time.strftime("%H:%M:%S",time.gmtime(stamp)),
               "user":"Pletzel","epoch":stamp,"message_hash":"a"*64}
        self.assertIn("missing chat message",
                      guardian.check_new_event(clock,{},{}))


    def test_historical_time_report_uses_midnight_rollover_not_reordering(self):
        stamp=1793498400  # 2026-11-01 02:00 UTC
        last={"kind":"message",
              "time":time.strftime("%H:%M:%S",time.gmtime(stamp)),
              "user":"Alice","text":"known UTC"}
        proof={"kind":"clock","time":last["time"],"user":"Alice",
               "epoch":stamp,"message_hash":guardian.plain_message_hash(last)}
        old={"kind":"message","time":"23:58:00","user":"Alice","text":"before midnight"}
        midnight={"kind":"message","time":"00:01:00","user":"Alice","text":"after midnight"}
        initial=[old,midnight,last,proof]
        report=guardian.historical_clock_audit(initial,now=stamp)
        rows=report["messages"]
        self.assertEqual([item["position"] for item in rows],[0,1,2])
        self.assertEqual(rows[-1]["basis"],"clock_metadata")
        self.assertEqual(rows[0]["basis"],"inferred_utc_not_verified")
        self.assertEqual(rows[0]["epoch"]%86400,23*3600+58*60)
        self.assertEqual(rows[1]["epoch"]%86400,60)
        self.assertTrue(rows[0]["epoch"]<rows[1]["epoch"]<rows[2]["epoch"])
        self.assertEqual(initial[0],old)

    def test_live_exact_clock_sidecars_detect_out_of_order_posts_without_rewriting(self):
        # Real shared chat can have 02:57:37 appended before 02:57:35.
        older={"kind":"message","time":"02:57:37","user":"Sasha","text":"hi"}
        newer={"kind":"message","time":"02:57:35","user":"Pletzel","text":"test"}
        def sidecar(message,epoch):
            return {"kind":"clock","time":message["time"],
                    "user":message["user"],"epoch":epoch,
                    "message_hash":guardian.plain_message_hash(message)}
        live=[older,sidecar(older,1793415457),
              newer,sidecar(newer,1793415455)]
        frozen=json.dumps(live)
        report=guardian.historical_clock_audit(live,now=1793415457)
        self.assertEqual(report["near_order_inversions"],[{
            "previous_position":0,"next_position":2,
            "seconds":2,"confidence":"exact"}])
        self.assertEqual(json.dumps(live),frozen)

    def test_legacy_short_clock_inversion_is_not_converted_to_one_day(self):
        earlier={"kind":"message","time":"02:09:28","user":"Sasha","text":"test"}
        later={"kind":"message","time":"02:09:25","user":"Pletzel","text":"hi"}
        anchored={"kind":"message","time":"02:57:37","user":"Sasha","text":"known"}
        proof={"kind":"clock","time":"02:57:37","user":"Sasha",
               "epoch":1793415457,
               "message_hash":guardian.plain_message_hash(anchored)}
        report=guardian.historical_clock_audit(
            [earlier,later,anchored,proof],now=1793415457)
        times=report["messages"]
        self.assertEqual(times[0]["epoch"]-times[1]["epoch"],3)
        self.assertEqual(report["near_order_inversions"][0]["confidence"],"inferred")

    def test_guardian_writes_private_clock_audit_without_rewriting_chat(self):
        original=self.shared.read_bytes()
        self.assertEqual(self.mon.process(),"enrolled")
        report_path=self.private/"historical_clock_audit.json"
        self.assertTrue(report_path.is_file())
        parsed=json.loads(report_path.read_text())
        self.assertIn("counts",parsed)
        self.assertEqual(self.shared.read_bytes(),original)

    def test_historical_baseline_does_not_count_as_fresh_flood(self):
        initial=[{"kind":"message","time":"13:00:00",
                  "user":"Alice","text":"old note "+str(i)}
                 for i in range(guardian.RATE_60_SECONDS+8)]
        self.shared.write_text(json.dumps(initial))
        self.assertEqual(self.mon.process(),"enrolled")
        existing=self.mon.db.execute(
            "SELECT COUNT(*) FROM approved WHERE observed_at IS NOT NULL").fetchone()[0]
        self.assertEqual(existing,0)
        new={"kind":"message","time":"13:00:01",
             "user":"Alice","text":"brand new message"}
        self.shared.write_text(json.dumps(initial+[new]))
        self.assertEqual(self.mon.process(),"healthy")

    def test_cross_poll_spam_limits_remain_active(self):
        self.mon.process()
        records=[self.legacy]
        for index in range(guardian.RATE_10_SECONDS):
            records.append({"kind":"message","time":"12:00:00",
                            "user":"Alice","text":"unique post "+str(index)})
            self.shared.write_text(json.dumps(records))
            self.assertEqual(self.mon.process(),"healthy")
        records.append({"kind":"message","time":"12:00:00",
                        "user":"Alice","text":"unique post blocked"})
        self.shared.write_text(json.dumps(records))
        self.assertEqual(self.mon.process(),"restored")
        self.assertEqual(guardian.count_events(self.mon.db),1+guardian.RATE_10_SECONDS)
        self.assertEqual(len(self.current()),1+guardian.RATE_10_SECONDS)

    def test_guardian_name_rejects_format_control(self):
        self.assertFalse(guardian.valid_name("Alex\\u202e"))
        self.assertFalse(guardian.valid_name("Al\\u200bex"))

    def test_guardian_reads_framed_append_only_events(self):
        self.mon.process()
        original=self.shared.read_bytes()
        first={"kind":"message","time":"04:15:50",
               "user":"Pletzel","text":"append-only message"}
        second={"kind":"title","time":"04:15:51",
                "user":"Pletzel","title":"Verified"}
        with open(self.shared,"ab") as fp:
            for record in (first,second):
                fp.write(b"\n\x1e"+json.dumps(record,indent=2).encode()+b"\n")
        raw,records=guardian.read_shared(self.shared)
        self.assertTrue(raw.startswith(original))
        self.assertEqual(records[-2:],[first,second])
        self.assertEqual(self.mon.process(),"healthy")
        self.assertEqual(guardian.count_events(self.mon.db),3)
        self.assertTrue(self.shared.read_bytes().startswith(original))

    def test_guardian_does_not_overwrite_incomplete_journal_frame(self):
        self.mon.process()
        with open(self.shared,"ab") as fp:
            fp.write(b"\n\x1e{\"kind\":")
        broken=self.shared.read_bytes()
        self.assertEqual(self.mon.process(),"corrupt")
        self.assertEqual(self.shared.read_bytes(),broken)

    def test_guardian_rejects_back_inserted_valid_signed_event(self):
        """An authentic signature must not authorize rewriting past order."""
        self.assertEqual(self.mon.process(),"enrolled")
        inserted=self.event("signed but placed before approved history")
        self.shared.write_text(json.dumps([inserted,self.legacy]))
        self.assertEqual(self.mon.process(),"restored")
        self.assertEqual(self.current(),[self.legacy])
        reasons=[r[0] for r in self.mon.db.execute(
            "SELECT reason FROM incidents")]
        self.assertTrue(any("prefix" in x for x in reasons),reasons)

    def test_guardian_detects_duplicate_json_object_keys(self):
        """Ambiguous objects must not pass through parser last-key-wins."""
        self.mon.process()
        ambiguous=b'[{"id":"'+guardian.ANCHOR.encode()+(
            b'","kind":"message","kind":"profile","user":"Legacy","text":"test"}]')
        self.shared.write_bytes(ambiguous)
        with self.assertRaisesRegex(ValueError,"duplicate JSON object key"):
            guardian.decode_shared_stream(ambiguous)
        self.assertEqual(self.mon.process(),"restored")
        self.assertEqual(self.current(),[self.legacy])

    def test_guardian_preserves_ambiguous_append_journal_for_inspection(self):
        self.mon.process()
        with open(self.shared,"ab") as stream:
            stream.write(b'\n\x1e{"kind":"message","kind":"title","time":"12:00:00","user":"Alice"}')
        original=self.shared.read_bytes()
        self.assertEqual(self.mon.process(),"corrupt")
        self.assertEqual(self.shared.read_bytes(),original)

    def test_guardian_rejects_nonfinite_numbers_and_invalid_clocks(self):
        with self.assertRaisesRegex(ValueError,"non-finite"):
            guardian.decode_shared_stream(b'[{"kind":"message","value":NaN}]')
        invalid={"kind":"message","time":"99:88:77",
                 "user":"Alice","text":"hello"}
        self.assertFalse(guardian.simple_message(invalid))
        self.assertEqual(guardian.check_new_event(invalid,{},{}),
                         "invalid event ID")
        self.assertEqual(guardian.invalid_text("looks\u200bnormal"),
                         "invisible formatting characters")
        self.assertIsNone(guardian.invalid_text("emoji \U0001f469\u200d\U0001f4bb works"))

    def test_guardian_metadata_flood_across_scans(self):
        self.mon.process()
        records=[self.legacy]
        for i in range(guardian.META_RATE_10_SECONDS):
            records.append({"kind":"title","time":"12:00:00",
                            "user":"Alice","title":"Verified" if i%2 else "None"})
        self.shared.write_text(json.dumps(records))
        self.assertEqual(self.mon.process(),"healthy")
        self.assertEqual(guardian.count_events(self.mon.db),len(records))
        records.append({"kind":"title","time":"12:00:00",
                        "user":"Alice","title":"Verified"})
        self.shared.write_text(json.dumps(records))
        self.assertEqual(self.mon.process(),"restored")
        self.assertEqual(self.current(),records[:-1])

    def test_guardian_cross_nickname_repetition_and_room_burst(self):
        rate={}
        for i in range(guardian.CROSS_NAME_REPEAT_20_SECONDS):
            event={"kind":"message","time":"12:00:00",
                   "user":"Student"+str(i),"text":"identical repeated chat"}
            self.assertIsNone(guardian.check_new_event(event,{},rate,now=1000))
        bad={"kind":"message","time":"12:00:00",
             "user":"Student999","text":"identical repeated chat"}
        self.assertIn("cross-nickname",guardian.check_new_event(
            bad,{},rate,now=1000))
        burst={}
        for i in range(guardian.ROOM_RATE_10_SECONDS):
            good={"kind":"message","time":"12:00:00",
                  "user":"Student"+str(i),"text":"unique content "+str(i)}
            self.assertIsNone(guardian.check_new_event(good,{},burst,now=1000))
        self.assertIn("room-wide message burst",guardian.check_new_event({
            "kind":"message","time":"12:00:00","user":"Student999",
            "text":"another unique post"}, {}, burst, now=1000))

    def test_signed_rate_limit_uses_observed_not_sender_epoch(self):
        rate={}
        now=int(time.time())
        for i in range(guardian.RATE_10_SECONDS):
            e=signed(self.private_key,self.public,text="unique text "+str(i))
            e["issued_at"]=now-3000*i
            e["issued_at_ns"]=e["issued_at"]*1000000000
            e["sig"]=base64.b64encode(
                self.private_key.sign(guardian.canonical(e))).decode()
            self.assertIsNone(guardian.check_new_event(e,{},rate,now=now))
        last=signed(self.private_key,self.public,text="one last post")
        self.assertIn("message rate exceeded",guardian.check_new_event(
            last,{},rate,now=now))

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
