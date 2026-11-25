"""An embedding host is its type and address: collections, indexes, and chats keep both."""
import copy
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from gi.repository import Gdk, Gio, Gtk

from legacy_schema import begin_index
from provider_fixture import LIMITED, provider_patch
from src import database
from src.app_context import AppContext
from src.database import DatabaseManager, DatabaseUpgradeError
from src.knowledge import DEFAULT_RAG, make_document, new_config, vector_blob
from src.providers import Capabilities, OllamaProvider, RequestCancelled
from src.providers.registry import PROVIDERS
from src.widgets.knowledge_common import HostModels, IndexDialog, verification_notice
from src.widgets.knowledge_view import KnowledgeView
from src.widgets.source_views import SourcePicker
from test_knowledge import TAG
import test_ui
from test_ui import pump_until

URL = 'http://embed:11434'
PRINT = {'name': 'embed-model', 'digest': 'fingerprint-one'}
UNVERIFIED = 'cannot be fully verified'


class FingerprintProvider(OllamaProvider):
    """Embeds, and knows a model only by what it reports of it, as the OpenAI-compatible types do."""
    kind = 'fingerprint'
    label = 'Fingerprint Server'
    capabilities = Capabilities(embeddings=True)


class HostedProvider(FingerprintProvider):
    """A service at one address, whose models the user cannot add to."""
    kind = 'hosted'
    fixed_url = 'https://hosted.example'


TYPES = {'fingerprint': FingerprintProvider, 'hosted': HostedProvider, **LIMITED}


def tags(host, **kwargs):
    """Each type at the address lists its own model."""
    return [PRINT] if host.kind == 'fingerprint' else [TAG]


def vectors(host, model, input, **kwargs):
    return {'embeddings': [[1., 0., 0.] for text in ([input] if isinstance(input, str) else input)]}


def three_hosts(storage):
    """An Ollama host, a fingerprint host, and one that cannot embed, all at one address."""
    return (storage.add_host('Embedding host', URL), storage.save_host('Prints', URL, provider='fingerprint'),
            storage.save_host('Limited', URL, provider='limited'))


def options(config, kind=None, **kwargs):
    value = dict(copy.deepcopy(DEFAULT_RAG), enabled=True, config_id=config['id'], host=URL, model=config['model'], **kwargs)
    if kind:
        value['host_kind'] = kind
    else:
        del value['host_kind']  # As a chat saved before the type was stored.
    return value


def descendants(widget):
    child = widget.get_first_child()
    while child:
        yield child
        yield from descendants(child)
        child = child.get_next_sibling()


def texts(widget):
    # A collapsed expander holds its child outside the tree.
    for expander in [w for w in descendants(widget) if isinstance(w, Gtk.Expander)]:
        expander.set_expanded(True)
    return '\n'.join(w.get_text() for w in descendants(widget) if isinstance(w, Gtk.Label) and w.get_visible())


class KnowledgeHostTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.context = AppContext(self.temp.name)
        self.storage, self.knowledge = self.context.storage, self.context.knowledge
        types = patch.dict(PROVIDERS, TYPES)
        types.start()
        self.addCleanup(types.stop)
        self.ollama, self.prints, self.limited = three_hosts(self.storage)

    def tearDown(self):
        self.knowledge.cancel_all()
        pump_until(lambda: self.knowledge.idle and self.storage.writer.idle)
        self.knowledge.shutdown()
        self.context.shutdown()
        self.temp.cleanup()

    def document(self, text='Alpha passage.\n\nBeta passage.'):
        document = make_document('Guide', text)
        self.storage.db.knowledge.add_knowledge_document(document)
        return document

    def build(self, document, config, kind='fingerprint', **kwargs):
        job = self.knowledge.create_index(document['id'], URL, config['model'], config, kind=kind, **kwargs)
        pump_until(lambda: job['done'] and self.storage.writer.idle, timeout=8)
        return job

    def test_two_types_at_one_address_resolve_to_the_stored_type(self):
        for address in (URL, URL + '/'):
            self.assertEqual(self.knowledge.require_host(address).host_id, self.ollama['id'])
            self.assertEqual(self.knowledge.require_host(address, 'ollama').host_id, self.ollama['id'])
            found = self.knowledge.require_host(address, 'fingerprint')
            self.assertEqual((found.kind, found.host_id), ('fingerprint', self.prints['id']))
        for kind in ('limited', 'unknown'):
            with self.assertRaisesRegex(ValueError, 'embedding host was removed'):
                self.knowledge.require_host(URL, kind)
        for address in ('', 'not a url', None):
            with self.assertRaisesRegex(ValueError, 'embedding host was removed'):
                self.knowledge.require_host(address, 'fingerprint')
        # No other type answers for one that was removed, in either direction.
        self.storage.delete_host(self.ollama['id'])
        with self.assertRaisesRegex(ValueError, 'embedding host was removed'):
            self.knowledge.require_host(URL)
        self.assertEqual(self.knowledge.require_host(URL, 'fingerprint').host_id, self.prints['id'])
        # A host created again is the same server: the record's id is not the identity.
        again = self.storage.add_host('Again', URL + '/')
        self.assertEqual(self.knowledge.require_host(URL).host_id, again['id'])

    def test_collection_is_built_and_searched_on_a_fingerprint_host(self):
        document, config = self.document(), new_config(PRINT['name'], PRINT['digest'])
        done = []
        with provider_patch('list_models', side_effect=tags), provider_patch('embed', side_effect=vectors) as embed, \
                provider_patch('embedding_context', return_value=None):
            self.knowledge.create_collection('Guides', URL, PRINT['name'], config, document_ids=[document['id']],
                                             callback=lambda id, error: done.append((id, error)), kind='fingerprint')
            pump_until(lambda: done and self.knowledge.idle and self.storage.writer.idle, timeout=8)
            collection = self.storage.db.knowledge.knowledge_collections()[0]
            self.assertEqual(done, [(collection['id'], None)])
            self.assertEqual((collection['host'], collection['host_kind'], collection['ready']), (URL, 'fingerprint', 1))
            index = self.storage.db.knowledge.knowledge_indexes()[0]
            self.assertEqual((index['host'], index['host_kind'], index['status']), (URL, 'fingerprint', 'complete'))
            # No server lists a model's dimensions: they are those of the first vector returned.
            self.assertEqual(collection['dimensions'], 3)
            chosen = options(config, 'fingerprint', collection_ids=[collection['id']])
            snapshot = self.knowledge.retrieve(chosen, 'Alpha', Gio.Cancellable())
            self.assertEqual(snapshot['hits'][0]['document_id'], document['id'])
            self.assertEqual({call.args[0].kind for call in embed.call_args_list}, {'fingerprint'})
            # Without a type the address names the Ollama host there, which has no such model.
            calls = embed.call_count
            with self.assertRaisesRegex(ValueError, 'unavailable'):
                self.knowledge.retrieve(options(config, collection_ids=[collection['id']]), 'Alpha', Gio.Cancellable())
            self.assertEqual(embed.call_count, calls)
        # A changed fingerprint is refused as a changed digest is, for a search and for a build.
        changed = lambda host, **kw: [dict(PRINT, digest='fingerprint-two')]  # noqa: E731
        with provider_patch('list_models', side_effect=changed), provider_patch('embed') as embed, \
                provider_patch('embedding_context', return_value=None):
            with self.assertRaisesRegex(ValueError, 'embedding model has changed'):
                self.knowledge.retrieve(chosen, 'Alpha', Gio.Cancellable())
            job = self.build(document, config)
            self.assertIn('embedding model has changed', job['error'])
            embed.assert_not_called()

    def test_over_long_text_is_refused_before_any_request_where_the_context_is_known(self):
        # Chunks of 400 characters: 100 tokens by the estimate.
        document, config = self.document('word ' * 200), new_config(PRINT['name'], PRINT['digest'])
        with provider_patch('list_models', side_effect=tags), provider_patch('embed', side_effect=vectors) as embed:
            with provider_patch('embedding_context', return_value=100) as context:
                job = self.build(document, config, size=400, overlap=0)
                self.assertRegex(job['error'], r'chunk is estimated at 100 tokens.* accepts 100,.* above 80 ')
                embed.assert_not_called()
                # Asked once for the job, not for each chunk.
                self.assertEqual([call.args[1] for call in context.call_args_list], [PRINT['name']])
                index = self.storage.db.knowledge.knowledge_indexes()[0]
                self.assertEqual((index['status'], index['chunks']), ('failed', 0))
            # A fifth of the limit is held back, and a chunk at that mark is sent.
            with provider_patch('embedding_context', return_value=125):
                self.assertIsNone(self.build(document, config, size=400, overlap=0, index_id=index['id'])['error'])
                self.assertEqual(self.storage.db.knowledge.knowledge_indexes()[0]['chunks'], 3)
            chosen = options(config, 'fingerprint', selection={index['id']: None})
            calls = embed.call_count
            with provider_patch('embedding_context', return_value=100) as context:
                with self.assertRaisesRegex(ValueError, r'search query is estimated at 100 tokens.* accepts 100,.* above 80 '):
                    self.knowledge.retrieve(chosen, 'q' * 400, Gio.Cancellable())
                self.assertEqual(embed.call_count, calls)
                self.assertTrue(self.knowledge.retrieve(chosen, 'q' * 320, Gio.Cancellable())['hits'])
                self.assertEqual(context.call_count, 2)
            # Where the server does not say, everything is sent.
            with provider_patch('embedding_context', return_value=None):
                self.assertTrue(self.knowledge.retrieve(chosen, 'q' * 4000, Gio.Cancellable())['hits'])
                self.assertIsNone(self.build(document, config, size=400, overlap=0)['error'])

    def test_ollama_hosts_are_not_checked_for_length(self):
        document, config = self.document('word ' * 200), new_config(TAG['name'], TAG['digest'])
        with provider_patch('list_models', side_effect=tags), provider_patch('embed', side_effect=vectors) as embed, \
                provider_patch('embedding_context', return_value=1) as context:
            job = self.build(document, config, kind=None, size=400, overlap=0)
            self.assertIsNone(job['error'])
            chosen = options(config, selection={job['index_id']: None})
            self.assertTrue(self.knowledge.retrieve(chosen, 'q' * 4000, Gio.Cancellable())['hits'])
            context.assert_not_called()
            self.assertEqual({call.args[0].kind for call in embed.call_args_list}, {'ollama'})
        self.assertEqual(self.storage.db.knowledge.knowledge_indexes()[0]['host_kind'], 'ollama')

    def test_a_job_holds_only_the_type_it_runs_on(self):
        document, config = self.document(), new_config(PRINT['name'], PRINT['digest'])
        started, release = threading.Event(), threading.Event()
        def stalled(host, model, input, cancellable=None, **kwargs):
            started.set()
            release.wait(3)
            raise RequestCancelled('stopped')
        with provider_patch('list_models', side_effect=tags), provider_patch('embed', side_effect=stalled), \
                provider_patch('embedding_context', return_value=None):
            waiting = self.knowledge.create_index(document['id'], URL, PRINT['name'], config, kind='fingerprint')
            # Queued or running, the job is its type's alone.
            self.assertTrue(self.knowledge.busy(self.prints, PRINT['name']))
            self.assertFalse(self.knowledge.busy(self.ollama, PRINT['name']))
            pump_until(started.is_set)
            for host, busy in ((self.prints, True), (dict(self.prints, hostname=URL + '/'), True),
                               (self.context.connection(self.prints), True), (self.ollama, False), (self.limited, False)):
                self.assertEqual(self.knowledge.busy(host, PRINT['name'] + ':latest'), busy)
            reserve = lambda host: self.knowledge.reserve_model(host, PRINT['name'], lambda h, m: False)  # noqa: E731
            self.assertFalse(reserve(self.prints))
            self.assertTrue(reserve(self.ollama))
            self.context.services.models.release(self.ollama, PRINT['name'])
            waiting['cancel'].cancel()
            release.set()
            pump_until(lambda: waiting['done'] and self.storage.writer.idle)
        self.assertFalse(self.knowledge.busy(self.prints, PRINT['name']))

    def test_changing_a_collections_model_stores_the_new_type(self):
        config = new_config(TAG['name'], TAG['digest'])
        self.storage.db.knowledge.add_embedding_config(config)
        self.storage.db.knowledge.create_knowledge_collection(dict(
            id='guides', name='Guides', config_id=config['id'], host='http://old:11434', model=TAG['name'],
            chunk_size=1600, overlap=200, created_at=0, updated_at=0))
        self.assertEqual(self.storage.db.knowledge.knowledge_collection('guides')['host_kind'], 'ollama')
        with self.assertRaisesRegex(ValueError, 'original digest'):
            self.storage.db.knowledge.update_collection_endpoint('guides', URL, TAG['name'], 'other', 'fingerprint')
        self.storage.db.knowledge.update_collection_endpoint('guides', URL, TAG['name'], TAG['digest'], 'fingerprint')
        saved = self.storage.db.knowledge.knowledge_collection('guides')
        self.assertEqual((saved['host'], saved['host_kind']), (URL, 'fingerprint'))
        # New members are built where the collection now points.
        document = self.document()
        jobs = self.storage.db.knowledge.prepare_collection_indexes('guides', [document['id']])
        self.assertEqual([(j['host'], j['host_kind']) for j in jobs], [(URL, 'fingerprint')])
        self.assertEqual(self.storage.db.knowledge.knowledge_indexes()[0]['host_kind'], 'fingerprint')


class HostKindMigrationTests(unittest.TestCase):
    def old_database(self, path):
        """A version 12 database holding a host, a ready collection, and a chat that searches it."""
        with patch.object(database, 'MIGRATIONS', database.MIGRATIONS[:11]):
            old = DatabaseManager(str(path))
        document, config = make_document('Old source', 'Kept passage.'), new_config(TAG['name'], TAG['digest'])
        old.knowledge.add_knowledge_document(document)
        old.knowledge.add_embedding_config(config)
        begin_index(old, dict(id='index', document_id=document['id'], config_id=config['id'], host=URL,
                              model=TAG['name'], chunk_size=1600, overlap=200, created_at=0))
        old.knowledge.save_embedding_batch('index', config['id'], 2, [
            dict(id='chunk', ordinal=0, start=0, end=13, vector=vector_blob([1, 0]))])
        old.knowledge.finish_knowledge_index('index', 'complete')
        chosen = options(config, collection_ids=['old'])
        with old._get_conn() as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 12)
            conn.execute("INSERT INTO hosts (id, name, hostname, is_default) VALUES ('embed', 'Embedding host', ?, 1)", (URL,))
            conn.execute('''INSERT INTO knowledge_collections
                (id, name, config_id, host, model, chunk_size, overlap, created_at, updated_at)
                VALUES ('old', 'Old', ?, ?, ?, 1600, 200, 0, 0)''', (config['id'], URL, TAG['name']))
            conn.execute("INSERT INTO knowledge_collection_documents VALUES ('old', ?, 'index')", (document['id'],))
            conn.commit()
        old.chats.create_chat('chat', 'Saved chat', 0, 0, 'model')
        old.chats.save_tool_state('chat', options={'knowledge': chosen})
        old.close()
        return document, chosen

    def columns(self, conn, table):
        return [row[1] for row in conn.execute(f'PRAGMA table_info({table})')]

    def test_upgrade_gives_existing_rows_the_ollama_type_and_old_chats_find_their_host(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(PROVIDERS, TYPES):
            path = Path(temp) / 'gnollama.db'
            document, chosen = self.old_database(path)
            context = AppContext(temp)
            try:
                db = context.storage.db
                self.assertIn('.pre-v14-', db.backup_path)
                with sqlite3.connect(db.backup_path) as backup:
                    self.assertEqual(backup.execute('PRAGMA user_version').fetchone()[0], 12)
                    self.assertNotIn('host_kind', self.columns(backup, 'knowledge_collections'))
                    self.assertEqual(backup.execute('SELECT host FROM knowledge_indexes').fetchone()[0], URL)
                with db._get_conn() as conn:
                    self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 14)
                collection, index = db.knowledge.knowledge_collections()[0], db.knowledge.knowledge_indexes()[0]
                self.assertEqual((collection['host'], collection['host_kind'], collection['ready']), (URL, 'ollama', 1))
                self.assertEqual((index['host'], index['host_kind'], index['status']), (URL, 'ollama', 'complete'))
                self.assertEqual(db.knowledge.knowledge_vector('chunk'), vector_blob([1, 0]))
                # The chat's options are not rewritten; a missing type reads as Ollama.
                saved = db.chats.get_chat('chat')['options']['knowledge']
                self.assertEqual(saved, chosen)
                self.assertNotIn('host_kind', saved)
                self.assertEqual(dict(copy.deepcopy(DEFAULT_RAG), **saved)['host_kind'], 'ollama')
                # Another type that embeds, listed first at the address, does not answer for them.
                context.storage.delete_host('embed')
                context.storage.save_host('Prints', URL, provider='fingerprint')
                ollama = context.storage.add_host('Embedding host', URL)
                with provider_patch('list_models', side_effect=tags), provider_patch('embed', return_value={'embeddings': [[1, 0]]}) as embed:
                    snapshot = context.knowledge.retrieve(saved, 'Kept', Gio.Cancellable())
                self.assertEqual(snapshot['hits'][0]['document_id'], document['id'])
                self.assertEqual((embed.call_args.args[0].kind, embed.call_args.args[0].host_id), ('ollama', ollama['id']))
                self.assertEqual(context.knowledge.require_host(index['host'], index['host_kind']).host_id, ollama['id'])
            finally:
                context.knowledge.shutdown()
                context.shutdown()
            self.assertIsNone(DatabaseManager(str(path)).backup_path)

    def test_failed_upgrade_leaves_the_old_schema_and_a_backup_then_retries(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'gnollama.db'
            self.old_database(path)
            def fail(conn, progress=None):
                conn.execute("ALTER TABLE knowledge_collections ADD COLUMN host_kind TEXT NOT NULL DEFAULT 'ollama'")
                raise ValueError('Injected migration failure')
            with patch.object(database, 'MIGRATIONS', database.MIGRATIONS[:11] + [fail]):
                with self.assertRaisesRegex(DatabaseUpgradeError, 'Injected') as caught:
                    DatabaseManager(str(path))
            self.assertTrue(Path(caught.exception.backup_path).exists())
            with sqlite3.connect(path) as conn:
                self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 12)
                self.assertNotIn('host_kind', self.columns(conn, 'knowledge_collections'))
            upgraded = DatabaseManager(str(path))
            self.assertEqual(upgraded.knowledge.knowledge_collections()[0]['host_kind'], 'ollama')
            upgraded.close()


@unittest.skipUnless(Gdk.Display.get_default(), 'GTK tests require a display')
class KnowledgeHostUITests(unittest.TestCase):
    make_window = test_ui.UITests.make_window

    def setUp(self):
        test_ui.UITests.setUp(self)
        self.stack.enter_context(patch.dict(PROVIDERS, TYPES))
        self.ollama, self.prints, self.limited = three_hosts(self.storage)
        self.pickers = []

    def tearDown(self):
        for picker in self.pickers:
            picker.stop()
        self.context.knowledge.cancel_all()
        pump_until(lambda: self.context.knowledge.idle)
        self.context.knowledge.shutdown()
        test_ui.UITests.tearDown(self)

    def picker(self, *args, **kwargs):
        picker = HostModels(self.context, *args, **kwargs)
        self.pickers.append(picker)
        self.settle(picker)
        return picker

    def settle(self, picker):
        pump_until(lambda: self.context.services.idle and picker.notice.get_text() != 'Checking embedding models…')

    def collection(self, id, kind=None):
        """A ready collection of one document, built on the host of type `kind` at the address."""
        document = make_document('Reference ' + id, 'Alpha reference for ' + id)
        config = new_config(TAG['name'], TAG['digest'])
        knowledge = self.storage.db.knowledge
        knowledge.add_knowledge_document(document)
        knowledge.add_embedding_config(config)
        value = dict(id=id, name=id, config_id=config['id'], host=URL, model=TAG['name'], chunk_size=1600,
                     overlap=200, created_at=time.time(), updated_at=time.time())
        index = dict(value, id=id + '-index', document_id=document['id'])
        if kind:
            value['host_kind'] = index['host_kind'] = kind
        knowledge.create_knowledge_collection(value, [document['id']])
        knowledge.begin_knowledge_index(index)
        knowledge.save_embedding_batch(index['id'], config['id'], 2, [
            dict(id=id + '-chunk', ordinal=0, start=0, end=5, vector=vector_blob([1, 0]))])
        knowledge.finish_knowledge_index(index['id'], 'complete')
        knowledge.prepare_collection_indexes(id)
        return document, config

    def test_picker_lists_through_the_provider_and_selects_by_type_and_address(self):
        def listed(host, **kwargs):
            return [dict(name='zeta', digest='z'), dict(name='Alpha', digest='a')] if host.kind == 'fingerprint' else []
        with provider_patch('list_embedding_models', side_effect=listed) as models, \
                provider_patch('list_models') as tags, provider_patch('show_model') as show:
            picker = self.picker(URL + '/', 'zeta', kind='fingerprint')
            # Every type that embeds is offered, by name; one that does not is left out.
            self.assertEqual([h['name'] for h in picker.hosts][-2:], ['Embedding host', 'Prints'])
            self.assertNotIn(self.limited['id'], [h['id'] for h in picker.hosts])
            self.assertEqual((picker.host(), picker.kind(), picker.connection().host_id), (URL, 'fingerprint', self.prints['id']))
            self.assertEqual([m['name'] for m in picker.models], ['Alpha', 'zeta'])
            self.assertEqual(picker.model(), dict(name='zeta', digest='z'))
            self.assertEqual(models.call_count, 1)
            self.assertIn(UNVERIFIED, picker.verification.get_text())
            self.assertTrue(picker.verification.get_visible())
            self.assertEqual(picker.notice.get_text(), '')
            # A stored host without a type is the Ollama one at the address.
            for stored in (self.picker(URL, 'zeta'), self.picker(URL, 'zeta', kind='ollama')):
                self.assertEqual((stored.kind(), stored.connection().host_id), ('ollama', self.ollama['id']))
                self.assertFalse(stored.verification.get_visible())
                self.assertIn('Download one in Manage Models', stored.notice.get_text())
            # A type no host has selects nothing new, and the first host stays chosen.
            self.assertEqual(self.picker(URL, 'zeta', kind='hosted').host_dropdown.get_selected(), 0)
            # The notices follow the host as it is changed.
            stored.select_host(URL, 'fingerprint')
            self.settle(stored)
            self.assertTrue(stored.verification.get_visible())
            stored.select_host(URL)
            self.settle(stored)
            self.assertFalse(stored.verification.get_visible())
            tags.assert_not_called()
            show.assert_not_called()

    def test_empty_list_says_what_the_host_type_allows(self):
        with provider_patch('list_embedding_models', return_value=[]):
            self.assertIn('Load one there', self.picker(URL, kind='fingerprint').notice.get_text())
            self.assertIn('Download one in Manage Models', self.picker(URL).notice.get_text())
        notice = HostModels._empty_notice
        self.assertIn('Download one in Manage Models', notice(None))
        self.assertIn('Try refreshing later', notice(HostedProvider(HostedProvider.fixed_url)))
        self.assertEqual(len({notice(None), notice(HostedProvider(HostedProvider.fixed_url)), notice(FingerprintProvider(URL))}), 3)

    def test_source_picker_follows_a_collections_type_and_keeps_it_in_the_chat_options(self):
        document, config = self.collection('prints', 'fingerprint')
        self.collection('plain')
        window, tab = self.make_window()
        with provider_patch('list_embedding_models', return_value=[TAG]), provider_patch('list_models', return_value=[TAG]), \
                provider_patch('embed', return_value={'embeddings': [[1, 0]]}) as embed, \
                provider_patch('embedding_context', return_value=None):
            picker = SourcePicker(self.context, {}, lambda value: None)
            self.pickers.append(picker.host_models)
            picker.present(window)
            self.settle(picker.host_models)
            for id, kind, host in (('prints', 'fingerprint', self.prints), ('plain', 'ollama', self.ollama)):
                check = picker.collection_checks[id][0]
                check.set_active(True)
                self.settle(picker.host_models)
                current = picker.current_options()
                self.assertEqual((current['host'], current['host_kind'], current['collection_ids']), (URL, kind, [id]))
                self.assertEqual(picker.host_models.connection().host_id, host['id'])
                snapshot = self.context.knowledge.retrieve(current, 'Alpha', Gio.Cancellable())
                self.assertEqual(snapshot['collections'][0]['id'], id)
                self.assertEqual(embed.call_args.args[0].host_id, host['id'])
                check.set_active(False)
            picker.close()
            # Options saved with a chat select the same host again.
            restored = SourcePicker(self.context, dict(current, host_kind='fingerprint'), lambda value: None)
            self.pickers.append(restored.host_models)
            self.settle(restored.host_models)
            self.assertEqual(restored.host_models.kind(), 'fingerprint')
            restored._disconnect_sources()
            restored._closed()
            tab.knowledge_control.load(dict(current, host_kind='fingerprint'))
            self.assertEqual(tab.read_draft()['settings']['knowledge']['host_kind'], 'fingerprint')
            tab.knowledge_control.load({k: v for k, v in current.items() if k != 'host_kind'})
            self.assertEqual(tab.knowledge_control.options['host_kind'], 'ollama')

    def test_details_and_editors_say_when_a_model_cannot_be_fully_verified(self):
        self.assertIn(UNVERIFIED, verification_notice(self.context, 'fingerprint'))
        for kind in ('ollama', None, 'unknown'):
            self.assertEqual(verification_notice(self.context, kind), '')
        document, config = self.collection('prints', 'fingerprint')
        plain, config = self.collection('plain')
        view = KnowledgeView(self.context)
        self.addCleanup(view._destroyed)
        with provider_patch('list_embedding_models', return_value=[TAG]), provider_patch('list_models', return_value=[TAG]):
            for id, shown in (('prints', True), ('plain', False)):
                view.open_collection(id)
                pump_until(lambda: view.detail_page.get_title() == id and self.context.services.idle)
                self.assertEqual(UNVERIFIED in texts(view.detail), shown)
            for source, shown, state in ((document, True, 'Original model available'), (plain, False, 'Original model available')):
                view.open_document(source['id'])
                pump_until(lambda: view.detail_page.get_title() == source['title'] and state in texts(view.detail))
                self.assertEqual(UNVERIFIED in texts(view.detail), shown)
            # The collection creator and the index creator say so where the model is chosen.
            creator = IndexDialog(self.context, document, lambda: None,
                                  self.storage.db.knowledge.knowledge_collection('prints'))
            self.pickers.append(creator.host_models)
            self.settle(creator.host_models)
            self.assertEqual(creator.host_models.kind(), 'fingerprint')
            self.assertIn(UNVERIFIED, texts(creator.box))
            with patch.object(self.context.knowledge, 'create_index') as create, patch.object(creator, 'close'):
                creator._start(lambda: None)
            self.assertEqual((create.call_args.args[1], create.call_args.kwargs['kind']), (URL, 'fingerprint'))
            creator._closed()
            # Changing a collection's model stores the type of the host chosen.
            with patch.object(view, 'present_dialog') as present:
                view._collection_host(self.storage.db.knowledge.knowledge_collections()[0])
            dialog = present.call_args.args[0]
            hosts = next(w for w in descendants(dialog.box) if isinstance(w, HostModels))
            self.pickers.append(hosts)
            self.settle(hosts)
            self.assertEqual(hosts.kind(), 'ollama')
            hosts.select_host(URL, 'fingerprint')
            self.settle(hosts)
            save = next(w for w in descendants(dialog.header) if isinstance(w, Gtk.Button) and w.get_label() == 'Save')
            with patch.object(dialog, 'close'):
                save.emit('clicked')
            pump_until(lambda: self.storage.writer.idle and self.context.services.idle)
            dialog._closed()
        saved = self.storage.db.knowledge.knowledge_collection('plain')
        self.assertEqual((saved['host'], saved['host_kind']), (URL, 'fingerprint'))


if __name__ == '__main__':
    unittest.main()
