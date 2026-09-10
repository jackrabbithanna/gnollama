"""Collection membership, shared builds, live source resolution, and upgrades."""
import copy
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from gi.repository import Adw, Gdk, Gio, Gtk
from src import database, ollama, session
from src.database import DatabaseManager, DatabaseUpgradeError
from src.knowledge import DEFAULT_RAG, make_document, new_config
from src.storage import ChatStorage
from src.widgets.knowledge_view import CollectionDialog, DocumentPicker, KnowledgeControl, KnowledgeView, SourcePicker
from test_knowledge import TAG, indexed
from test_ui import pump_until
from test_vectors import add_index


def collection(db, name, config, size=1600, overlap=200):
    db.add_embedding_config(config)
    value = dict(id=name, name=name, config_id=config['id'], host='http://embed:11434', model=TAG['name'],
                 chunk_size=size, overlap=overlap, created_at=time.time(), updated_at=time.time())
    db.create_knowledge_collection(value)
    return value


class CollectionTests(unittest.TestCase):
    def setUp(self):
        ollama.resume()
        self.temp = tempfile.TemporaryDirectory()
        self.storage = ChatStorage(self.temp.name)
        self.storage.add_host('Embedding host', 'http://embed:11434')
        self.config = new_config(TAG['name'], TAG['digest'])
        self.patches = [patch.object(ollama, 'fetch_model_details', return_value=[TAG]),
                        patch.object(ollama, 'fetch_models', return_value=['chat']),
                        patch.object(ollama, 'show_model', return_value={'capabilities': ['embedding', 'completion']}),
                        patch.object(ollama, 'embed', side_effect=lambda host, model, inputs, **kw:
                                     {'embeddings': [[1., 0.] for _ in ([inputs] if isinstance(inputs, str) else inputs)]})]
        for p in self.patches:
            p.start()
        self.widgets = []

    def tearDown(self):
        for widget in self.widgets:
            if isinstance(widget, KnowledgeView):
                widget._destroyed()
            elif isinstance(widget, KnowledgeControl):
                widget.close_dialog(dispose=True)
            elif isinstance(widget, SourcePicker):
                widget._disconnect_sources()
                widget.host_models.stop()
                widget._closed()
            elif isinstance(widget, CollectionDialog):
                widget.host_models.stop()
                widget._closed()
        self.storage.knowledge.cancel_all()
        pump_until(lambda: self.storage.knowledge.idle and self.storage.writer.idle and session.worker.idle, timeout=10)
        self.storage.knowledge.shutdown()
        self.storage.writer.shutdown()
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    def document(self, title='Reference'):
        doc = make_document(title, title + ' contains the information for this collection.')
        self.storage.db.add_knowledge_document(doc)
        return doc

    def drain(self):
        pump_until(lambda: self.storage.knowledge.idle and self.storage.writer.idle, timeout=10)

    def options(self, *ids):
        return dict(copy.deepcopy(DEFAULT_RAG), enabled=True, config_id=self.config['id'],
                    host='http://embed:11434', model=TAG['name'], collection_ids=list(ids))

    def test_membership_reuses_newest_complete_and_deletion_preserves_data(self):
        doc, config, index, options = indexed(self.storage)
        for name in ('Manuals', 'Project'):
            collection(self.storage.db, name, config)
            self.assertEqual(self.storage.db.prepare_collection_indexes(name, [doc['id'], doc['id']]), [])
            members = self.storage.db.collection_documents(name)
            self.assertEqual(len(members), 1)
            self.assertEqual(members[0]['index_id'], index['id'])
        self.assertEqual(self.storage.db.knowledge_collections()[0]['ready'], 1)
        self.storage.db.remove_collection_document('Manuals', doc['id'])
        self.storage.db.delete_knowledge_collection('Project')
        self.assertEqual(len(self.storage.db.knowledge_indexes()), 1)
        self.assertIsNotNone(self.storage.db.knowledge_vector('index0'))
        self.assertIn(doc['id'], self.storage.db.ungrouped_document_ids())

    def test_reuse_prefers_newest_complete_and_requires_exact_chunk_settings(self):
        doc, config, index, options = indexed(self.storage)
        newer = dict(index, id='newer', created_at=index['created_at'] + 1)
        self.storage.db.begin_knowledge_index(newer)
        raw = self.storage.db.knowledge_vector('index0')
        self.storage.db.save_embedding_batch('newer', config['id'], 2,
            [dict(id='newer-chunk', ordinal=0, start=0, end=10, vector=raw)])
        self.storage.db.finish_knowledge_index('newer', 'complete')
        collection(self.storage.db, 'Latest', config)
        self.assertEqual(self.storage.db.prepare_collection_indexes('Latest', [doc['id']]), [])
        self.assertEqual(self.storage.db.collection_documents('Latest')[0]['index_id'], 'newer')
        collection(self.storage.db, 'Smaller', config, size=128, overlap=16)
        jobs = self.storage.db.prepare_collection_indexes('Smaller', [doc['id']])
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]['chunk_size'], 128)

    def test_configuration_and_membership_constraints_are_enforced(self):
        doc, config, index, options = indexed(self.storage)
        collection(self.storage.db, 'Manuals', config, size=128, overlap=16)
        with self.storage.db._get_conn() as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO knowledge_collection_documents VALUES ('Manuals', ?, 'index')", (doc['id'],))
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE knowledge_collections SET chunk_size=256 WHERE id='Manuals'")
        with self.assertRaises(ValueError):
            self.storage.db.update_collection_endpoint('Manuals', 'http://elsewhere', TAG['name'], 'wrong-digest')
        self.storage.db.update_collection_endpoint('Manuals', 'http://elsewhere', 'alias', TAG['digest'])
        self.assertEqual(self.storage.db.knowledge_collection('Manuals')['model'], 'alias')

    def test_automatic_builds_share_pending_work_and_copy_keeps_original(self):
        doc = self.document()
        a = self.storage.knowledge.create_collection('A', 'http://embed:11434', TAG['name'], self.config, document_ids=[doc['id']])
        b = self.storage.knowledge.create_collection('B', 'http://embed:11434', TAG['name'], self.config, document_ids=[doc['id']])
        self.drain()
        self.assertIsNone(a['error'])
        self.assertIsNone(b['error'])
        members = [self.storage.db.collection_documents(j['collection_id'])[0] for j in (a, b)]
        self.assertEqual(members[0]['index_id'], members[1]['index_id'])
        self.assertEqual(len(self.storage.db.knowledge_indexes()), 1)
        new_config_value = new_config(TAG['name'], TAG['digest'], 'custom', query_prefix='Find: ')
        c = self.storage.knowledge.create_collection('New settings', 'http://embed:11434', TAG['name'], new_config_value,
                                                      document_ids=[doc['id']])
        self.drain()
        self.assertIsNone(c['error'])
        self.assertEqual(len(self.storage.db.knowledge_indexes()), 2)
        self.assertEqual(self.storage.db.collection_documents(a['collection_id'])[0]['index_id'], members[0]['index_id'])

    def test_failed_and_cancelled_builds_retry_without_losing_membership(self):
        doc = self.document()
        collection(self.storage.db, 'Retry', self.config)
        with patch.object(ollama, 'embed', side_effect=ollama.OllamaError('Test failure')):
            self.storage.knowledge.build_collection('Retry', [doc['id']])
            self.drain()
        self.assertEqual(self.storage.db.collection_documents('Retry')[0]['status'], 'failed')
        entered, release = threading.Event(), threading.Event()
        def held(*args, **kwargs):
            entered.set()
            release.wait(5)
            return {'embeddings': [[1., 0.]]}
        with patch.object(ollama, 'embed', side_effect=held):
            self.storage.knowledge.build_collection('Retry')
            pump_until(entered.is_set)
            job = next(j for j in self.storage.knowledge.jobs.values() if not j['done'] and j.get('index_id'))
            job['cancel'].cancel()
            release.set()
            self.drain()
        self.assertEqual(self.storage.db.collection_documents('Retry')[0]['status'], 'interrupted')
        self.storage.knowledge.build_collection('Retry')
        self.drain()
        self.assertEqual(self.storage.db.collection_documents('Retry')[0]['status'], 'complete')
        self.assertEqual(len(self.storage.db.knowledge_indexes()), 1)

    def test_deleted_collection_during_queued_preparation_does_not_pause_writer(self):
        collection(self.storage.db, 'Gone', self.config)
        entered, release = threading.Event(), threading.Event()
        self.storage.knowledge.submit('Hold queue', lambda *args: entered.set() or release.wait(5), preparation=True)
        pump_until(entered.is_set)
        job = self.storage.knowledge.build_collection('Gone', [self.document()['id']])
        self.storage.db.delete_knowledge_collection('Gone')
        release.set()
        self.drain()
        self.assertIn('deleted', str(job['error']))
        self.assertIsNone(self.storage.writer.error)
        self.assertEqual(self.storage.db.knowledge_indexes(), [])

    def test_deleted_copy_member_does_not_leave_a_half_created_collection(self):
        doc = self.document()
        job = self.storage.knowledge.create_collection('Copy', 'http://embed:11434', TAG['name'], self.config,
                                                       document_ids=[doc['id'], 'deleted-document'])
        self.drain()
        self.assertIn('deleted', str(job['error']))
        self.assertIsNone(self.storage.db.knowledge_collection(job['collection_id']))
        self.assertIsNone(self.storage.writer.error)

    def test_deleted_reserved_index_is_not_recreated_by_delayed_job(self):
        doc = self.document()
        collection(self.storage.db, 'Queued', self.config)
        entered, release = threading.Event(), threading.Event()
        self.storage.knowledge.submit('Hold embeddings', lambda *args: entered.set() or release.wait(5))
        pump_until(entered.is_set)
        try:
            prepare = self.storage.knowledge.build_collection('Queued', [doc['id']])
            pump_until(lambda: prepare['done'])
            member = self.storage.db.collection_documents('Queued')[0]
            self.storage.db.delete_knowledge_index(member['index_id'])
        finally:
            release.set()
        self.drain()
        self.assertIsNone(self.storage.db.collection_documents('Queued')[0]['index_id'])
        self.assertEqual(self.storage.db.knowledge_indexes(), [])
        self.assertIsNone(self.storage.writer.error)

    def test_addition_is_visible_while_embeddings_wait_and_reuses_ordinary_queued_build(self):
        doc = self.document()
        collection(self.storage.db, 'Queued', self.config)
        entered, release = threading.Event(), threading.Event()
        self.storage.knowledge.submit('Hold embeddings', lambda *args: entered.set() or release.wait(5))
        pump_until(entered.is_set)
        try:
            ordinary = self.storage.knowledge.create_index(doc['id'], 'http://embed:11434', TAG['name'], self.config)
            prepare = self.storage.knowledge.build_collection('Queued', [doc['id']])
            pump_until(lambda: prepare['done'])
            self.assertIsNone(prepare['error'])
            member = self.storage.db.collection_documents('Queued')[0]
            self.assertEqual(member['index_id'], ordinary['index_id'])
            self.assertEqual(member['status'], 'indexing')
            with self.assertRaisesRegex(ValueError, 'not ready'):
                self.storage.knowledge.retrieve(self.options('Queued'), 'Question', Gio.Cancellable())
        finally:
            release.set()
        self.drain()
        self.assertEqual(len(self.storage.db.knowledge_indexes()), 1)
        self.assertEqual(self.storage.db.knowledge_collections()[0]['ready'], 1)

    def test_cancelled_queued_collection_build_is_interrupted_and_retryable(self):
        doc = self.document()
        collection(self.storage.db, 'Queued', self.config)
        entered, release = threading.Event(), threading.Event()
        self.storage.knowledge.submit('Hold embeddings', lambda *args: entered.set() or release.wait(5))
        pump_until(entered.is_set)
        try:
            prepare = self.storage.knowledge.build_collection('Queued', [doc['id']])
            pump_until(lambda: prepare['done'])
            job = next(j for j in self.storage.knowledge.jobs.values() if j.get('index_id') and not j['done'])
            job['cancel'].cancel()
        finally:
            release.set()
        self.drain()
        self.assertEqual(self.storage.db.collection_documents('Queued')[0]['status'], 'interrupted')
        self.storage.knowledge.build_collection('Queued')
        self.drain()
        self.assertEqual(self.storage.db.knowledge_collections()[0]['ready'], 1)

    def test_shutdown_cancels_bulk_builds_without_leaving_indexing_members(self):
        docs = [self.document('Document %s' % i) for i in range(12)]
        collection(self.storage.db, 'Bulk', self.config)
        entered, release = threading.Event(), threading.Event()
        def held(*args, **kwargs):
            entered.set()
            release.wait(5)
            return {'embeddings': [[1., 0.]]}
        with patch.object(ollama, 'embed', side_effect=held):
            self.storage.knowledge.build_collection('Bulk', [d['id'] for d in docs])
            pump_until(entered.is_set)
            self.storage.knowledge.cancel_all()
            release.set()
            self.drain()
        members = self.storage.db.collection_documents('Bulk')
        self.assertEqual(len(members), len(docs))
        self.assertTrue(all(m['status'] == 'interrupted' for m in members), members)
        with self.assertRaisesRegex(ValueError, 'closing'):
            self.storage.knowledge.build_collection('Bulk')

    def test_collection_union_partial_overlap_and_live_membership_snapshot(self):
        doc, config, index, opts = indexed(self.storage)
        other, _, _, _ = indexed(self.storage, id='other', text='A separate document about another topic.')
        for name in ('A', 'B'):
            collection(self.storage.db, name, config)
            self.storage.db.prepare_collection_indexes(name, [doc['id']])
        options = self.options('A', 'B')
        options['selection'] = {'index': ['index0']}
        snapshot = self.storage.knowledge.retrieve(options, 'Question', Gio.Cancellable())
        self.assertEqual(len(snapshot['hits']), 2)
        self.assertEqual(len({h['id'] for h in snapshot['hits']}), 2)
        self.assertEqual(snapshot['selection'], {'index': None})
        self.assertEqual({c['id'] for c in snapshot['collections']}, {'A', 'B'})
        self.storage.db.create_chat('saved', 'Saved answer', 0, 0, 'chat')
        self.storage.db.save_messages('saved', [{'role': 'assistant', 'content': 'Answer [S1]', 'response_metadata': {'retrieval': snapshot}}])
        original = copy.deepcopy(snapshot)
        self.storage.db.prepare_collection_indexes('A', [other['id']])
        self.storage.db.remove_collection_document('B', doc['id'])
        options['collection_ids'] = ['A']
        updated = self.storage.knowledge.retrieve(options, 'Question', Gio.Cancellable())
        self.assertEqual(len(updated['collections'][0]['members']), 2)
        self.storage.db.rename_knowledge_collection('A', 'Renamed')
        self.assertEqual(self.storage.db.get_messages('saved')[0]['response_metadata']['retrieval'], original)

    def test_empty_incomplete_deleted_and_incompatible_collections_block_search(self):
        doc, config, index, opts = indexed(self.storage)
        collection(self.storage.db, 'A', config)
        for ids, message in [(['A'], 'empty'), (['missing'], 'deleted')]:
            with self.assertRaisesRegex(ValueError, message):
                self.storage.knowledge.retrieve(self.options(*ids), 'Question', Gio.Cancellable())
        self.storage.db.prepare_collection_indexes('A', [doc['id']])
        self.storage.db.prepare_collection_indexes('A', [self.document('Pending')['id']])
        with patch.object(ollama, 'embed') as embed:
            with self.assertRaisesRegex(ValueError, 'Pending'):
                self.storage.knowledge.retrieve(self.options('A'), 'Question', Gio.Cancellable())
            embed.assert_not_called()
        different = new_config(TAG['name'], 'other-digest')
        collection(self.storage.db, 'Different', different)
        with self.assertRaisesRegex(ValueError, 'different embedding'):
            self.storage.knowledge.retrieve(self.options('Different'), 'Question', Gio.Cancellable())

    def test_membership_resolves_in_search_snapshot_during_concurrent_changes(self):
        doc, config, index, opts = indexed(self.storage)
        other, _, _, _ = indexed(self.storage, id='other')
        collection(self.storage.db, 'A', config)
        self.storage.db.prepare_collection_indexes('A', [doc['id']])
        original = self.storage.db.resolve_collection_sources
        def resolve(*args):
            result = original(*args)
            self.storage.db.prepare_collection_indexes('A', [other['id']])
            self.storage.db.remove_collection_document('A', doc['id'])
            return result
        with patch.object(self.storage.db, 'resolve_collection_sources', side_effect=resolve):
            snapshot = {}
            hits = self.storage.db.search_knowledge(config['id'], {}, [1, 0], collection_ids=['A'], source_snapshot=snapshot)
        self.assertEqual({h['document_id'] for h in hits}, {doc['id']})
        self.assertEqual(snapshot['collections'][0]['members'], [{'document_id': doc['id'], 'index_id': index['id']}])
        self.assertEqual(self.storage.db.collection_documents('A')[0]['id'], other['id'])

    def test_deleting_vectors_and_documents_keeps_correct_collection_state(self):
        doc, config, index, opts = indexed(self.storage)
        collection(self.storage.db, 'A', config)
        self.storage.db.prepare_collection_indexes('A', [doc['id']])
        self.storage.db.delete_model_embeddings(config['digest'])
        member = self.storage.db.collection_documents('A')[0]
        self.assertIsNone(member['index_id'])
        self.assertIsNotNone(self.storage.db.knowledge_document(doc['id']))
        self.assertEqual(self.storage.db.knowledge_collections()[0]['ready'], 0)
        self.storage.db.delete_knowledge_document(doc['id'])
        self.assertEqual(self.storage.db.collection_documents('A'), [])
        self.assertIsNotNone(self.storage.db.knowledge_collection('A'))

    @unittest.skipUnless(Gdk.Display.get_default(), 'GTK requires a display')
    def test_library_bulk_picker_and_chat_sources_keep_collection_ids(self):
        doc, config, index, opts = indexed(self.storage)
        collection(self.storage.db, 'Manuals', config)
        view = KnowledgeView(self.storage)
        self.widgets.append(view)
        view.open_collection('Manuals')
        pump_until(lambda: view.detail_page.get_title() == 'Manuals')
        picker = DocumentPicker(self.storage, 'Manuals', view.refresh)
        picker.selected.add(doc['id'])
        with patch.object(picker, 'close'):
            picker._add()
        self.drain()
        pump_until(lambda: 'Manuals' in view._library_choices)
        options = self.options('Manuals')
        source = SourcePicker(self.storage, options, lambda result: None)
        self.widgets.append(source)
        pump_until(lambda: source.host_models.model() is not None)
        chosen = source.current_options()
        self.assertEqual(chosen['collection_ids'], ['Manuals'])
        self.assertEqual(chosen['selection'], {})
        control = KnowledgeControl(self.storage, lambda: None)
        self.widgets.append(control)
        control.load(chosen)
        self.assertIn('1/1 ready', control.notice.get_text())
        chat = self.storage.create_chat()
        self.storage.save_tool_state(chat['id'], options={'knowledge': chosen})
        self.drain()
        restored = self.storage.get_chat(chat['id'])['options']['knowledge']
        self.assertEqual(restored['collection_ids'], ['Manuals'])
        control.load(restored)
        self.storage.db.delete_knowledge_collection('Manuals')
        self.storage.knowledge.changed()
        pump_until(lambda: 'deleted' in control.notice.get_text())
        self.assertEqual(source.collection_ids, {'Manuals'})

    @unittest.skipUnless(Gdk.Display.get_default(), 'GTK requires a display')
    def test_collection_copy_dialog_prefills_settings_and_duplicate_add_keeps_text(self):
        doc, config, index, opts = indexed(self.storage)
        original = collection(self.storage.db, 'Original', config)
        self.storage.db.prepare_collection_indexes('Original', [doc['id']])
        dialog = CollectionDialog(self.storage, lambda id: None, original)
        self.widgets.append(dialog)
        pump_until(lambda: dialog.host_models.model() is not None)
        self.assertEqual(dialog.document_ids, [doc['id']])
        self.assertEqual(dialog.settings()[2]['id'], config['id'])
        collection(self.storage.db, 'Destination', config)
        view = KnowledgeView(self.storage)
        self.widgets.append(view)
        with patch.object(view, 'present_dialog') as present:
            view._save_document(make_document('Duplicate', doc['text']), 'Destination')
            prompt = present.call_args.args[0]
            self.assertTrue(prompt.has_response('add'))
            prompt.emit('response', 'add')
        self.drain()
        self.assertEqual(len(self.storage.db.knowledge_documents()), 1)
        self.assertEqual(self.storage.db.collection_documents('Destination')[0]['index_id'], index['id'])

    @unittest.skipUnless(Gdk.Display.get_default(), 'GTK requires a display')
    def test_collection_only_picker_replaces_legacy_sources_explicitly_and_restores_metric(self):
        doc, config, index, options = indexed(self.storage)
        collection(self.storage.db, 'Manuals', config)
        self.storage.db.prepare_collection_indexes('Manuals', [doc['id']])
        original = copy.deepcopy(options)
        picker = SourcePicker(self.storage, options, lambda result: None)
        self.widgets.append(picker)
        pump_until(lambda: picker.host_models.model() is not None)
        with self.assertRaisesRegex(ValueError, 'Select at least one collection'):
            picker.current_options()
        checks = []
        def visit(widget):
            if isinstance(widget, Gtk.CheckButton):
                checks.append(widget)
            child = widget.get_first_child()
            while child:
                visit(child)
                child = child.get_next_sibling()
        visit(picker.box)
        self.assertEqual(len(checks), 1)
        self.assertIn('Manuals', checks[0].get_child().get_text())
        checks[0].set_active(True)
        pump_until(lambda: picker.host_models.model() is not None)
        picker.threshold.set_text('.6')
        self.assertEqual(picker.current_options()['minimum'], .6)
        picker.metric_dropdown.set_selected(2)
        self.assertEqual(picker.threshold.get_text(), '')
        self.assertEqual(picker.threshold_label.get_text(), 'Maximum distance')
        picker.threshold.set_text('1.25')
        chosen = picker.current_options()
        self.assertEqual(chosen['selection'], {})
        self.assertEqual(chosen['collection_ids'], ['Manuals'])
        self.assertEqual(chosen['metric'], 'manhattan')
        self.assertEqual(chosen['maximum'], 1.25)
        self.assertIsNone(chosen['minimum'])
        self.assertEqual(options, original)
        chat = self.storage.create_chat()
        self.storage.save_tool_state(chat['id'], options={'knowledge': chosen})
        self.drain()
        saved = self.storage.get_chat(chat['id'])['options']['knowledge']
        restored = SourcePicker(self.storage, saved, lambda result: None)
        self.widgets.append(restored)
        self.assertEqual(restored.metric(), 'manhattan')
        self.assertEqual(restored.threshold.get_text(), '1.25')
        snapshot = self.storage.knowledge.retrieve(chosen, 'Question', Gio.Cancellable())
        self.assertEqual(snapshot['metric'], 'manhattan')
        self.assertTrue(all(h['score'] <= 1.25 for h in snapshot['hits']))
        self.storage.db.save_messages(chat['id'], [{'role': 'assistant', 'content': 'Answer', 'response_metadata': {'retrieval': snapshot}}])
        self.assertEqual(self.storage.get_chat(chat['id'])['messages'][0]['response_metadata']['retrieval'], snapshot)


class CollectionMigrationTests(unittest.TestCase):
    def test_v7_upgrade_backs_up_and_keeps_vectors_and_chat_options(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'v7.db'
            with patch.object(database, 'MIGRATIONS', database.MIGRATIONS[:6]):
                old = DatabaseManager(str(path))
            doc, config, chunks = add_index(old)
            old.create_chat('saved', 'Saved chat', 0, 0, 'chat')
            options = {'knowledge': {'config_id': config['id'], 'selection': {'index': None}}}
            old.save_tool_state('saved', options=options)
            upgraded = DatabaseManager(str(path))
            self.assertEqual(upgraded.get_chat('saved')['options'], options)
            self.assertEqual(upgraded.knowledge_vector(chunks[0]['id']), chunks[0]['vector'])
            self.assertEqual(upgraded.knowledge_collections(), [])
            self.assertEqual(upgraded.ungrouped_document_ids(), {doc['id']})
            with sqlite3.connect(upgraded.backup_path) as conn:
                self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 7)
            self.assertIn('.pre-v9-', upgraded.backup_path)
            self.assertIsNone(DatabaseManager(str(path)).backup_path)

    def test_collection_migration_failure_rolls_back_and_retries(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'v7.db'
            with patch.object(database, 'MIGRATIONS', database.MIGRATIONS[:6]):
                old = DatabaseManager(str(path))
            doc, config, chunks = add_index(old)
            def fail(conn, progress=None):
                conn.execute('CREATE TABLE knowledge_collections(id TEXT PRIMARY KEY)')
                raise ValueError('Injected migration failure')
            with patch.object(database, 'MIGRATIONS', database.MIGRATIONS[:6] + [fail]):
                with self.assertRaises(DatabaseUpgradeError) as caught:
                    DatabaseManager(str(path))
            self.assertTrue(Path(caught.exception.backup_path).exists())
            with sqlite3.connect(path) as conn:
                self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 7)
                self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='knowledge_collections'").fetchone())
            upgraded = DatabaseManager(str(path))
            self.assertEqual(upgraded.knowledge_vector(chunks[0]['id']), chunks[0]['vector'])
