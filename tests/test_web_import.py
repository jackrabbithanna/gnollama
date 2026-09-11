import gzip
import io
import sqlite3
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from gi.repository import Gdk, Gio
from src import database, ollama, session
from src.database import DatabaseManager, DatabaseUpgradeError
from src.knowledge import augmented_messages, make_document
from src.web_import import extract_download, extract_html, fetch_url, normalize_url, parse_urls
from src.widgets.url_import import URLImportDialog
from test_knowledge import indexed, TAG
import test_ui
from test_ui import pump_until
from src.widgets.json_view import buffer_text

GUIDE = '''<html><head><title>Vector guide</title></head><body><nav>Global navigation</nav>
<div class="layout"><main><h1>Search your documents</h1><nav>Table of contents</nav>
<p>Store each document as vectors. Use the same embedding model for the query and the indexed documents.
The search returns the most relevant passages, which are supplied to the model alongside your question.</p>
<pre><code>def search(query):
    return query + " result"
</code></pre><table><tr><th>Model</th><th>Dimensions</th></tr><tr><td>Qwen</td><td>1024</td></tr></table>
<div class="notes"><p>Keep this nested note.</p><div><p>Keep this detail too.</p></div></div>
<a href="../reference">Reference</a><div hidden>Hidden text</div><script>run_dangerous_code()</script>
<aside>Sidebar links</aside><section id="comments">User comments</section></main></div><footer>Footer links</footer></body></html>'''


class WebServer:
    def __init__(self):
        self.pages = {}
        self.started = threading.Event()
        self.release = threading.Event()
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.path == '/redirect':
                    self.send_response(302)
                    self.send_header('Location', '/guide')
                    self.end_headers()
                    return
                if self.path in ('/loop', '/unsafe'):
                    self.send_response(302)
                    self.send_header('Location', '/loop' if self.path == '/loop' else 'file:///etc/passwd')
                    self.end_headers()
                    return
                if self.path == '/stall':
                    owner.started.set()
                    owner.release.wait(4)
                if self.path == '/error':
                    self.send_error(403)
                    return
                body = b'Plain text from a URL.' if self.path == '/text' else GUIDE.encode()
                mime = 'text/plain; charset=utf-8' if self.path == '/text' else 'text/html; charset=utf-8'
                if self.path == '/image':
                    body, mime = b'\x89PNG\x00', 'image/png'
                body, mime = owner.pages.get(self.path, (body, mime))
                if self.path == '/gzip':
                    body = gzip.compress(b'A' * 10000)
                self.send_response(200)
                self.send_header('Content-Type', mime)
                self.send_header('Content-Length', str(len(body)))
                if self.path == '/gzip':
                    self.send_header('Content-Encoding', 'gzip')
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.host = 'http://127.0.0.1:' + str(self.server.server_port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class ExtractionTests(unittest.TestCase):
    def test_normalization_preserves_query_identity_and_validates_schemes(self):
        self.assertEqual(normalize_url(' HTTPS://Example.COM:443/docs?a=1&b=2#intro '), 'https://example.com/docs?a=1&b=2')
        self.assertEqual(parse_urls('https://example.com\nhttps://EXAMPLE.com/#part'), ['https://example.com/'])
        self.assertEqual(normalize_url('http://[::1]:8080/a'), 'http://[::1]:8080/a')
        for value in ('file:///etc/passwd', 'ftp://example.com', 'http://user:password@example.com', 'http://bad host', 'https://x:99999', ''):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_url(value)
        with self.assertRaises(ValueError):
            parse_urls('\n'.join('https://example.com/'+str(n) for n in range(21)))

    def test_documentation_structure_and_boilerplate(self):
        title, text, extractor = extract_html(GUIDE.encode(), 'https://example.com/docs/guide')
        self.assertEqual(title, 'Vector guide')
        for value in ('# Search your documents', '```', '    return query', '| Qwen | 1024 |', 'Keep this nested note.', 'https://example.com/reference'):
            self.assertIn(value, text)
        for value in ('Global navigation', 'Table of contents', 'Sidebar links', 'User comments', 'Footer links', 'Hidden text', 'run_dangerous_code'):
            self.assertNotIn(value, text)

    def test_article_divs_and_selector_override_preserve_order_without_duplicates(self):
        page = '<html><title>Article</title><body><div class="sidebar">Noise</div><div class="entry-content"><h1>News</h1><p>First story.</p><p>Second story.</p><div class="related-posts">Related stories</div></div></body></html>'
        _, text, _ = extract_html(page.encode(), 'https://example.com/')
        self.assertIn('First story.', text)
        self.assertNotIn('Related stories', text)
        self.assertNotIn('Noise', text)
        _, selected, _ = extract_html(GUIDE.encode(), 'https://example.com/', '.notes, .notes div, h1')
        self.assertEqual(selected.count('Keep this detail too.'), 1)
        self.assertLess(selected.index('Search your documents'), selected.index('Keep this nested note.'))
        self.assertNotIn('def search', selected)
        for selector in ('no-such-element', '[broken'):
            with self.assertRaises(ValueError):
                extract_html(GUIDE.encode(), 'https://example.com/', selector)

    def test_generic_article_fallback_encoding_and_empty_selection(self):
        paragraph = 'This article explains how the town library serves its community, with extended opening hours and more books. '
        page = '<html><title>Café</title><body><div><p>'+paragraph*4+'</p><p>'+paragraph*3+'</p></div></body></html>'
        title, text, extractor = extract_html(page.encode('iso-8859-1'), 'https://example.com/', charset='iso-8859-1')
        self.assertEqual(title, 'Café')
        self.assertIn('town library', text)
        self.assertTrue(extractor.startswith('trafilatura'))
        page = '<html><title>Café 🌻</title><main><p>Unicode 🌻 content.</p></main></html>'
        title, text, _ = extract_html(page.encode(), 'https://example.com/')
        self.assertEqual(title, 'Café 🌻')
        self.assertIn('Unicode 🌻', text)
        page = '<html><head><meta charset="iso-8859-1"><title>Café</title></head><main><p>Café content.</p></main></html>'
        title, text, _ = extract_html(page.encode('iso-8859-1'), 'https://example.com/')
        self.assertEqual(title, 'Café')
        self.assertIn('Café', text)
        with self.assertRaises(ValueError):
            extract_html(b'<main><script>only_js()</script></main>', 'https://example.com/', 'main')

    def test_fetch_redirect_text_and_failure_limits(self):
        fixture = WebServer()
        self.addCleanup(fixture.close)
        with tempfile.TemporaryDirectory() as temp:
            result = fetch_url(fixture.host+'/redirect', temp, Gio.Cancellable())
            self.assertEqual(result.final_url, fixture.host+'/guide')
            doc, source, warnings = extract_download(result)
            self.assertEqual(source['source_url'], fixture.host+'/redirect')
            self.assertIn('Keep this nested note', doc['text'])
            result.discard()
            result = fetch_url(fixture.host+'/text', temp, Gio.Cancellable())
            self.assertEqual(extract_download(result)[0]['text'], 'Plain text from a URL.')
            result.discard()
            result = fetch_url(fixture.host+'/image', temp, Gio.Cancellable())
            with self.assertRaises(ValueError):
                extract_download(result)
            result.discard()
            for path in ('/loop', '/unsafe', '/error'):
                with self.assertRaises(ValueError):
                    fetch_url(fixture.host+path, temp, Gio.Cancellable())
            with patch('src.web_import.MAX_DOWNLOAD', 1000), self.assertRaisesRegex(ValueError, '50 MiB'):
                fetch_url(fixture.host+'/gzip', temp, Gio.Cancellable())
            self.assertEqual(list(Path(temp).iterdir()), [])

    def test_fetch_timeout_and_cancellation_close_partial_files(self):
        fixture = WebServer()
        self.addCleanup(fixture.close)
        with tempfile.TemporaryDirectory() as temp:
            with patch('src.web_import.REQUEST_TIMEOUT', .1), self.assertRaisesRegex(ValueError, 'timed out'):
                fetch_url(fixture.host+'/stall', temp, Gio.Cancellable())
            cancel = Gio.Cancellable()
            timer = threading.Timer(.1, cancel.cancel)
            timer.start()
            try:
                with self.assertRaises(ollama.RequestCancelled):
                    fetch_url(fixture.host+'/stall', temp, cancel)
            finally:
                timer.join()
            self.assertEqual(list(Path(temp).iterdir()), [])

    def test_direct_pdf_retains_page_locations(self):
        from pypdf import PdfWriter
        from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
        writer = PdfWriter()
        page = writer.add_blank_page(300, 300)
        font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'),
                                 NameObject('/BaseFont'): NameObject('/Helvetica')})
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
        content = DecodedStreamObject()
        content.set_data(b'BT /F1 12 Tf 30 250 Td (A searchable URL PDF.) Tj ET')
        page[NameObject('/Contents')] = writer._add_object(content)
        output = io.BytesIO()
        writer.write(output)
        fixture = WebServer()
        fixture.pages['/pdf'] = (output.getvalue(), 'application/pdf')
        self.addCleanup(fixture.close)
        with tempfile.TemporaryDirectory() as temp:
            downloaded = fetch_url(fixture.host+'/pdf', temp, Gio.Cancellable())
            doc, source, warnings = extract_download(downloaded)
            self.assertIn('searchable URL PDF', doc['text'])
            self.assertEqual(doc['pages'][0]['page'], 1)
            self.assertEqual(source['content_type'], 'application/pdf')
            self.assertEqual(source['selector'], '')
            downloaded.discard()


class WebKnowledgeTests(unittest.TestCase):
    def setUp(self):
        from src.storage import ChatStorage
        ollama.resume()
        self.temp = tempfile.TemporaryDirectory()
        self.storage = ChatStorage(self.temp.name)
        self.doc, self.config, self.index, self.options = indexed(self.storage)
        for id in ('a', 'b'):
            self.storage.db.create_knowledge_collection(dict(id=id, name='Collection '+id, config_id=self.config['id'],
                host=self.index['host'], model=self.index['model'], chunk_size=1600, overlap=200, created_at=0, updated_at=0))
            self.storage.db.prepare_collection_indexes(id, [self.doc['id']])
        self.source = dict(source_url='https://example.com/', final_url='https://example.com/article', fetched_at=1,
                           content_type='text/html', selector='main', extractor='test', edited=False)
        with self.storage.db._get_conn() as conn:
            conn.execute('INSERT INTO knowledge_web_sources VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                         (self.doc['id'],) + tuple(self.source.values()))
            conn.commit()

    def tearDown(self):
        self.storage.knowledge.cancel_all()
        pump_until(lambda: self.storage.knowledge.idle and self.storage.writer.idle and session.worker.idle, timeout=10)
        self.storage.knowledge.shutdown()
        self.storage.writer.shutdown()
        self.temp.cleanup()

    def test_replacement_invalidates_all_shared_vectors_and_preserves_history(self):
        db = self.storage.db
        hits = db.search_knowledge(self.config['id'], {}, [1, 0], collection_ids=['a'])
        self.assertEqual(hits[0]['web_source']['source_url'], self.source['source_url'])
        snapshot = {'hits': hits}
        self.assertIn('URL: https://example.com/article', augmented_messages([{'role':'user','content':'question'}], snapshot)[-1]['content'])
        db.create_chat('saved', 'Saved', 0, 0, 'chat')
        db.save_messages('saved', [{'role':'assistant', 'content':'Earlier answer', 'response_metadata':{'retrieval':snapshot}}])
        candidate = make_document('Updated', 'Different article text.')
        result = db.save_web_document(candidate, self.source, 'a', self.doc['id'], self.doc['content_hash'])
        self.assertEqual(set(result['collections']), {'a', 'b'})
        self.assertEqual(len(result['indexes']), 1)
        self.assertEqual(db.knowledge_document(self.doc['id'])['text'], candidate['text'])
        self.assertTrue(all(c['ready'] == 0 for c in db.knowledge_collections()))
        self.assertEqual(db.knowledge_chunk_ids(self.index['id']), set())
        with self.assertRaises(ValueError):
            db.search_knowledge(self.config['id'], {}, [1, 0], collection_ids=['b'])
        self.assertEqual(db.get_chat('saved')['messages'][0]['response_metadata']['retrieval'], snapshot)

    def test_unchanged_source_reuses_vectors_and_stale_preview_cannot_replace(self):
        db = self.storage.db
        old = db.web_document(self.source['source_url'])
        ids = db.knowledge_chunk_ids(self.index['id'])
        result = db.save_web_document(self.doc, dict(self.source, fetched_at=2), 'a', old['id'], old['content_hash'])
        self.assertFalse(result['changed'])
        self.assertEqual(db.knowledge_chunk_ids(self.index['id']), ids)
        with self.assertRaisesRegex(ValueError, 'changed since'):
            db.save_web_document(make_document('Stale', 'Wrong content'), self.source, 'a', old['id'], 'outdated-hash')
        with self.assertRaisesRegex(ValueError, 'deleted'):
            db.save_web_document(self.doc, self.source, 'missing', old['id'], old['content_hash'])
        self.assertEqual(db.knowledge_chunk_ids(self.index['id']), ids)
        other = db.save_web_document(make_document('Same text, distinct URL', self.doc['text']),
                                     dict(self.source, source_url='https://other.example/'), 'a')
        self.assertNotEqual(other['id'], old['id'])

    def test_service_rebuilds_and_rejects_busy_documents(self):
        done = []
        with patch.object(ollama, 'fetch_model_details', return_value=[TAG]), \
             patch.object(ollama, 'embed', side_effect=lambda host, model, texts, **kw: {'embeddings': [[1, 0] for _ in texts]}):
            job = self.storage.knowledge.save_web_document(make_document('Updated', 'New article information.'),
                self.source, 'a', self.storage.db.web_document(self.source['source_url']), lambda r, e: done.append((r,e)))
            with self.assertRaisesRegex(ValueError, 'Wait for'):
                self.storage.knowledge.save_web_document(self.doc, self.source, 'b', self.doc)
            pump_until(lambda: bool(done) and self.storage.knowledge.idle and self.storage.writer.idle)
            self.assertIsNone(done[0][1])
            self.assertTrue(all(c['ready'] == 1 for c in self.storage.db.knowledge_collections()))
            self.assertFalse(self.storage.knowledge._document_updates)
            self.assertEqual(len(self.storage.db.knowledge_indexes()), 1)

    def test_migration_rollback_and_backup(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp)/'old.db')
            with patch.object(database, 'MIGRATIONS', database.MIGRATIONS[:7]):
                old = DatabaseManager(path)
            old.add_knowledge_document(self.doc)
            def fail(conn, progress=None):
                conn.execute('CREATE TABLE knowledge_web_sources(id TEXT)')
                raise ValueError('injected failure')
            with patch.object(database, 'MIGRATIONS', database.MIGRATIONS[:7]+[fail]):
                with self.assertRaises(DatabaseUpgradeError):
                    DatabaseManager(path)
            with sqlite3.connect(path) as conn:
                self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 8)
                self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='knowledge_web_sources'").fetchone())
            new = DatabaseManager(path)
            self.assertEqual(new.knowledge_document(self.doc['id'])['text'], self.doc['text'])
            self.assertIsNone(new.knowledge_document(self.doc['id'])['web_source'])
            self.assertIn('.pre-v10-', new.backup_path)

    def test_failed_replacement_rolls_back_text_vectors_and_provenance(self):
        db = self.storage.db
        before = db.knowledge_document(self.doc['id'])
        chunks = db.knowledge_chunk_ids(self.index['id'])
        delete = db._delete_index_vectors
        def fail(conn, index_id):
            delete(conn, index_id)
            raise sqlite3.OperationalError('injected disk error')
        with patch.object(db, '_delete_index_vectors', side_effect=fail), self.assertRaises(sqlite3.OperationalError):
            db.save_web_document(make_document('New', 'New content'), dict(self.source, fetched_at=3),
                                  'a', self.doc['id'], self.doc['content_hash'])
        self.assertEqual(db.knowledge_document(self.doc['id']), before)
        self.assertEqual(db.knowledge_chunk_ids(self.index['id']), chunks)
        self.assertTrue(db.search_knowledge(self.config['id'], {}, [1, 0], collection_ids=['a']))
        self.assertTrue(all(c['ready'] == 1 for c in db.knowledge_collections()))

    def test_failed_rebuild_can_retry_and_preserves_standalone_settings(self):
        db = self.storage.db
        service = self.storage.knowledge
        second = dict(self.index, id='standalone', chunk_size=250, overlap=25)
        db.begin_knowledge_index(second)
        db.finish_knowledge_index(second['id'], 'interrupted')
        with patch.object(ollama, 'fetch_model_details', return_value=[TAG]), \
             patch.object(ollama, 'embed', side_effect=ollama.OllamaError('offline')):
            service.save_web_document(make_document('Updated', 'New reference text.'), self.source, 'a',
                                       db.web_document(self.source['source_url']))
            pump_until(lambda: service.idle and self.storage.writer.idle)
        indexes = db.knowledge_indexes(document_id=self.doc['id'])
        self.assertEqual({i['status'] for i in indexes}, {'failed'})
        self.assertEqual({(i['id'], i['chunk_size'], i['overlap']) for i in indexes},
                          {(self.index['id'], 1600, 200), ('standalone', 250, 25)})
        self.assertTrue(all(c['ready'] == 0 for c in db.knowledge_collections()))
        with patch.object(ollama, 'fetch_model_details', return_value=[TAG]), \
             patch.object(ollama, 'embed', side_effect=lambda h,m,t,**kw: {'embeddings':[[1,0] for _ in t]}):
            service.build_collection('a')
            pump_until(lambda: service.idle and self.storage.writer.idle)
        self.assertTrue(all(c['ready'] == 1 for c in db.knowledge_collections()))
        self.assertTrue(db.search_knowledge(self.config['id'], {}, [1, 0], collection_ids=['b']))

    def test_queued_replacement_guards_new_builds_and_cancellation_releases_reservation(self):
        service = self.storage.knowledge
        release = threading.Event()
        started = threading.Event()
        def wait(cancel, progress):
            started.set()
            release.wait(5)
        service.submit('Other embedding work', wait)
        self.assertTrue(started.wait(2))
        try:
            job = service.save_web_document(make_document('Changed', 'Updated reference'), self.source, 'a',
                                            self.storage.db.web_document(self.source['source_url']))
            with self.assertRaisesRegex(ValueError, 'being replaced'):
                service.build_collection('a')
            with self.assertRaisesRegex(ValueError, 'being replaced'):
                service.create_index(self.doc['id'], self.index['host'], TAG['name'], self.config)
            job['cancel'].cancel()
        finally:
            release.set()
        pump_until(lambda: service.idle and self.storage.writer.idle)
        self.assertFalse(service._document_updates)
        self.assertEqual(self.storage.db.knowledge_document(self.doc['id'])['text'], self.doc['text'])


@unittest.skipUnless(Gdk.Display.get_default(), 'GTK tests require a display')
class URLDialogTests(unittest.TestCase):
    setUp = test_ui.UITests.setUp
    make_window = test_ui.UITests.make_window

    def tearDown(self):
        self.storage.knowledge.cancel_all()
        pump_until(lambda: self.storage.knowledge.idle)
        self.storage.knowledge.shutdown()
        test_ui.UITests.tearDown(self)
    def test_batch_preview_selector_edit_save_and_mixed_failure(self):
        fixture = WebServer()
        self.addCleanup(fixture.close)
        document, config, index, options = indexed(self.storage)
        self.storage.db.create_knowledge_collection(dict(id='web', name='Web', config_id=config['id'],
            host=index['host'], model=index['model'], chunk_size=1600, overlap=200, created_at=0, updated_at=0))
        window, tab = self.make_window()
        window.present()
        dialog = URLImportDialog(self.storage, 'web')
        dialog.present(window)
        dialog.urls.get_buffer().set_text(fixture.host+'/guide\n'+fixture.host+'/error')
        dialog.start()
        pump_until(lambda: dialog.active == 0 and all(i['status'] not in ('queued', 'fetching') for i in dialog.items), timeout=10)
        self.assertEqual([i['status'] for i in dialog.items], ['ready', 'failed'])
        self.assertEqual(len(self.storage.db.knowledge_documents()), 1)
        dialog.rows.select_row(dialog.items[0]['row'])
        dialog.selector.set_text('.notes')
        dialog.reextract()
        pump_until(lambda: dialog.active == 0)
        self.assertNotIn('def search', buffer_text(dialog.editor))
        valid_text = buffer_text(dialog.editor)
        dialog.selector.set_text('no-such-element')
        dialog.reextract()
        pump_until(lambda: dialog.active == 0)
        self.assertEqual(buffer_text(dialog.editor), valid_text)
        self.assertFalse(dialog.save_button.get_sensitive())
        dialog.selector.set_text('.notes')
        dialog.reextract()
        pump_until(lambda: dialog.active == 0)
        dialog.title_entry.set_text('Edited page')
        dialog.editor.get_buffer().set_text('My reviewed text for the collection.')
        with patch.object(ollama, 'fetch_model_details', return_value=[TAG]), \
                patch.object(ollama, 'embed', side_effect=lambda h,m,t,**kw: {'embeddings':[[1,0] for _ in t]}):
            dialog.save()
            pump_until(lambda: dialog.items[0]['status'] == 'saved' and self.storage.knowledge.idle and self.storage.writer.idle)
        saved = self.storage.db.web_document(fixture.host+'/guide')
        self.assertEqual(saved['title'], 'Edited page')
        self.assertEqual(saved['text'], 'My reviewed text for the collection.')
        self.assertTrue(saved['web_source']['edited'])
        self.assertEqual(saved['web_source']['selector'], '.notes')
        dialog.close()
        pump_until(lambda: dialog.closed)
        self.assertFalse(Path(dialog.cache.name).exists())

    def make_import(self, urls):
        from test_collections import collection
        document, config, index, options = indexed(self.storage)
        collection(self.storage.db, 'Web', config)
        dialog = URLImportDialog(self.storage, 'Web', urls)
        window, tab = self.make_window()
        window.present()
        dialog.present(window)
        dialog.start()
        return dialog

    def test_cache_pauses_and_skip_resumes_without_losing_another_preview(self):
        fixture = WebServer()
        self.addCleanup(fixture.close)
        size = len(GUIDE.encode())
        with patch('src.widgets.url_import.MAX_DOWNLOAD', size), patch('src.widgets.url_import.MAX_CACHE', 2*size):
            dialog = self.make_import('\n'.join(fixture.host+'/'+str(n) for n in range(4)))
            pump_until(lambda: dialog.active == 0)
            self.assertEqual([i['status'] for i in dialog.items], ['ready', 'ready', 'queued', 'queued'])
            dialog.rows.select_row(dialog.items[1]['row'])
            dialog.editor.get_buffer().set_text('Unsaved second preview')
            dialog.rows.select_row(dialog.items[0]['row'])
            dialog.skip()
            pump_until(lambda: dialog.active == 0)
            self.assertEqual([i['status'] for i in dialog.items], ['skipped', 'ready', 'ready', 'queued'])
            dialog.rows.select_row(dialog.items[1]['row'])
            self.assertEqual(buffer_text(dialog.editor), 'Unsaved second preview')
            dialog.rows.select_row(dialog.items[2]['row'])
            dialog.retry()
            pump_until(lambda: dialog.active == 0)
            self.assertEqual(dialog.items[2]['status'], 'ready')
            self.assertEqual(dialog.items[3]['status'], 'queued')
            self.assertLessEqual(sum(i['download'].size for i in dialog.items if i['download']), 2*size)
            dialog.close()
            pump_until(lambda: dialog.closed)

    def test_close_cancels_fetch_and_does_not_add_documents(self):
        fixture = WebServer()
        self.addCleanup(fixture.close)
        dialog = self.make_import(fixture.host+'/stall')
        pump_until(fixture.started.is_set)
        dialog.close()
        pump_until(lambda: dialog.closed and dialog.active == 0)
        self.assertFalse(Path(dialog.cache.name).exists())
        self.assertEqual(len(self.storage.db.knowledge_documents()), 1)

    def test_replacement_requires_confirmation_and_cancel_preserves_document(self):
        fixture = WebServer()
        self.addCleanup(fixture.close)
        dialog = self.make_import(fixture.host+'/guide')
        pump_until(lambda: dialog.active == 0)
        item = dialog.items[0]
        saved = self.storage.db.save_web_document(item['document'], item['source'], 'Web')
        old = self.storage.db.web_document(item['url'])
        dialog.retry()
        pump_until(lambda: dialog.active == 0)
        dialog.editor.get_buffer().set_text('A replacement page reviewed by the user.')
        prompts = []
        with patch('src.widgets.url_import.Adw.AlertDialog.present', lambda d, parent: prompts.append(d)):
            dialog.save()
        self.assertEqual(len(prompts), 1)
        self.assertIn('Web', prompts[0].get_body())
        prompts[0].emit('response', 'cancel')
        self.assertEqual(self.storage.db.knowledge_document(saved['id'])['text'], old['text'])
        with patch('src.widgets.url_import.Adw.AlertDialog.present', lambda d, parent: prompts.append(d)), \
             patch.object(ollama, 'fetch_model_details', return_value=[TAG]), \
             patch.object(ollama, 'embed', side_effect=lambda h,m,t,**kw: {'embeddings':[[1,0] for _ in t]}):
            dialog.save()
            prompts[-1].emit('response', 'replace')
            pump_until(lambda: item['status'] == 'saved' and self.storage.knowledge.idle and self.storage.writer.idle)
        self.assertEqual(self.storage.db.web_document(item['url'])['id'], old['id'])
        self.assertEqual(self.storage.db.knowledge_document(old['id'])['text'], 'A replacement page reviewed by the user.')
        dialog.close()
        pump_until(lambda: dialog.closed)
