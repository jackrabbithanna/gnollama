import copy
import io
import json
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from gi.repository import Gdk, Gio
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from src import ollama, session
from src.database import DatabaseManager
from src.knowledge import (DEFAULT_RAG, augmented_messages, chunk_text, extract_document,
                           make_document, new_config, vector_blob)
from src.storage import ChatStorage
from src.tab import GenerationTab
from src.widgets.knowledge_view import ChunkPicker, IndexDialog, SourcePicker, SourcesView
from test_transport import Server
import test_ui
from test_ui import pump_until


TAG = {'name': 'embed:latest', 'digest': 'digest-one'}


def indexed(storage, id='index', text='Alpha reference.\n\nBeta reference.', vectors=None):
    if not any(h['hostname'] == 'http://embed:11434' for h in storage.get_all_hosts()):
        storage.add_host('Embedding host', 'http://embed:11434')
    document = make_document('Reference ' + id, text, pages=[dict(page=1, start=0, end=len(text))])
    config = new_config(TAG['name'], TAG['digest'])
    storage.db.add_knowledge_document(document)
    storage.db.add_embedding_config(config)
    index = dict(id=id, document_id=document['id'], config_id=config['id'], host='http://embed:11434', model=TAG['name'],
                 chunk_size=1600, overlap=200, created_at=time.time())
    storage.db.begin_knowledge_index(index)
    vectors = vectors or [[1, 0], [0, 1]]
    middle = len(text) // len(vectors)
    storage.db.save_embedding_batch(id, config['id'], len(vectors[0]), [
        dict(id=id + str(n), ordinal=n, start=n * middle, end=(n+1)*middle if n+1 < len(vectors) else len(text), vector=vector_blob(v))
        for n, v in enumerate(vectors)])
    storage.db.finish_knowledge_index(id, 'complete')
    config = storage.db.embedding_config(config['id'])
    options = dict(copy.deepcopy(DEFAULT_RAG), enabled=True, config_id=config['id'], host=index['host'],
                   model=TAG['name'], selection={id: None})
    return document, config, index, options


class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        ollama.resume()
        self.temp = tempfile.TemporaryDirectory()
        self.storage = ChatStorage(self.temp.name)
        self.storage.add_host('Embedding host', 'http://embed:11434')

    def tearDown(self):
        self.storage.knowledge.cancel_all()
        pump_until(lambda: self.storage.knowledge.idle and self.storage.writer.idle)
        self.storage.knowledge.shutdown()
        self.storage.writer.shutdown()
        self.temp.cleanup()

    def test_embedding_transport_validates_response_and_cancels(self):
        fixture = Server('json')
        self.addCleanup(fixture.close)
        fixture.server.payload = {'embeddings': [[1., 0.], [0., 1.]], 'model': TAG['name']}
        response = ollama.embed(fixture.host, TAG['name'], ['one', 'two'], dimensions=2, keep_alive=0)
        self.assertEqual(len(response['embeddings']), 2)
        path, payload = fixture.server.received[-1]
        self.assertEqual(path, '/api/embed')
        self.assertEqual(payload, dict(model=TAG['name'], input=['one', 'two'], dimensions=2, keep_alive=0, truncate=False))
        for vectors in ([], [[1, 0]], [[0, 0], [1, 0]], [[1, 0], [1, 2, 3]], [[float('nan'), 0], [1, 0]], [[True, 1], [1, 0]]):
            fixture.server.payload = {'embeddings': vectors}
            with self.assertRaises(ollama.OllamaError):
                ollama.embed(fixture.host, TAG['name'], ['one', 'two'])
        cancel = Gio.Cancellable()
        cancel.cancel()
        with self.assertRaises(ollama.RequestCancelled):
            ollama.embed(fixture.host, TAG['name'], 'one', cancellable=cancel)
        fixture.server.mode = 'error'
        with self.assertRaisesRegex(ollama.OllamaError, 'pull this model'):
            ollama.embed(fixture.host, TAG['name'], 'one')

    def test_extraction_pdf_pages_unicode_and_rejections(self):
        text = 'Café 🌻\r\nsource'
        doc, notices = extract_document('source.py', text.encode())
        self.assertEqual(doc['text'], 'Café 🌻\nsource')
        self.assertEqual(notices, [])
        writer = PdfWriter()
        page = writer.add_blank_page(300, 300)
        font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
        content = DecodedStreamObject()
        content.set_data(b'BT /F1 12 Tf 30 250 Td (A searchable PDF sentence.) Tj ET')
        page[NameObject('/Contents')] = writer._add_object(content)
        writer.add_blank_page(300, 300)
        output = io.BytesIO()
        writer.write(output)
        doc, notices = extract_document('reference.pdf', output.getvalue())
        self.assertIn('searchable PDF', doc['text'])
        self.assertEqual([p['page'] for p in doc['pages']], [1, 2])
        self.assertEqual(len(notices), 1)
        writer.encrypt('secret')
        output = io.BytesIO()
        writer.write(output)
        with self.assertRaisesRegex(ValueError, 'Password'):
            extract_document('encrypted.pdf', output.getvalue())
        for raw in (b'\x00binary', b'\xff', b'   '):
            with self.assertRaises(ValueError):
                extract_document('bad.txt', raw)

    def test_chunks_preserve_coverage_offsets_and_overlap(self):
        text = ('First paragraph with Unicode 🌻.\n\nSecond paragraph.\n' * 70) + 'tail'
        chunks = list(chunk_text(text, 160, 20))
        covered = set()
        for chunk in chunks:
            self.assertEqual(chunk['text'], text[chunk['start']:chunk['end']])
            self.assertLessEqual(len(chunk['text']), 160)
            covered.update(range(chunk['start'], chunk['end']))
        self.assertEqual(covered, set(range(len(text))))
        self.assertTrue(all(a['end'] - b['start'] == 20 for a, b in zip(chunks, chunks[1:])))
        with self.assertRaises(ValueError):
            list(chunk_text(text, 160, 160))

    def test_ranking_selection_isolation_budget_and_missing_sources(self):
        document, config, index, options = indexed(self.storage)
        indexed(self.storage, 'other', 'Another document, not selected.', [[1, 0]])
        search = lambda selection, **kwargs: self.storage.db.search_knowledge(config['id'], selection, [1, 0], **kwargs)
        hits = search(options['selection'])
        self.assertEqual(hits[0]['id'], 'index0')
        self.assertEqual(len(hits), 2)
        self.assertTrue(all(h['document_id'] == document['id'] for h in hits))
        self.assertEqual(search({'index': ['index1']})[0]['id'], 'index1')
        self.assertEqual(search(options['selection'], minimum=.9)[0]['id'], 'index0')
        short = search(options['selection'], budget=5)
        self.assertEqual(short[0]['text'], document['text'][:5])
        self.assertTrue(short[0]['truncated'])
        self.assertEqual(short[0]['pages'], [1])
        self.assertEqual(search({'index': ['index1']}, minimum=.9), [])
        for selection in ({}, {'index': ['deleted']}, {'index': []}, {'missing': None}):
            with self.assertRaises(ValueError):
                search(selection)
        with self.assertRaises(ValueError):
            self.storage.db.search_knowledge('different-config', options['selection'], [1, 0])
        self.storage.db.finish_knowledge_index('index', 'failed')
        with self.assertRaises(ValueError):
            search(options['selection'])

    def test_model_changes_and_snapshot_survive_cleanup(self):
        doc, config, index, options = indexed(self.storage)
        with patch.object(ollama, 'fetch_model_details', return_value=[TAG]), patch.object(ollama, 'embed', return_value={'embeddings': [[1, 0]]}):
            snapshot = self.storage.knowledge.retrieve(options, 'Question', Gio.Cancellable())
        with patch.object(ollama, 'fetch_model_details', return_value=[dict(TAG, digest='changed')]), patch.object(ollama, 'embed') as embed:
            with self.assertRaisesRegex(ValueError, 'changed'):
                self.storage.knowledge.retrieve(options, 'Question', Gio.Cancellable())
            embed.assert_not_called()
        with patch.object(ollama, 'fetch_model_details', side_effect=[[TAG], [dict(TAG, digest='changed')]]), patch.object(ollama, 'embed', return_value={'embeddings': [[1, 0]]}):
            with self.assertRaisesRegex(ValueError, 'changed'):
                self.storage.knowledge.retrieve(options, 'Question', Gio.Cancellable())
        self.storage.db.create_chat('chat', 'Sources', 0, 0, 'model')
        self.storage.db.save_messages('chat', [{'role': 'assistant', 'content': 'Answer [S1]', 'response_metadata': {'retrieval': snapshot}}])
        self.assertEqual(self.storage.db.model_embedding_usage(TAG['digest'])['vectors'], 2)
        self.storage.db.delete_model_embeddings(TAG['digest'])
        self.assertIsNotNone(self.storage.db.knowledge_document(doc['id']))
        self.assertEqual(self.storage.db.knowledge_indexes(), [])
        self.storage.db.delete_knowledge_document(doc['id'])
        saved = self.storage.db.get_messages('chat')[0]['response_metadata']['retrieval']
        self.assertEqual(saved, snapshot)

    def test_index_queue_splits_oversized_input_and_retries_interruption(self):
        document = make_document('Long', 'searchable source ' * 80)
        self.storage.db.add_knowledge_document(document)
        config = new_config(TAG['name'], TAG['digest'], 'nomic')
        seen = []
        def embed(host, model, input, **kwargs):
            inputs = [input] if isinstance(input, str) else input
            seen.extend(inputs)
            if any(len(t) > 400 for t in inputs):
                raise ollama.OllamaError('input length exceeds context length')
            return {'embeddings': [[1, 0] for t in inputs]}
        with patch.object(ollama, 'fetch_model_details', return_value=[TAG]), patch.object(ollama, 'embed', side_effect=embed):
            job = self.storage.knowledge.create_index(document['id'], 'http://embed:11434', TAG['name'], config)
            pump_until(lambda: job['done'] and self.storage.writer.idle, timeout=8)
        self.assertIsNone(job['error'])
        index = self.storage.db.knowledge_indexes()[0]
        self.assertEqual(index['status'], 'complete')
        self.assertGreater(index['chunks'], 1)
        self.assertTrue(all(t.startswith('search_document: ') for t in seen))
        started, release = threading.Event(), threading.Event()
        def stalled(*args, **kwargs):
            started.set()
            release.wait(3)
            if kwargs['cancellable'].is_cancelled():
                raise ollama.RequestCancelled('stopped')
            return {'embeddings': [[1, 0]]}
        with patch.object(ollama, 'fetch_model_details', return_value=[TAG]), patch.object(ollama, 'embed', side_effect=stalled):
            second = self.storage.knowledge.create_index(document['id'], 'http://embed:11434', TAG['name'], config)
            pump_until(started.is_set)
            self.assertTrue(self.storage.knowledge.busy('http://embed:11434', TAG['name']))
            second['cancel'].cancel()
            release.set()
            pump_until(lambda: second['done'] and self.storage.writer.idle)
        interrupted = next(i for i in self.storage.db.knowledge_indexes() if i['id'] == second['index_id'])
        self.assertEqual(interrupted['status'], 'interrupted')
        self.assertEqual(next(i for i in self.storage.db.knowledge_indexes() if i['id'] == index['id'])['status'], 'complete')
        with patch.object(ollama, 'fetch_model_details', return_value=[TAG]), patch.object(ollama, 'embed', side_effect=embed):
            retry = self.storage.knowledge.create_index(document['id'], interrupted['host'], TAG['name'], config, index_id=interrupted['id'])
            pump_until(lambda: retry['done'] and self.storage.writer.idle, timeout=8)
        self.assertIsNone(retry['error'])
        self.assertEqual(len(self.storage.db.knowledge_indexes()), 2)

    def test_v5_migration_and_empty_chat_selection(self):
        path = self.temp.name + '/v5.db'
        db = DatabaseManager(path)
        db.create_chat('old', 'Old', 0, 0, 'chat')
        db.save_messages('old', [{'role': 'assistant', 'content': 'saved'}])
        with db._get_conn() as conn:
            for table in ('knowledge_web_sources', 'knowledge_collection_documents', 'knowledge_collections', 'knowledge_chunks', 'knowledge_indexes', 'embedding_configs', 'knowledge_documents'):
                conn.execute('DROP TABLE ' + table)
            conn.execute('ALTER TABLE hosts DROP COLUMN provider')
            conn.execute('ALTER TABLE hosts DROP COLUMN credential_id')
            from legacy_schema import remove_workspace
            remove_workspace(conn)
            conn.execute('PRAGMA user_version=5')
            conn.commit()
        migrated = DatabaseManager(path)
        self.assertEqual(migrated.get_messages('old')[0]['content'], 'saved')
        self.assertEqual(migrated.knowledge_documents(), [])
        migrated.create_chat('empty', 'New Chat', 0, 0, '')
        migrated.save_tool_state('empty', options={'knowledge': {'selection': {'index': None}}})
        migrated.cleanup_empty_chats()
        self.assertIsNotNone(migrated.get_chat('empty'))

    def test_index_write_failure_waits_for_retry_and_digest_change_never_publishes(self):
        document = make_document('Retryable', 'Saved source text for indexing.')
        self.storage.db.add_knowledge_document(document)
        config = new_config(TAG['name'], TAG['digest'])
        original = self.storage.db.save_embedding_batch
        fail = [True]
        def write(*args):
            if fail[0]:
                raise OSError('disk full')
            return original(*args)
        with patch.object(ollama, 'fetch_model_details', return_value=[TAG]), patch.object(ollama, 'embed', return_value={'embeddings': [[1, 0]]}) as embed, patch.object(self.storage.db, 'save_embedding_batch', side_effect=write):
            job = self.storage.knowledge.create_index(document['id'], 'http://embed:11434', TAG['name'], config)
            pump_until(lambda: self.storage.writer.error is not None)
            self.assertFalse(job['done'])
            self.assertEqual(self.storage.db.knowledge_indexes()[0]['status'], 'indexing')
            fail[0] = False
            self.storage.writer.retry()
            pump_until(lambda: job['done'] and self.storage.writer.idle)
        self.assertIsNone(job['error'])
        self.assertEqual(embed.call_count, 1)
        self.assertEqual(self.storage.db.knowledge_indexes()[0]['chunks'], 1)
        with patch.object(ollama, 'fetch_model_details', side_effect=[[TAG], [dict(TAG, digest='replacement')]]), patch.object(ollama, 'embed', return_value={'embeddings': [[1, 0]]}):
            changed = self.storage.knowledge.create_index(document['id'], 'http://embed:11434', TAG['name'], config)
            pump_until(lambda: changed['done'] and self.storage.writer.idle)
        self.assertIn('changed', changed['error'])
        statuses = {i['id']: i['status'] for i in self.storage.db.knowledge_indexes()}
        self.assertEqual(statuses[job['index_id']], 'complete')
        self.assertEqual(statuses[changed['index_id']], 'failed')


@unittest.skipUnless(Gdk.Display.get_default(), 'GTK tests require a display')
class KnowledgeUITests(unittest.TestCase):
    setUp = test_ui.UITests.setUp
    make_tab = test_ui.UITests.make_tab
    make_window = test_ui.UITests.make_window

    def tearDown(self):
        self.storage.knowledge.cancel_all()
        pump_until(lambda: self.storage.knowledge.idle)
        self.storage.knowledge.shutdown()
        test_ui.UITests.tearDown(self)

    def test_rag_chat_json_tools_and_restored_continuation(self):
        doc, config, index, options = indexed(self.storage)
        collection = dict(id='tool-sources', name='Tool sources', config_id=config['id'], host=index['host'],
                          model=index['model'], chunk_size=1600, overlap=200, created_at=0, updated_at=0)
        self.storage.db.create_knowledge_collection(collection)
        self.storage.db.prepare_collection_indexes(collection['id'], [doc['id']])
        options.update(selection={}, collection_ids=[collection['id']])
        tab = self.make_tab()
        tab.knowledge_control.load(options)
        tab.knowledge_control.query.set_text('standalone query')
        tab.chat_input.entry.set_text('What about that?')
        tab.options_panel.system_prompt_entry.set_text('My system instructions.')
        tab.options_panel.output_dropdown.set_selected(2)
        tab.options_panel.schema_text = '{"type":"object"}'
        calls = [{'function': {'name': 'lookup', 'arguments': {}}}]
        payloads = []
        def chat(**kwargs):
            payloads.append(kwargs)
            return iter([{'message': {'tool_calls': calls}, 'done': True}])
        with patch.object(ollama, 'fetch_model_details', return_value=[TAG]), patch.object(ollama, 'embed', return_value={'embeddings': [[1, 0]]}) as embed, patch.object(ollama, 'chat', side_effect=chat):
            tab.on_send_clicked()
            pump_until(lambda: tab.request is None and not tab._tool_busy and self.storage.writer.idle)
        self.assertEqual(embed.call_args.args[2], 'standalone query')
        self.assertEqual(payloads[0]['host'], 'http://localhost:11434')
        self.assertEqual(payloads[0]['format'], {'type': 'object'})
        self.assertTrue(payloads[0]['messages'][0]['content'].startswith('My system instructions.'))
        self.assertIn('<reference_passages>', payloads[0]['messages'][-1]['content'])
        self.assertEqual(tab.strategy.history[0]['content'], 'What about that?')
        self.assertEqual(tab.knowledge_control.query.get_text(), '')
        snapshot = tab.strategy.history[-1]['response_metadata']['retrieval']
        self.assertEqual(snapshot['collections'][0]['id'], collection['id'])
        tab._save_tool_result(tab.strategy.pending_round, 0, 'result')
        pump_until(lambda: not tab._tool_busy and self.storage.writer.idle)
        saved = self.storage.get_chat(tab.strategy.chat_id)
        self.storage.db.delete_knowledge_document(doc['id'])
        restored = GenerationTab(mode='chat', storage=self.storage, chat_id=saved['id'], initial_history=saved['messages'])
        self.tabs.append(restored)
        pump_until(lambda: session.worker.idle)
        with patch.object(ollama, 'embed') as embed, patch.object(ollama, 'chat', return_value=iter([{'message': {'content': '{}'}, 'done': True}])) as chat:
            restored.on_send_clicked(continuation=True)
            pump_until(lambda: restored.request is None and not restored._tool_busy and self.storage.writer.idle)
        embed.assert_not_called()
        self.assertIn('<reference_passages>', next(m['content'] for m in chat.call_args.kwargs['messages'] if m['role'] == 'user'))
        self.assertEqual(restored.strategy.history[-1]['response_metadata']['retrieval'], snapshot)
        self.assertEqual(restored.strategy.history[-1]['response_metadata']['validation']['status'], 'valid')

    def test_retrieval_failure_stop_and_explicit_bypass_preserve_draft(self):
        doc, config, index, options = indexed(self.storage)
        window, tab = self.make_window()
        tab.knowledge_control.load(options)
        tab.chat_input.entry.set_text('Keep this prompt')
        with patch.object(ollama, 'fetch_model_details', return_value=[]), patch.object(ollama, 'chat') as chat:
            tab.on_send_clicked()
            pump_until(lambda: tab.request is None)
        chat.assert_not_called()
        self.assertEqual(tab.chat_input.entry.get_text(), 'Keep this prompt')
        self.assertEqual(tab.strategy.history, [])
        with patch.object(ollama, 'chat', return_value=iter([{'message': {'content': 'answer'}, 'done': True}])) as chat:
            tab._retrieval_dialog.emit('response', 'without')
            pump_until(lambda: tab.request is None and self.storage.writer.idle)
        self.assertNotIn('<reference_passages>', chat.call_args.kwargs['messages'][-1]['content'])
        self.assertTrue(tab.knowledge_control.options['enabled'])
        tab.chat_input.entry.set_text('Stop this query')
        started, release = threading.Event(), threading.Event()
        def retrieve(*args):
            started.set()
            release.wait(3)
            raise ollama.RequestCancelled('stopped')
        with patch.object(self.storage.knowledge, 'retrieve', side_effect=retrieve), patch.object(ollama, 'chat') as chat:
            tab.on_send_clicked()
            pump_until(started.is_set)
            tab.on_send_or_stop()
            release.set()
            pump_until(lambda: tab.request is None)
        chat.assert_not_called()
        self.assertEqual(tab.chat_input.entry.get_text(), 'Stop this query')

    def test_library_dialogs_select_chunks_validate_hosts_and_search(self):
        doc, config, index, options = indexed(self.storage)
        self.storage.db.create_knowledge_collection(dict(id='sources', name='Sources', config_id=config['id'],
            host=index['host'], model=index['model'], chunk_size=1600, overlap=200, created_at=0, updated_at=0))
        self.storage.db.prepare_collection_indexes('sources', [doc['id']])
        options.update(selection={}, collection_ids=['sources'])
        window, tab = self.make_window()
        with patch.object(ollama, 'fetch_model_details', return_value=[TAG]), patch.object(ollama, 'show_model', return_value={'capabilities': ['embedding']}):
            picker = SourcePicker(self.storage, options, lambda value: None)
            picker.present(window)
            pump_until(lambda: session.worker.idle)
            current = picker.current_options()
            self.assertEqual(current['selection'], {})
            self.assertEqual(current['collection_ids'], ['sources'])
            index_row = self.storage.db.knowledge_indexes()[0]
            selected = []
            chunks = ChunkPicker(self.storage, index_row, [], selected.append)
            chunks.present(picker)
            chunks._all(True)
            chunks._apply()
            self.assertEqual(selected, [None])
            picker.query.set_text('Alpha')
            with patch.object(ollama, 'embed', return_value={'embeddings': [[1, 0]]}):
                picker._search()
                pump_until(lambda: picker.search_button.get_sensitive() and session.worker.idle)
            self.assertIsInstance(picker.results.get_first_child(), SourcesView)
            picker.host_models.models[0] = dict(TAG, digest='wrong')
            with self.assertRaisesRegex(ValueError, 'digest'):
                picker.current_options()
            picker.close()
            creator = IndexDialog(self.storage, doc, lambda: None)
            creator.present(window)
            pump_until(lambda: session.worker.idle)
            creator.preset.set_selected(1)
            self.assertEqual(creator.query_prefix.get_text(), 'task: search result | query: ')
            creator.host_models.models = [dict(TAG, name='qwen3-embedding:0.6b')]
            creator._model_changed()
            self.assertEqual(creator.preset.get_selected(), 3)
            self.assertEqual(creator.document_prefix.get_text(), '')
            self.assertTrue(creator.query_prefix.get_text().endswith('\nQuery:'))
            creator.overlap.set_value(2000)
            creator._start(lambda: None)
            self.assertTrue(creator.error.get_visible())
            creator.close()

    def test_rag_waits_for_saved_sources_and_close_cancels_before_chat(self):
        doc, config, index, options = indexed(self.storage)
        tab = self.make_tab()
        tab.knowledge_control.load(options)
        tab.chat_input.entry.set_text('Save sources before sending')
        blocked, release = threading.Event(), threading.Event()
        def block():
            blocked.set()
            release.wait(3)
        self.storage._submit(block)
        pump_until(blocked.is_set)
        with patch.object(ollama, 'fetch_model_details', return_value=[TAG]), patch.object(ollama, 'embed', return_value={'embeddings': [[1, 0]]}), patch.object(ollama, 'chat') as chat:
            tab.on_send_clicked()
            pump_until(lambda: tab.request is not None and tab.request.retrieval is not None and tab.chat_input.entry.get_text() == '')
            chat.assert_not_called()
            closed = []
            tab.close_session(lambda: closed.append(True))
            release.set()
            pump_until(lambda: closed and self.storage.writer.idle)
        chat.assert_not_called()
        saved = self.storage.get_chat(tab.strategy.chat_id)
        self.assertIn('retrieval', saved['messages'][0]['response_metadata'])
        self.assertEqual(saved['messages'][-1]['response_metadata']['status'], 'stopped')

    def test_model_delete_keeps_data_by_default_and_cleans_only_on_success(self):
        from src.model_manager import ModelManagerDialog
        doc, config, index, options = indexed(self.storage)
        self.stack.enter_context(patch.object(ollama, 'fetch_model_details', return_value=[]))
        self.stack.enter_context(patch('src.model_manager.Adw.AlertDialog.close'))
        manager = ModelManagerDialog(self.storage)
        dialogs = []
        with patch('src.model_manager.Adw.AlertDialog.present', lambda d, parent: dialogs.append(d)), patch.object(ollama, 'delete_model', return_value=True):
            manager.on_model_delete_clicked(None, TAG)
            dialog = dialogs[-1]
            self.assertIn('2 vectors', dialog.get_body())
            self.assertFalse(dialog.get_extra_child().get_active())
            dialog.emit('response', 'delete')
            pump_until(lambda: session.worker.idle and self.storage.writer.idle)
        self.assertEqual(self.storage.db.model_embedding_usage(TAG['digest'])['vectors'], 2)
        with patch('src.model_manager.Adw.AlertDialog.present', lambda d, parent: dialogs.append(d)), patch.object(ollama, 'delete_model', side_effect=ollama.OllamaError('offline')):
            manager.on_model_delete_clicked(None, TAG)
            dialog = dialogs[-1]
            dialog.get_extra_child().set_active(True)
            dialog.emit('response', 'delete')
            pump_until(lambda: session.worker.idle and self.storage.writer.idle)
        self.assertEqual(self.storage.db.model_embedding_usage(TAG['digest'])['vectors'], 2)
        with patch('src.model_manager.Adw.AlertDialog.present', lambda d, parent: dialogs.append(d)), patch.object(ollama, 'delete_model', return_value=True):
            manager.on_model_delete_clicked(None, TAG)
            dialog = dialogs[-1]
            dialog.get_extra_child().set_active(True)
            dialog.emit('response', 'delete')
            pump_until(lambda: session.worker.idle and self.storage.writer.idle)
        self.assertEqual(self.storage.db.model_embedding_usage(TAG['digest'])['vectors'], 0)
        self.assertIsNotNone(self.storage.db.knowledge_document(doc['id']))
        manager.close()
