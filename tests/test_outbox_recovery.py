"""Local sender receipts recover messages after another Drive client overwrites them."""
import ast
import json
import os
import re
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1].joinpath("messaging_app.py").read_text()
TREE = ast.parse(SOURCE)
FUNCTIONS = [node for node in TREE.body
             if isinstance(node, ast.FunctionDef) and node.name in {
                 'read_messages', 'append_event_in_place', 'write_event',
                 'load_outbox', 'save_outbox', 'remember_outgoing',
                 'reconcile_outbox',
             }]


class PrivateRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        private = base / '.schoolchat'
        private.mkdir()
        self.file = base / 'chat.json'
        self.anchor = str(uuid.uuid4())
        self.original = [{'id': self.anchor, 'kind': 'message', 'text': 'original'}]
        self.file.write_text(json.dumps(self.original))
        self.env = {
            'os': os, 're': re, 'json': json, 'time': time,
            'uuid': uuid, 'file_lock': threading.RLock(),
            'CHAT_FILE_PATH': str(self.file),
            'PRIVATE_FOLDER': str(private),
            'PRIVATE_OUTBOX_PATH': str(private / 'message_outbox.json'),
            'ORIGINAL_HISTORY_ANCHOR': self.anchor,
            'identity_public': 'alice-signed-key',
            'last_outbox_check': 0.0,
            'valid_signature': lambda e: e.get('sig') == 'good',
            'ChatFileError': type('ChatFileError', (Exception,), {}),
        }
        exec(compile(ast.Module(body=FUNCTIONS, type_ignores=[]),
                     '<outbox>', 'exec'), self.env)

    def event(self):
        return {
            'id': str(uuid.uuid4()), 'kind': 'message',
            'sender_key': 'alice-signed-key', 'sig': 'good',
            'issued_at': int(time.time()), 'text': 'must not disappear',
        }

    def test_recover_outgoing_after_competing_save(self):
        event = self.event()
        self.assertTrue(self.env['write_event'](event))
        self.assertEqual(len(self.env['read_messages']()), 2)
        self.assertEqual(len(self.env['load_outbox']()), 1)
        # Simulate an older Colab client saving stale history. That client's
        # full-file rewrite removes the latest event in Google Drive.
        self.file.write_text(json.dumps(self.original))
        self.env['reconcile_outbox']()
        records = self.env['read_messages']()
        self.assertEqual(len(records), 2)
        self.assertEqual(records[-1]['id'], event['id'])
        self.assertEqual(len(self.env['load_outbox']()), 1)

    def test_damaged_json_is_never_overwritten(self):
        bad = b'[{unclosed'
        self.file.write_bytes(bad)
        with self.assertRaises(self.env['ChatFileError']):
            self.env['write_event'](self.event())
        self.assertEqual(self.file.read_bytes(), bad)

    def test_duplicate_event_is_not_reappended(self):
        event = self.event()
        self.assertTrue(self.env['write_event'](event))
        size = self.file.stat().st_size
        self.assertTrue(self.env['write_event'](event))
        self.assertEqual(size, self.file.stat().st_size)
        self.assertEqual(len(self.env['load_outbox']()), 1)


if __name__ == '__main__':
    unittest.main()
