"""Cloud authentication, persistence, and GTK regressions without real credentials."""
import json
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch

from gi.repository import Adw, Gdk, Gio, Gtk
from src import session
from src.providers import (Capabilities, CLOUD_URL, LMStudioProvider, OllamaCloudProvider, OllamaProvider,
                           OpenAICompatibleProvider, OpenRouterProvider, PROVIDERS, ProviderError,
                           RequestCancelled, transport)
from provider_fixture import KEYED, OPTIONAL_KEY, OptionalKeyProvider, RequiredKeyProvider, provider_patch
from src.providers.base import same_address
from src.credentials import CredentialStore, CredentialError
from src import credentials
from src.app_context import AppContext
from src.host_manager import HostEditDialog, HostManagerDialog
from src.model_manager import ModelManagerDialog
from src.widgets.options_panel import OptionsPanel
from src.widgets.knowledge_common import HostModels
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
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 13)
        self.assertIsNotNone(upgraded.db.backup_path)


class OptionalKeyFixture(CloudStorageFixture):
    URL = OptionalKeyProvider.default_url
    OTHER = 'http://other.lan:1234'
    KINDS = OPTIONAL_KEY

    def setUp(self):
        super().setUp()
        registered = patch.dict(PROVIDERS, self.KINDS)
        registered.start()
        self.addCleanup(registered.stop)

    def optional(self, api_key='', host_id=None, **kwargs):
        return self.storage.save_host('Optional', self.URL, provider='optional_key', api_key=api_key,
                                      host_id=host_id, **kwargs)

    def required(self, host_id, url=None, **kwargs):
        return self.storage.save_host('Required', url or self.URL, provider='required_key',
                                      host_id=host_id, **kwargs)


class OptionalKeyStorageTests(OptionalKeyFixture, unittest.TestCase):
    def test_key_is_never_demanded_and_is_kept_out_of_the_database(self):
        bare = self.optional()
        self.assertIsNone(bare['credential_id'])
        self.assertEqual(bare['provider'], 'optional_key')
        self.assertEqual(self.keys.saved, {})
        with self.assertRaises(CredentialError):
            self.optional('not a key')
        keyed = self.optional('test-secret')
        self.assertEqual(self.keys.saved, {keyed['credential_id']: 'test-secret'})
        with self.storage.db._get_conn() as conn:
            self.assertNotIn('test-secret', '\n'.join(conn.iterdump()))
        added = self.optional('added-later', host_id=bare['id'])
        self.assertEqual(self.keys.saved[added['credential_id']], 'added-later')
        self.storage.delete_host(keyed['id'])
        self.assertEqual(list(self.keys.saved), [added['credential_id']])

    def test_blank_keeps_the_key_and_only_removal_clears_it(self):
        host = self.optional('test-secret')
        renamed = self.storage.save_host('Renamed', 'http://localhost:4321', host_id=host['id'], provider='optional_key')
        self.assertEqual(renamed['credential_id'], host['credential_id'])
        self.assertEqual(self.keys.saved[host['credential_id']], 'test-secret')
        self.optional('replacement', host_id=host['id'])
        self.assertEqual(self.keys.saved, {host['credential_id']: 'replacement'})
        removed = self.optional(host_id=host['id'], clear_key=True)
        self.assertIsNone(removed['credential_id'])
        self.assertEqual(self.keys.saved, {})
        # Removing a key that is not there is not an error.
        self.assertIsNone(self.optional(host_id=host['id'], clear_key=True)['credential_id'])

    def test_removal_is_ignored_where_a_key_is_required(self):
        host = self.cloud()
        saved = self.storage.save_host('Cloud', CLOUD_URL, host_id=host['id'], provider='ollama_cloud', clear_key=True)
        self.assertEqual(saved['credential_id'], host['credential_id'])
        self.assertEqual(self.keys.saved[host['credential_id']], 'test-secret')

    def test_session_only_key(self):
        self.keys.unavailable = True
        before = self.storage.get_all_hosts()
        with self.assertRaises(CredentialError):
            self.optional('test-secret')
        self.assertEqual(self.storage.get_all_hosts(), before)
        host = self.optional('test-secret', session_only=True)
        self.assertIsNone(host['credential_id'])
        self.assertEqual(self.keys.lookup(host['id'], None), 'test-secret')
        self.assertEqual(self.keys.saved, {})
        # A blank save keeps it; removal forgets it.
        self.optional(host_id=host['id'])
        self.assertTrue(self.keys.has_session(host['id']))
        self.optional(host_id=host['id'], clear_key=True)
        self.assertFalse(self.keys.has_session(host['id']))

    def test_keyring_failure_leaves_the_host_unchanged(self):
        host = self.optional('test-secret')
        self.keys.unavailable = True
        for change in (dict(api_key='new-key'), dict(clear_key=True)):
            with self.subTest(change=change), self.assertRaises(CredentialError):
                self.storage.save_host('Changed', 'http://localhost:4321', host_id=host['id'],
                                       provider='optional_key', **change)
        with self.assertRaises(CredentialError):
            self.storage.save_host('Changed', self.URL, host_id=host['id'])
        self.assertEqual(self.storage.get_host(host['id']), host)
        self.assertEqual(self.keys.saved, {host['credential_id']: 'test-secret'})
        # Without a keyring a host can still be saved with no key, or a session one.
        self.assertIsNone(self.optional()['credential_id'])
        self.optional('temporary', host_id=host['id'], session_only=True)
        self.assertEqual(self.keys.lookup(host['id'], host['credential_id']), 'temporary')

    def test_changing_type_never_carries_a_key_to_another_address(self):
        # To a type without a key, then between types whose addresses differ.
        host = self.optional('test-secret')
        local = self.storage.save_host('Local', self.URL, host_id=host['id'])
        self.assertIsNone(local['credential_id'])
        self.assertEqual(self.keys.saved, {})
        # Back to the optional type: nothing to restore, nothing demanded.
        self.assertIsNone(self.optional(host_id=host['id'])['credential_id'])
        # To a type that requires a key: the old one does not count.
        self.optional('test-secret', host_id=host['id'])
        with self.assertRaises(CredentialError):
            self.storage.save_host('Cloud', CLOUD_URL, host_id=host['id'], provider='ollama_cloud')
        self.assertEqual(self.storage.get_host(host['id'])['provider'], 'optional_key')
        self.assertEqual(list(self.keys.saved.values()), ['test-secret'])
        cloud = self.storage.save_host('Cloud', CLOUD_URL, host_id=host['id'], provider='ollama_cloud', api_key='cloud-secret')
        self.assertEqual(self.keys.saved, {cloud['credential_id']: 'cloud-secret'})
        # And from it: the cloud key is not sent to the other server.
        back = self.optional(host_id=host['id'])
        self.assertIsNone(back['credential_id'])
        self.assertEqual(self.keys.saved, {})
        self.keys.set_session(host['id'], 'session-secret')
        self.storage.save_host('Cloud', CLOUD_URL, host_id=host['id'], provider='ollama_cloud',
                               api_key='cloud-secret', session_only=True)
        self.optional(host_id=host['id'])
        self.assertFalse(self.keys.has_session(host['id']))


class KeyOnTypeChangeTests(OptionalKeyFixture, unittest.TestCase):
    KINDS = KEYED

    def test_addresses_are_the_same_only_when_that_is_certain(self):
        for first, second in (('http://h:1234', 'http://h:1234/'), ('HTTP://Host.LAN:1234', 'http://host.lan:1234'),
                              ('http://h', 'http://h:80'), ('https://h:443/', 'https://h'),
                              ('http://h/api/', 'http://h/api'), ('http://[::1]:1234', 'http://[::1]:1234/')):
            with self.subTest(first=first, second=second):
                self.assertTrue(same_address(first, second))
                self.assertTrue(same_address(second, first))
        for first, second in (('http://h:1234', 'http://h:1234/v1'), ('http://h:1234', 'https://h:1234'),
                              ('http://h:1234', 'http://h:4321'), ('http://h', 'http://h:8080'),
                              ('http://h/API', 'http://h/api'), ('http://user@h', 'http://h'),
                              ('http://h', 'http://h.lan'), ('http://localhost', 'http://127.0.0.1'),
                              ('', ''), ('not a url', 'not a url'), ('ftp://h', 'ftp://h'),
                              ('http://h:bad', 'http://h:bad'), ('http://h?a=1', 'http://h?a=1'),
                              (None, 'http://h'), (None, None)):
            with self.subTest(first=first, second=second):
                self.assertFalse(same_address(first, second))
                self.assertFalse(same_address(second, first))

    def test_v1_ending_is_one_address_only_between_types_that_add_it_themselves(self):
        for first, second in (('http://h:1234', 'http://h:1234/v1'), ('http://h:1234/v1/', 'http://H:1234'),
                              ('http://h/api/v1', 'http://h/api'), ('http://h/v1', 'http://h/v1')):
            with self.subTest(first=first, second=second):
                self.assertTrue(same_address(first, second, '/v1'))
                self.assertTrue(same_address(second, first, '/v1'))
        for first, second in (('http://h:1234', 'http://h:1234/v2'), ('http://h:1234/v1/v1', 'http://h:1234'),
                              ('http://h:1234/V1', 'http://h:1234'), ('http://h:1234//v1', 'http://h:1234'),
                              ('http://h:1234/v1', 'http://other:1234'), ('http://h/v1?a=1', 'http://h')):
            with self.subTest(first=first, second=second):
                self.assertFalse(same_address(first, second, '/v1'))
                self.assertFalse(same_address(second, first, '/v1'))
        generic = dict(provider='openai', hostname='http://h:1234/v1')
        studio = dict(provider='lmstudio', hostname='http://h:1234')
        self.assertTrue(LMStudioProvider.keeps_key(generic, 'http://h:1234'))
        self.assertTrue(OpenAICompatibleProvider.keeps_key(studio, 'http://h:1234/v1/'))
        self.assertFalse(LMStudioProvider.keeps_key(generic, 'http://h:4321'))
        self.assertFalse(OpenRouterProvider.keeps_key(generic, OpenRouterProvider.fixed_url))
        # To a type that sends the address as it is given, the ending is another place.
        self.assertFalse(RequiredKeyProvider.keeps_key(generic, 'http://h:1234'))
        for kind in ('required_key', 'gone', None):
            with self.subTest(kind=kind):
                self.assertFalse(LMStudioProvider.keeps_key(dict(generic, provider=kind), 'http://h:1234'))
        host = self.storage.save_host('Generic', self.URL + '/v1', provider='openai', api_key='test-secret')
        moved = self.storage.save_host('Studio', self.URL, host_id=host['id'], provider='lmstudio')
        self.assertEqual((moved['provider'], moved['hostname'], moved['credential_id']),
                         ('lmstudio', self.URL, host['credential_id']))
        self.assertEqual(self.keys.saved, {host['credential_id']: 'test-secret'})

    def test_stored_key_follows_the_host_to_a_keyed_type_at_the_same_address(self):
        host = self.optional('test-secret')
        kept = {host['credential_id']: 'test-secret'}
        required = self.required(host['id'])  # Not demanded again.
        self.assertEqual((required['provider'], required['credential_id']), ('required_key', host['credential_id']))
        self.assertEqual(self.keys.saved, kept)
        self.assertEqual(self.optional(host_id=host['id'])['credential_id'], host['credential_id'])
        # A trailing slash, the case of the host name, and a default port are the same address.
        slashed = self.required(host['id'], self.URL + '/')
        self.assertEqual((slashed['hostname'], slashed['credential_id']), (self.URL, host['credential_id']))
        self.optional(host_id=host['id'])
        self.assertEqual(self.required(host['id'], 'http://LOCALHOST:1234')['credential_id'], host['credential_id'])
        self.assertEqual(self.keys.saved, kept)
        connection = self.context.connection(self.storage.get_host(host['id']))
        self.assertEqual(self.keys.lookup(connection.host_id, connection.credential_id), 'test-secret')

    def test_session_key_follows_the_host_to_a_keyed_type_at_the_same_address(self):
        host = self.optional('session-secret', session_only=True)
        required = self.required(host['id'])
        self.assertEqual((required['provider'], required['credential_id']), ('required_key', None))
        self.assertEqual(self.keys.lookup(host['id'], None), 'session-secret')
        self.optional(host_id=host['id'])
        self.assertTrue(self.keys.has_session(host['id']))
        self.assertEqual(self.keys.saved, {})

    def test_key_is_dropped_for_a_type_that_takes_none(self):
        host = self.optional('test-secret')
        self.keys.set_session(host['id'], 'session-secret')
        local = self.storage.save_host('Local', self.URL, host_id=host['id'])
        self.assertEqual((local['provider'], local['credential_id']), ('ollama', None))
        self.assertEqual(self.keys.saved, {})
        self.assertFalse(self.keys.has_session(host['id']))
        # Nothing is left to follow it back.
        with self.assertRaisesRegex(CredentialError, 'An API key is required'):
            self.required(host['id'])
        self.assertEqual(self.storage.get_host(host['id']), local)

    def test_key_is_dropped_when_the_address_changes_with_the_type(self):
        for other in (self.OTHER, self.URL + '/v1', self.URL.replace('http:', 'https:')):
            with self.subTest(other=other):
                host = self.required(None, api_key='test-secret')
                self.keys.set_session(host['id'], 'session-secret')
                moved = self.storage.save_host('Moved', other, host_id=host['id'], provider='optional_key')
                self.assertEqual((moved['provider'], moved['hostname'], moved['credential_id']),
                                 ('optional_key', other, None))
                self.assertEqual(self.keys.saved, {})
                self.assertFalse(self.keys.has_session(host['id']))
        # A type that requires a key does not count the one left at the old address.
        for session_only in (False, True):
            with self.subTest(session_only=session_only):
                host = self.optional('test-secret', session_only=session_only)
                saved = dict(self.keys.saved)
                with self.assertRaisesRegex(CredentialError, 'An API key is required'):
                    self.required(host['id'], self.OTHER)
                self.assertEqual(self.storage.get_host(host['id']), host)
                self.assertEqual(self.keys.saved, saved)
                self.assertEqual(self.keys.lookup(host['id'], host['credential_id']), 'test-secret')
                self.storage.delete_host(host['id'])

    def test_new_key_replaces_the_old_one_on_a_type_change(self):
        host = self.optional('test-secret')
        same = self.required(host['id'], api_key='second-secret')
        self.assertEqual(self.keys.saved, {same['credential_id']: 'second-secret'})
        moved = self.storage.save_host('Moved', self.OTHER, host_id=host['id'], provider='optional_key',
                                       api_key='third-secret')
        self.assertEqual(self.keys.saved, {moved['credential_id']: 'third-secret'})
        # A session key entered on moving leaves no stored key for the old address.
        session = self.required(host['id'], api_key='fourth-secret', session_only=True)
        self.assertIsNone(session['credential_id'])
        self.assertEqual(self.keys.saved, {})
        self.assertEqual(self.keys.lookup(host['id'], None), 'fourth-secret')
        # A stored key replaces a session key that followed the host.
        stored = self.optional('fifth-secret', host_id=host['id'])
        self.assertFalse(self.keys.has_session(host['id']))
        self.assertEqual(self.keys.saved, {stored['credential_id']: 'fifth-secret'})

    def test_required_type_demands_a_key_from_a_host_that_has_none(self):
        local = self.storage.get_all_hosts()[0]
        bare = self.optional()
        for host in (local, bare):
            with self.subTest(provider=host['provider']):
                with self.assertRaisesRegex(CredentialError, 'An API key is required'):
                    self.required(host['id'], host['hostname'])
                self.assertEqual(self.storage.get_host(host['id']), host)
        self.assertEqual(self.keys.saved, {})
        keyed = self.required(bare['id'], api_key='test-secret')
        self.assertEqual(self.keys.saved, {keyed['credential_id']: 'test-secret'})

    def test_removal_still_applies_on_a_type_change(self):
        host = self.optional('test-secret')
        # Ignored where the new type requires a key, as for an unchanged type.
        self.assertEqual(self.required(host['id'], clear_key=True)['credential_id'], host['credential_id'])
        removed = self.optional(host_id=host['id'], clear_key=True)
        self.assertIsNone(removed['credential_id'])
        self.assertEqual(self.keys.saved, {})

    def test_keyring_failure_on_a_type_change_leaves_the_host_unchanged(self):
        host = self.optional('test-secret')
        other = self.required(None, api_key='other-secret')
        self.keys.unavailable = True
        for change in (dict(provider='required_key', api_key='new-key'),             # replaced
                       dict(provider='required_key', api_key='new-key', url=self.OTHER),
                       dict(provider='ollama'),                                      # dropped: no key taken
                       dict(provider='optional_key', clear_key=True)):
            with self.subTest(change=change), self.assertRaises(CredentialError):
                self.storage.save_host('Changed', change.pop('url', self.URL), host_id=host['id'], **change)
        self.assertEqual(self.storage.get_host(host['id']), host)
        with self.assertRaises(CredentialError):  # Dropped with the address, which needs the keyring.
            self.storage.save_host('Changed', self.OTHER, host_id=other['id'], provider='optional_key')
        self.assertEqual(self.storage.get_host(other['id']), other)
        saved = {host['credential_id']: 'test-secret', other['credential_id']: 'other-secret'}
        self.assertEqual(self.keys.saved, saved)
        # A kept key does not touch the keyring, so the change is saved.
        self.assertEqual(self.required(host['id'])['credential_id'], host['credential_id'])
        self.assertEqual(self.optional(host_id=other['id'])['credential_id'], other['credential_id'])
        self.assertEqual(self.keys.saved, saved)


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
        # Both are kept for the chat's stored options, apart from what is sent.
        self.assertEqual(options.get_unsent(), dict(output_mode='schema', keep_alive=-1))
        options.keep_alive_dropdown.set_selected(5)
        options.keep_alive_entry.set_text('45')
        self.assertEqual(options.get_unsent(), dict(output_mode='schema', keep_alive='45'))
        options.keep_alive_dropdown.set_selected(4)
        options.set_capabilities(OllamaProvider.capabilities)
        self.assertEqual(options.get_unsent(), {})
        self.assertEqual(options.get_request_settings()['format'], {'type': 'object'})
        self.assertEqual(options.get_keep_alive(), -1)

    def test_options_a_host_lacks_are_disabled_explained_and_not_sent(self):
        options = OptionsPanel()
        fields = dict(top_k_entry='40', min_p_entry='0.05', num_ctx_entry='8192', top_p_entry='0.9')
        for name, text in fields.items():
            getattr(options, name).set_text(text)
        hints = {name: options.field_hints[name][0].get_text() for name in fields}
        for missing, names in (('sampling_extras', ('top_k_entry', 'min_p_entry')), ('context_size', ('num_ctx_entry',))):
            with self.subTest(missing=missing):
                options.set_capabilities(replace(OllamaProvider.capabilities, **{missing: False}))
                sent = options.get_options_from_ui()
                for name in fields:
                    lacking = name in names
                    self.assertEqual(getattr(options, name).get_parent().get_sensitive(), not lacking)
                    self.assertEqual(name.removesuffix('_entry') in sent, not lacking)
                    self.assertEqual(options.field_hints[name][0].get_text(),
                                     'Not available on this server.' if lacking else hints[name])
                    # The value waits in its field for a host that takes it.
                    self.assertEqual(getattr(options, name).get_text(), fields[name])
        # An invalid value in a field the host lacks does not block sending.
        options.set_capabilities(Capabilities())
        options.top_k_entry.set_text('not a number')
        self.assertEqual(options.get_options_from_ui(), {'top_p': 0.9})
        options.top_k_entry.set_text('40')
        # Tool calling and logprobs are disabled the same way, and what was ticked is held, not sent.
        options.tools_available = True
        options.tools_check.set_active(True)
        options.logprobs_check.set_active(True)
        options.top_logprobs_entry.set_text('3')
        options.set_capabilities(Capabilities())
        for widget in (options.tools_check, options.tools_button, options.logprobs_check,
                       options.top_logprobs_entry.get_parent()):
            self.assertFalse(widget.get_sensitive())
        self.assertEqual((options.tools_notice.get_text(), options.field_hints['top_logprobs_entry'][0].get_text()),
                         ('Not available on this server.',) * 2)
        self.assertEqual((options.get_tools_options()['tools_enabled'], options.logprobs_enabled(),
                          options.get_logprobs()), (False, False, None))
        self.assertEqual(options.get_unsent(), dict(top_k='40', min_p='0.05', num_ctx='8192', logprobs=True,
                                                    top_logprobs='3', tools_enabled=True))
        options.tools_check.set_active(False)
        options.logprobs_check.set_active(False)
        options.set_capabilities(OllamaProvider.capabilities)
        self.assertEqual(options.get_options_from_ui(), dict(top_k=40, min_p=0.05, num_ctx=8192, top_p=0.9))
        self.assertEqual({name: options.field_hints[name][0].get_text() for name in fields}, hints)

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


@unittest.skipUnless(Gdk.Display.get_default(), 'GTK smoke tests require a display')
class HostEditorTests(OptionalKeyFixture, unittest.TestCase):
    KINDS = KEYED

    def setUp(self):
        super().setUp()
        self.window = Adw.Window()
        self.window.set_content(Gtk.Box())
        self.window.present()

    def tearDown(self):
        self.window.destroy()
        pump_until(lambda: self.context.services.idle)
        super().tearDown()

    def select(self, dialog, kind):
        dialog.provider_dropdown.set_selected(list(PROVIDERS).index(kind))

    def shown(self, dialog):
        return dict(key=dialog.key_box.get_visible(), link=dialog.key_link.get_visible(),
                    remove=dialog.remove_key_check.get_visible(), hint=dialog.key_hint.get_text(),
                    editable=dialog.hostname_entry.get_editable(), save=dialog.get_response_enabled('save'))

    def edit(self, manager, host=None):
        manager.show_edit_dialog(host)
        dialog = self.window.get_visible_dialog()
        self.assertIsInstance(dialog, HostEditDialog)
        return dialog

    def test_fields_follow_the_selected_type(self):
        dialog = HostEditDialog()
        model = dialog.provider_dropdown.get_model()
        labels = [model.get_string(i) for i in range(model.get_n_items())]
        self.assertEqual(labels, [kind.label for kind in PROVIDERS.values()])  # In registration order.
        self.assertEqual(labels[:2] + labels[-1:], ['Ollama Server', 'Ollama Cloud', 'Optional Key Server'])
        self.assertEqual(dialog.provider, 'ollama')
        self.assertEqual(self.shown(dialog), dict(key=False, link=False, remove=False, editable=True, save=False,
                                                  hint=dialog.key_hint.get_text()))
        self.assertEqual(dialog.hostname_entry.get_text(), '')

        self.select(dialog, 'optional_key')
        self.assertEqual(dialog.hostname_entry.get_text(), self.URL)
        self.assertEqual(dialog.name_entry.get_text(), '')
        self.assertEqual(self.shown(dialog), dict(key=True, link=False, remove=False, editable=True, save=False,
                                                  hint='Optional. Stored securely in your desktop keyring.'))
        dialog.name_entry.set_text('LAN')
        self.assertTrue(dialog.get_response_enabled('save'))  # No key is demanded.
        dialog.api_key_entry.set_text('not a key')
        self.assertFalse(dialog.get_response_enabled('save'))
        dialog.api_key_entry.set_text('test-secret')
        self.assertTrue(dialog.get_response_enabled('save'))

        self.select(dialog, 'ollama_cloud')
        self.assertEqual(dialog.hostname_entry.get_text(), CLOUD_URL)
        self.assertEqual(dialog.name_entry.get_text(), 'LAN')
        self.assertEqual(dialog.key_link.get_uri(), 'https://ollama.com/settings/keys')
        self.assertEqual(self.shown(dialog), dict(key=True, link=True, remove=False, editable=False, save=True,
                                                  hint='Stored securely in your desktop keyring.'))
        dialog.api_key_entry.set_text('')
        self.assertFalse(dialog.get_response_enabled('save'))

        # A suggested address gives way to the next type; a typed one is remembered.
        self.select(dialog, 'ollama')
        self.assertEqual(dialog.hostname_entry.get_text(), '')
        self.assertEqual(dialog.api_key, '')
        dialog.hostname_entry.set_text('http://gpu.lan:8000')
        for kind in ('ollama_cloud', 'optional_key', 'ollama_cloud', 'ollama'):
            self.select(dialog, kind)
        self.assertEqual(dialog.hostname_entry.get_text(), 'http://gpu.lan:8000')
        self.assertTrue(dialog.hostname_entry.get_editable())

        named = HostEditDialog()
        self.select(named, 'ollama_cloud')
        self.assertEqual(named.name_entry.get_text(), 'Ollama Cloud')

    def test_existing_ollama_hosts_edit_as_before(self):
        local = self.storage.get_all_hosts()[0]
        dialog = HostEditDialog(host=local)
        self.assertEqual((dialog.provider, dialog.name_entry.get_text(), dialog.hostname_entry.get_text()),
                         ('ollama', local['name'], local['hostname']))
        self.assertEqual(self.shown(dialog), dict(key=False, link=False, remove=False, editable=True, save=True,
                                                  hint=dialog.key_hint.get_text()))
        self.select(dialog, 'ollama_cloud')
        self.assertFalse(dialog.get_response_enabled('save'))
        self.select(dialog, 'ollama')
        self.assertEqual(dialog.hostname_entry.get_text(), local['hostname'])

        cloud = self.cloud()
        dialog = HostEditDialog(host=cloud)
        self.assertEqual((dialog.provider, dialog.name_entry.get_text(), dialog.hostname_entry.get_text()),
                         ('ollama_cloud', 'Cloud', CLOUD_URL))
        self.assertEqual(self.shown(dialog), dict(key=True, link=True, remove=False, editable=False, save=True,
                                                  hint='Leave blank to keep the existing key.'))
        self.assertFalse(dialog.clear_key)
        # Its key is not offered to a type at another address, and is still there on coming back.
        self.select(dialog, 'optional_key')
        self.assertEqual(self.shown(dialog)['hint'], 'Optional. Stored securely in your desktop keyring.')
        self.assertFalse(dialog.remove_key_check.get_visible())
        self.select(dialog, 'ollama_cloud')
        self.assertEqual(self.shown(dialog)['hint'], 'Leave blank to keep the existing key.')
        self.assertTrue(dialog.get_response_enabled('save'))
        session = HostEditDialog(host=dict(cloud, credential_id=None), has_session=True)
        self.assertTrue(session.get_response_enabled('save'))
        self.assertFalse(HostEditDialog(host=dict(cloud, credential_id=None)).get_response_enabled('save'))

    def test_kept_key_follows_the_type_and_the_address(self):
        keep = 'Leave blank to keep the existing key.'
        host = self.optional('test-secret')
        dialog = HostEditDialog(host=host)
        self.assertEqual(self.shown(dialog), dict(key=True, link=False, remove=True, editable=True, save=True, hint=keep))
        # Another keyed type at the same address keeps the key and demands none.
        self.select(dialog, 'required_key')
        self.assertEqual(dialog.hostname_entry.get_text(), self.URL)
        self.assertTrue(dialog.keeps_key)
        self.assertEqual(self.shown(dialog), dict(key=True, link=False, remove=False, editable=True, save=True, hint=keep))
        # The address is part of the rule, as it is typed.
        dialog.hostname_entry.set_text(self.OTHER)
        self.assertFalse(dialog.keeps_key)
        self.assertEqual(self.shown(dialog), dict(key=True, link=False, remove=False, editable=True, save=False,
                                                  hint='Stored securely in your desktop keyring.'))
        dialog.api_key_entry.set_text('new-secret')
        self.assertTrue(dialog.get_response_enabled('save'))
        dialog.api_key_entry.set_text('')
        self.assertFalse(dialog.get_response_enabled('save'))
        for same in (self.URL + '/', 'http://LOCALHOST:1234', self.URL):
            dialog.hostname_entry.set_text(same)
            self.assertEqual(self.shown(dialog), dict(key=True, link=False, remove=False, editable=True, save=True, hint=keep))
        dialog.hostname_entry.set_text(self.URL + '/v1')
        self.assertFalse(dialog.get_response_enabled('save'))
        # The saved type keeps its key at any address, as storage does.
        self.select(dialog, 'optional_key')
        self.assertEqual(self.shown(dialog), dict(key=True, link=False, remove=True, editable=True, save=True, hint=keep))
        # A fixed address elsewhere, and a type without a key.
        self.select(dialog, 'ollama_cloud')
        self.assertEqual(self.shown(dialog), dict(key=True, link=True, remove=False, editable=False, save=False,
                                                  hint='Stored securely in your desktop keyring.'))
        self.select(dialog, 'ollama')
        self.assertFalse(dialog.keeps_key)
        self.assertFalse(dialog.key_box.get_visible())

        # Removal is offered for an optional type the key would follow the host to.
        host = self.required(host['id'])
        dialog = HostEditDialog(host=host)
        self.assertEqual(self.shown(dialog), dict(key=True, link=False, remove=False, editable=True, save=True, hint=keep))
        self.select(dialog, 'optional_key')
        self.assertEqual(self.shown(dialog), dict(key=True, link=False, remove=True, editable=True, save=True, hint=keep))
        dialog.remove_key_check.set_active(True)
        self.assertTrue(dialog.clear_key)
        self.assertFalse(dialog.api_key_entry.get_sensitive())
        dialog.hostname_entry.set_text(self.OTHER)
        self.assertEqual(self.shown(dialog), dict(key=True, link=False, remove=False, editable=True, save=True,
                                                  hint='Optional. Stored securely in your desktop keyring.'))
        self.assertFalse(dialog.clear_key)
        self.assertTrue(dialog.api_key_entry.get_sensitive())
        dialog.hostname_entry.set_text(self.URL)
        self.assertTrue(dialog.remove_key_check.get_visible())
        self.assertFalse(dialog.remove_key_check.get_active())

        # A session key counts as a stored one does; a host without a key has none to keep.
        session = HostEditDialog(host=dict(host, provider='optional_key', credential_id=None), has_session=True)
        self.select(session, 'required_key')
        self.assertEqual(self.shown(session)['hint'], keep)
        self.assertTrue(session.get_response_enabled('save'))
        session.hostname_entry.set_text(self.OTHER)
        self.assertFalse(session.get_response_enabled('save'))
        bare = HostEditDialog(host=dict(host, provider='optional_key', credential_id=None))
        self.select(bare, 'required_key')
        self.assertEqual(self.shown(bare), dict(key=True, link=False, remove=False, editable=True, save=False,
                                                hint='Stored securely in your desktop keyring.'))

    def test_type_change_through_the_editor_keeps_the_key_storage_keeps(self):
        host = self.optional('test-secret')
        manager = HostManagerDialog(self.context)
        manager.present(self.window)
        dialog = self.edit(manager, host)
        self.select(dialog, 'required_key')
        self.assertTrue(dialog.get_response_enabled('save'))
        dialog.save_button.emit('clicked')
        pump_until(lambda: self.storage.get_host(host['id'])['provider'] == 'required_key')
        pump_until(lambda: not dialog.busy)
        saved = self.storage.get_host(host['id'])
        self.assertEqual(self.keys.saved, {saved['credential_id']: 'test-secret'})
        self.assertEqual(saved['credential_id'], host['credential_id'])

        # At another address the editor asks for a key, and the old one is replaced.
        dialog = self.edit(manager, saved)
        self.select(dialog, 'optional_key')
        dialog.hostname_entry.set_text(self.OTHER)
        self.assertFalse(dialog.remove_key_check.get_visible())
        dialog.save_button.emit('clicked')
        pump_until(lambda: self.storage.get_host(host['id'])['provider'] == 'optional_key')
        pump_until(lambda: not dialog.busy)
        self.assertEqual((self.storage.get_host(host['id'])['hostname'], self.storage.get_host(host['id'])['credential_id']),
                         (self.OTHER, None))
        self.assertEqual(self.keys.saved, {})
        manager.force_close()

    def test_placeholders_follow_the_selected_type(self):
        dialog = HostEditDialog()
        expected = dict(ollama=('For example: Local Ollama', 'http://localhost:11434'),
                        ollama_cloud=('Ollama Cloud', ''),
                        openai=('For example: Local Server', 'http://localhost:8080'),
                        lmstudio=('LM Studio', 'http://localhost:1234'), openrouter=('OpenRouter', ''),
                        vercel=('Vercel AI Gateway', ''),
                        required_key=('For example: Local Ollama', 'http://localhost:11434'),  # Inherited.
                        optional_key=('', ''))
        self.assertEqual(set(expected), set(PROVIDERS))
        for kind in list(expected) + ['ollama']:
            with self.subTest(kind=kind):
                self.select(dialog, kind)
                self.assertEqual((dialog.name_entry.get_placeholder_text() or '',
                                  dialog.hostname_entry.get_placeholder_text() or ''), expected[kind])
                self.assertEqual((PROVIDERS[kind].name_example, PROVIDERS[kind].url_example), expected[kind])
        # An existing host shows its own type's examples.
        cloud = HostEditDialog(host=self.cloud())
        self.assertEqual(cloud.name_entry.get_placeholder_text(), 'Ollama Cloud')
        self.assertFalse(cloud.hostname_entry.get_placeholder_text())

    def test_optional_key_is_saved_kept_and_removed_through_the_editor(self):
        manager = HostManagerDialog(self.context)
        manager.present(self.window)
        dialog = self.edit(manager)
        self.select(dialog, 'optional_key')
        dialog.name_entry.set_text('LAN')
        dialog.save_button.emit('clicked')
        pump_until(lambda: any(h['name'] == 'LAN' for h in self.storage.get_all_hosts()))
        pump_until(lambda: not dialog.busy)
        host = next(h for h in self.storage.get_all_hosts() if h['name'] == 'LAN')
        self.assertEqual((host['provider'], host['hostname'], host['credential_id']),
                         ('optional_key', self.URL, None))

        dialog = self.edit(manager, host)
        self.assertEqual(dialog.provider, 'optional_key')
        self.assertFalse(dialog.remove_key_check.get_visible())
        dialog.api_key_entry.set_text('test-secret')
        dialog.save_button.emit('clicked')
        pump_until(lambda: self.keys.saved)
        pump_until(lambda: not dialog.busy)
        host = self.storage.get_host(host['id'])
        self.assertEqual(self.keys.saved, {host['credential_id']: 'test-secret'})

        dialog = self.edit(manager, host)
        self.assertEqual(self.shown(dialog), dict(key=True, link=False, remove=True, editable=True, save=True,
                                                  hint='Leave blank to keep the existing key.'))
        dialog.name_entry.set_text('Renamed')
        dialog.save_button.emit('clicked')
        pump_until(lambda: self.storage.get_host(host['id'])['name'] == 'Renamed')
        pump_until(lambda: not dialog.busy)
        self.assertEqual(self.keys.saved, {host['credential_id']: 'test-secret'})

        dialog = self.edit(manager, self.storage.get_host(host['id']))
        dialog.api_key_entry.set_text('not a key')
        self.assertFalse(dialog.get_response_enabled('save'))
        dialog.remove_key_check.set_active(True)
        self.assertFalse(dialog.api_key_entry.get_sensitive())
        self.assertTrue(dialog.get_response_enabled('save'))  # What was typed is not used.
        dialog.save_button.emit('clicked')
        pump_until(lambda: not self.keys.saved)
        pump_until(lambda: not dialog.busy)
        self.assertIsNone(self.storage.get_host(host['id'])['credential_id'])
        manager.force_close()

    def test_optional_key_falls_back_to_the_session_on_keyring_failure(self):
        self.keys.unavailable = True
        manager = HostManagerDialog(self.context)
        manager.present(self.window)
        dialog = self.edit(manager)
        self.select(dialog, 'optional_key')
        dialog.name_entry.set_text('LAN')
        dialog.api_key_entry.set_text('session-secret')
        dialog.save_button.emit('clicked')
        pump_until(lambda: dialog.session_button.get_visible())
        self.assertEqual(dialog.api_key_entry.get_text(), 'session-secret')
        self.assertFalse(any(h['name'] == 'LAN' for h in self.storage.get_all_hosts()))
        dialog.session_button.emit('clicked')
        pump_until(lambda: any(h['name'] == 'LAN' for h in self.storage.get_all_hosts()))
        pump_until(lambda: not dialog.busy)
        host = next(h for h in self.storage.get_all_hosts() if h['name'] == 'LAN')
        self.assertIsNone(host['credential_id'])
        self.assertEqual(self.keys.lookup(host['id'], None), 'session-secret')
        # The session key counts as a key the host has, and can be removed.
        dialog = self.edit(manager, host)
        self.assertTrue(dialog.remove_key_check.get_visible())
        dialog.remove_key_check.set_active(True)
        dialog.save_button.emit('clicked')
        pump_until(lambda: not self.keys.has_session(host['id']))
        pump_until(lambda: not dialog.busy)
        manager.force_close()
