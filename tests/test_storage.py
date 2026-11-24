import base64
import sqlite3
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
            self.storage.db.chats.save_chat(self.chat['id'], [{'role': 'user', 'content': 'bad', 'images': ['!']}], model='new')
        chat = self.storage.get_chat(self.chat['id'])
        self.assertEqual(chat['messages'], messages)
        self.assertEqual(chat['model'], 'old')

    def test_version_three_migration_preserves_messages(self):
        path = self.directory.name + '/v3.db'
        db = DatabaseManager(path)
        db.chats.create_chat('chat', 'old chat', 1, 1, 'model')
        db.chats.save_messages('chat', [{'role': 'assistant', 'content': 'saved'}])
        with db._get_conn() as conn:
            from legacy_schema import downgrade_to_version_three
            downgrade_to_version_three(conn)
        db = DatabaseManager(path)
        self.assertEqual(db.chats.get_chat('chat')['messages'], [{'role': 'assistant', 'content': 'saved'}])
        with db._get_conn() as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 12)

    def test_a_thread_keeps_one_connection_and_threads_do_not_share(self):
        db = self.storage.db
        with db._get_conn() as first:
            with db._get_conn() as nested:
                self.assertIs(nested, first)
        with db._get_conn() as again:
            self.assertIs(again, first)
            # Set up once, not on every use.
            self.assertEqual(again.execute('PRAGMA foreign_keys').fetchone()[0], 1)
            self.assertTrue(again.execute('SELECT vec_version()').fetchone()[0])
        seen = []
        def other():
            with db._get_conn() as conn:
                seen.append(conn)
        worker = threading.Thread(target=other)
        worker.start()
        worker.join()
        self.assertIsNot(seen[0], first)

    def test_uncommitted_work_is_rolled_back_when_a_use_ends(self):
        db = self.storage.db
        with db._get_conn() as conn:
            conn.execute("INSERT INTO hosts(id,name,hostname) VALUES ('lost','lost','http://lost')")
        self.assertIsNone(db.chats.get_host('lost'))
        with self.assertRaises(ZeroDivisionError), db._get_conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute("INSERT INTO hosts(id,name,hostname) VALUES ('lost','lost','http://lost')")
            1 / 0
        with db._get_conn() as conn:
            self.assertFalse(conn.in_transaction)
        self.assertIsNone(db.chats.get_host('lost'))

    def test_closing_releases_connections_and_later_use_still_works(self):
        db = self.storage.db
        with db._get_conn() as conn:
            pass
        with db._get_conn() as busy:
            db.close()
            # A connection in use is closed when its work ends.
            self.assertEqual(busy.execute('SELECT 1').fetchone()[0], 1)
        for closed in (conn, busy):
            with self.assertRaises(sqlite3.ProgrammingError):
                closed.execute('SELECT 1')
        self.assertTrue(db.chats.get_all_hosts())
        with db._get_conn() as transient:
            pass
        with self.assertRaises(sqlite3.ProgrammingError):
            transient.execute('SELECT 1')


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
