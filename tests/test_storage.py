import base64
import tempfile
import threading
import unittest

from src.database import DatabaseManager
from src.storage import ChatStorage
from src.writer import OrderedWriter


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.storage = ChatStorage(self.directory.name)
        self.chat = self.storage.create_chat()
        self.storage.writer.flush().result(2)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.storage.writer.flush().result(2)
        # A future completes immediately before the drain thread becomes idle.
        self.storage.writer._executor.submit(lambda: None).result(2)
        self.storage.writer.shutdown()
        self.directory.cleanup()

    def test_order_and_metadata_roundtrip(self):
        self.storage.save_chat(self.chat['id'], [{'role': 'user', 'content': 'older'}])
        messages = [{'role': 'user', 'content': 'newer', 'images': [base64.b64encode(b'image').decode()]},
                    {'role': 'assistant', 'content': 'partial', 'thinking_content': 'reasoning',
                     'response_metadata': {'status': 'stopped', 'metrics': {'prompt_eval_cached_count': 0}}}]
        self.storage.save_chat(self.chat['id'], messages, options={'thinking_val': None}).result(2)
        chat = self.storage.get_chat(self.chat['id'])
        self.assertEqual(chat['messages'], messages)
        self.assertIsNone(chat['options']['thinking_val'])

    def test_delete_and_clear_cannot_be_undone_by_late_saves(self):
        self.storage.delete_chat(self.chat['id'])
        self.storage.save_chat(self.chat['id'], [{'role': 'user', 'content': 'late'}]).result(2)
        self.assertIsNone(self.storage.get_chat(self.chat['id']))
        chat = self.storage.create_chat()
        self.storage.clear_all_chats()
        self.storage.save_chat(chat['id'], [{'role': 'user', 'content': 'late'}]).result(2)
        self.assertEqual(self.storage.get_all_chats(), [])

    def test_snapshot_transaction_rolls_back_on_invalid_image(self):
        messages = [{'role': 'user', 'content': 'original'}]
        self.storage.save_chat(self.chat['id'], messages, model='old').result(2)
        with self.assertRaises(Exception):
            self.storage.db.save_chat(self.chat['id'], [{'role': 'user', 'content': 'bad', 'images': ['!']}], model='new')
        chat = self.storage.get_chat(self.chat['id'])
        self.assertEqual(chat['messages'], messages)
        self.assertEqual(chat['model'], 'old')

    def test_version_three_migration_preserves_messages(self):
        path = self.directory.name + '/v3.db'
        db = DatabaseManager(path)
        db.create_chat('chat', 'old chat', 1, 1, 'model')
        db.save_messages('chat', [{'role': 'assistant', 'content': 'saved'}])
        with db._get_conn() as conn:
            for column in ('response_metadata', 'tool_calls', 'tool_name', 'tool_call_id'):
                conn.execute('ALTER TABLE messages DROP COLUMN ' + column)
            conn.execute('PRAGMA user_version = 3')
            conn.commit()
        db = DatabaseManager(path)
        self.assertEqual(db.get_chat('chat')['messages'], [{'role': 'assistant', 'content': 'saved'}])
        with db._get_conn() as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 5)

    def test_connection_closes_after_context(self):
        with self.storage.db._get_conn() as conn:
            conn.execute('SELECT 1')
        with self.assertRaises(Exception):
            conn.execute('SELECT 1')


class WriterTests(unittest.TestCase):
    def test_failure_retains_job_and_blocks_newer_writes_until_retry(self):
        writer = OrderedWriter()
        blocked = True
        order = []
        def write():
            if blocked:
                raise OSError('disk full')
            order.append('first')
        failed = writer.submit(write)
        with self.assertRaisesRegex(OSError, 'disk full'):
            failed.result(2)
        second = writer.submit(lambda: order.append('second'))
        self.assertFalse(second.done())
        with self.assertRaisesRegex(RuntimeError, 'unsaved'):
            writer.shutdown()
        blocked = False
        writer.retry()
        second.result(2)
        writer._executor.submit(lambda: None).result(2)
        self.assertEqual(order, ['first', 'second'])
        writer.shutdown()
