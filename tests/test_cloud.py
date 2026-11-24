"""Cloud authentication, persistence, and GTK regressions without real credentials."""
import json
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch

from gi.repository import Adw, Gdk, Gio, GLib, Gtk
from src import session
from src.providers import CLOUD_URL, OllamaCloudProvider, OllamaProvider, ProviderError, RequestCancelled, transport
from provider_fixture import provider_patch
from src.credentials import CredentialStore, CredentialError
from src import credentials
from src.app_context import AppContext
from src.host_manager import HostEditDialog, HostManagerDialog
from src.model_manager import ModelManagerDialog
from src.widgets.options_panel import OptionsPanel
from src.widgets.knowledge_view import HostModels
from test_transport import Server
from test_ui import pump_until


class FakeCredentials(CredentialStore):
    def __init__(self, saved=None):
        super().__init__()
        self.saved = saved if saved is not None else {}
        self.unavailable = False

    def save(self, reference, key, cancellable=None):
        if self.unavailable:
            raise CredentialError('Keyring unavailable')
        self.saved[reference] = self.validate(key)

    def clear(self, reference, cancellable=None):
        if reference and self.unavailable:
            raise CredentialError('Keyring unavailable')
        self.saved.pop(reference, None)

    def lookup(self, host_id, reference, cancellable=None):
        if self.has_session(host_id):
            return self._session[host_id]
        if self.unavailable:
            raise CredentialError('Keyring unavailable')
        if reference in self.saved:
            return self.saved[reference]
        raise CredentialError('An API key is required')


class CredentialBackendTests(unittest.TestCase):
    def test_locked_key_cannot_be_silently_left_behind(self):
        store = CredentialStore()
        with patch.object(credentials.Secret, 'password_clear_sync', return_value=False), \
             patch.object(credentials.Secret, 'password_search_sync', return_value=[object()]):
            with self.assertRaisesRegex(CredentialError, 'Unlock'):
                store.clear('locked-reference')
        with patch.object(credentials.Secret, 'password_clear_sync', return_value=False), \
             patch.object(credentials.Secret, 'password_search_sync', return_value=[]):
            store.clear('already-removed')


class CloudStorageFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.keys = FakeCredentials()
        self.contexts = []
        self.context = self.open_context(self.keys)
        self.storage = self.context.storage

    def open_context(self, keys):
        context = AppContext(self.temp.name, credentials=keys)
        self.contexts.append(context)
        return context

    def tearDown(self):
        for context in self.contexts:
            context.shutdown()
        self.temp.cleanup()

    def cloud(self, **kwargs):
        return self.storage.save_host('Cloud', CLOUD_URL, provider='ollama_cloud',
                                      api_key='test-secret', **kwargs)


class CloudStorageTests(CloudStorageFixture, unittest.TestCase):
    def test_keys_are_separate_and_survive_restart(self):
        host = self.cloud(is_default=True)
        other = self.cloud()
        self.assertNotEqual(host['credential_id'], other['credential_id'])
        with self.storage.db._get_conn() as conn:
            self.assertNotIn('test-secret', '\n'.join(conn.iterdump()))
        restarted = self.open_context(FakeCredentials(self.keys.saved))
        connection = restarted.connection(restarted.storage.get_host(host['id']))
        self.assertEqual(connection.credentials.lookup(connection.host_id, connection.credential_id), 'test-secret')
        self.assertNotIn('test-secret', repr(connection))
        updated = self.storage.save_host('Renamed', CLOUD_URL, host_id=host['id'], provider='ollama_cloud')
        self.assertEqual(updated['credential_id'], host['credential_id'])
        self.storage.save_host('Renamed', CLOUD_URL, host_id=host['id'], provider='ollama_cloud', api_key='replacement')
        self.assertEqual(self.keys.lookup(host['id'], host['credential_id']), 'replacement')
        self.storage.delete_host(host['id'])
        self.assertNotIn(host['credential_id'], self.keys.saved)
        self.assertIn(other['credential_id'], self.keys.saved)

    def test_failure_session_fallback_and_restart(self):
        self.keys.unavailable = True
        before = self.storage.get_all_hosts()
        with self.assertRaises(CredentialError):
            self.cloud()
        self.assertEqual(self.storage.get_all_hosts(), before)
        host = self.cloud(session_only=True)
        self.assertIsNone(host['credential_id'])
        self.assertEqual(self.keys.lookup(host['id'], None), 'test-secret')
        restarted = self.open_context(FakeCredentials()).storage
        self.assertIsNotNone(restarted.get_host(host['id']))
        with self.assertRaises(CredentialError):
            restarted.credentials.lookup(host['id'], None)
        self.storage.delete_host(host['id'])
        self.assertFalse(self.keys.has_session(host['id']))

    def test_failed_replacement_or_deletion_keeps_saved_host(self):
        host = self.cloud()
        self.keys.unavailable = True
        with self.assertRaises(CredentialError):
            self.storage.save_host('Changed', CLOUD_URL, host_id=host['id'], provider='ollama_cloud', api_key='new-key')
        with self.assertRaises(CredentialError):
            self.storage.delete_host(host['id'])
        self.assertEqual(self.storage.get_host(host['id']), host)
        self.assertEqual(self.keys.saved[host['credential_id']], 'test-secret')
        self.storage.save_host('Changed', CLOUD_URL, host_id=host['id'], provider='ollama_cloud',
                               api_key='temporary', session_only=True)
        self.assertEqual(self.keys.lookup(host['id'], host['credential_id']), 'temporary')

    def test_change_to_server_removes_authentication(self):
        host = self.cloud()
        local = self.storage.save_host('Local', 'http://localhost:11434', host_id=host['id'])
        self.assertIsNone(local['credential_id'])
        self.assertEqual(self.keys.saved, {})
        self.assertEqual(self.context.connection(local).url, 'http://localhost:11434')

    def test_upgrade_from_v9_preserves_hosts_and_history(self):
        host = self.storage.get_all_hosts()[0]
        with self.storage.db._get_conn() as conn:
            conn.execute('ALTER TABLE hosts DROP COLUMN provider')
            conn.execute('ALTER TABLE hosts DROP COLUMN credential_id')
            from legacy_schema import remove_workspace
            remove_workspace(conn)
            conn.execute('PRAGMA user_version=9')
            conn.commit()
        upgraded = self.open_context(FakeCredentials()).storage
        self.assertEqual(upgraded.get_host(host['id']), host)
        with upgraded.db._get_conn() as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 12)
        self.assertIsNotNone(upgraded.db.backup_path)


class CloudTransportTests(unittest.TestCase):
    def setUp(self):
        self.server = Server('json')
        self.server.server.payload = {'models': [{'name': 'cloud-model'}], 'capabilities': ['completion']}
        self.addCleanup(self.server.close)
        self.keys = FakeCredentials({'key-reference': 'test-secret'})
        self.cloud = OllamaCloudProvider(CLOUD_URL, 'host', 'key-reference', credentials=self.keys)
        # Route the fixed cloud origin to an isolated HTTP fixture, preserving
        # actual Soup header, redirect, streaming, and cancellation behavior.
        factory = transport.Soup.Message.new
        self.destinations = []
        def message(method, url):
            self.destinations.append(url)
            return factory(method, url.replace(CLOUD_URL, self.server.host))
        patcher = patch.object(transport.Soup.Message, 'new', side_effect=message)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_authenticated_discovery_chat_response_and_local_isolation(self):
        self.assertEqual(self.cloud.list_models(), [{'name': 'cloud-model'}])
        self.cloud.show_model('cloud-model')
        self.server.server.mode = 'stream'
        list(self.cloud.chat('cloud-model', [{'role': 'user', 'content': 'hi'}], format='json', keep_alive=-1))
        list(self.cloud.generate('cloud-model', 'hi', format={}, keep_alive=0))
        self.assertTrue(all(h.get('Authorization') == 'Bearer test-secret' for h in self.server.server.headers_received))
        for _, body in self.server.server.received[-2:]:
            self.assertNotIn('format', body)
            self.assertNotIn('keep_alive', body)
            self.assertEqual(body['model'], 'cloud-model')
        list(OllamaProvider(self.server.host).generate('local', 'hi'))
        self.assertNotIn('Authorization', self.server.server.headers_received[-1])
        self.keys.set_session('other', 'other-secret')
        list(replace(self.cloud, host_id='other').generate('cloud-model', 'hi'))
        self.assertEqual(self.server.server.headers_received[-1]['Authorization'], 'Bearer other-secret')

    def test_rejects_other_origins_and_redirects(self):
        for url in ('http://ollama.com', 'https://example.com', 'https://ollama.com.evil', 'https://ollama.com/api'):
            with self.assertRaises(ProviderError):
                replace(self.cloud, url=url).list_models()
        self.assertEqual(self.destinations, [])
        self.server.server.status = 302
        self.server.server.redirect_location = self.server.host + '/redirected'
        with self.assertRaises(ProviderError):
            self.cloud.list_models()
        self.assertEqual(len(self.server.server.received), 1)

    def test_errors_are_actionable_and_redact_secrets(self):
        for status, expected in ((401, 'API key'), (403, 'API key'), (429, 'limit'), (503, 'service unavailable')):
            self.server.server.status = status
            self.server.server.payload = {'error': 'service unavailable test-secret'}
            with self.assertRaises(ProviderError) as raised:
                self.cloud.list_models()
            self.assertIn(expected, str(raised.exception))
            self.assertNotIn('test-secret', str(raised.exception))
            self.assertEqual(raised.exception.status, status)
        self.server.server.status = 200
        with self.assertRaises(ProviderError) as raised:
            list(self.cloud.generate('cloud-model', 'hi'))
        self.assertNotIn('test-secret', str(raised.exception))

    def test_missing_key_and_cancellation_make_no_request(self):
        self.keys.saved.clear()
        with self.assertRaisesRegex(ProviderError, 'API key'):
            self.cloud.list_models()
        cancel = Gio.Cancellable()
        cancel.cancel()
        with self.assertRaises(RequestCancelled):
            self.cloud.list_models(cancellable=cancel)
        self.assertEqual(self.server.server.received, [])

    def test_connection_is_not_serialized_into_chat_details(self):
        from test_session import settings
        state = session.RequestState(settings(), 'hi', connection=self.cloud)
        rendered = json.dumps(state.api_details())
        self.assertNotIn('test-secret', rendered)
        self.assertNotIn('key-reference', rendered)
        self.assertNotIn('credentials', rendered)


@unittest.skipUnless(Gdk.Display.get_default(), 'GTK smoke tests require a display')
class CloudUITests(CloudStorageFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.window = Adw.Window()
        self.window.set_content(Gtk.Box())
        self.window.present()

    def tearDown(self):
        self.window.destroy()
        pump_until(lambda: self.context.services.idle)
        super().tearDown()

    def test_editor_preserves_fields_on_keyring_failure_and_session_save(self):
        self.keys.unavailable = True
        manager = HostManagerDialog(self.context)
        manager.present(self.window)
        manager.show_edit_dialog()
        dialog = self.window.get_visible_dialog()
        self.assertIsInstance(dialog, HostEditDialog)
        dialog.provider_dropdown.set_selected(1)
        self.assertEqual(dialog.hostname_entry.get_text(), CLOUD_URL)
        self.assertFalse(dialog.hostname_entry.get_editable())
        self.assertFalse(dialog.get_response_enabled('save'))
        dialog.api_key_entry.set_text('session-secret')
        dialog.save_button.emit('clicked')
        pump_until(lambda: not dialog.busy)
        self.assertEqual(dialog.api_key_entry.get_text(), 'session-secret')
        self.assertTrue(dialog.session_button.get_visible())
        dialog.session_button.emit('clicked')
        pump_until(lambda: not dialog.busy)
        host = next(h for h in self.storage.get_all_hosts() if h['provider'] == 'ollama_cloud')
        self.assertTrue(self.keys.has_session(host['id']))
        manager.force_close()

    def test_cloud_options_preserve_local_settings(self):
        options = OptionsPanel()
        options.output_dropdown.set_selected(2)
        options.schema_text = '{"type":"object"}'
        options.keep_alive_dropdown.set_selected(4)
        options.set_capabilities(OllamaCloudProvider.capabilities)
        self.assertFalse(options.output_dropdown.get_sensitive())
        self.assertFalse(options.schema_button.get_visible())
        settings = options.get_request_settings()
        self.assertIsNone(settings['format'])
        self.assertIsNone(settings['keep_alive'])
        options.set_capabilities(OllamaProvider.capabilities)
        self.assertEqual(options.get_request_settings()['format'], {'type': 'object'})
        self.assertEqual(options.get_keep_alive(), -1)

    def test_cloud_model_manager_and_embedding_hosts(self):
        cloud = self.cloud(is_default=True)
        with provider_patch('list_models', return_value=[{'name': 'cloud-model', 'details': None}]), \
             provider_patch('show_model', return_value={'capabilities': ['completion']}), \
             provider_patch('running') as running:
            manager = ModelManagerDialog(self.context)
            manager.present(self.window)
            pump_until(lambda: self.context.services.idle)
            self.assertFalse(manager.pull_button.get_visible())
            page = manager.model_stack.get_page(manager.model_stack.get_child_by_name('running'))
            self.assertFalse(page.get_visible())
            self.assertIsNone(manager._poll_id)
            running.assert_not_called()
            self.assertEqual(len(manager.model_rows), 1)
            picker = HostModels(self.context)
            self.assertNotIn(cloud['id'], [h['id'] for h in picker.hosts])
            picker.stop()
            manager.force_close()

    def test_chat_and_response_tabs_use_independent_cloud_connections(self):
        from src.tab import GenerationTab
        first = self.cloud(is_default=True)
        second = self.storage.save_host('Second', CLOUD_URL, provider='ollama_cloud',
                                        api_key='second-secret', session_only=True)
        received = []
        def generate(host, **kwargs):
            received.append((host.host_id, host.credentials.lookup(host.host_id, host.credential_id)))
            yield {'response': 'answer', 'done': True}
        with provider_patch('list_models', return_value=['cloud-model']), \
             provider_patch('show_model', return_value={'capabilities': ['completion']}), \
             provider_patch('chat', side_effect=generate), \
             provider_patch('generate', side_effect=generate):
            saved = self.storage.create_chat()
            chat = GenerationTab(mode='chat', context=self.context, chat_id=saved['id'])
            response = GenerationTab(mode='generate', context=self.context)
            for i, host in enumerate(response.options_panel.host_list):
                if host['id'] == second['id']:
                    response.options_panel.host_dropdown.set_selected(i)
            pump_until(lambda: self.context.services.idle)
            for tab in (chat, response):
                tab.chat_input.entry.set_text('hello')
                tab.on_send_clicked()
            pump_until(lambda: chat.request is None and response.request is None and self.context.services.idle)
            pump_until(lambda: self.storage.writer.idle)
            self.assertCountEqual(received, [(first['id'], 'test-secret'), (second['id'], 'second-secret')])
            history = json.dumps(self.storage.get_chat(saved['id']))
            self.assertNotIn('test-secret', history)
            self.assertNotIn('second-secret', history)
            self.assertIn('answer', history)
            for tab in (chat, response):
                tab.chat_input.cancel_fetches()
                tab.knowledge_control.close_dialog(dispose=True)
