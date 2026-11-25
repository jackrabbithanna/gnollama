"""A host is its type and address: caches and reservations never mix two types at one address."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from gi.repository import Gio

from provider_fixture import LIMITED, OPTIONAL_KEY, limited_host, provider_patch
from src.chat_controller import ChatController
from src.comparison import ComparisonRun, prepare_run
from src.model_conversation import validate_participants
from src.providers import OllamaProvider
from src.providers.registry import PROVIDERS
from src.records import ModelTarget, RequestSettings
from src.services import host_key, model_key
import test_workspace

URL = 'http://localhost:11434'


def by_kind(host, **kwargs):
    """Each type lists a model the other lacks."""
    return ['shared', 'only-' + host.kind]


class HostIdentityTests(unittest.TestCase):
    setUp = test_workspace.WorkspaceTests.setUp
    tearDown = test_workspace.WorkspaceTests.tearDown

    def hosts(self):
        """The default Ollama host, a second record of it, and another type, all at one address."""
        local = self.storage.get_all_hosts()[0]
        # add_host keeps the address as typed, as an old record may hold it.
        return local, self.storage.add_host('Again', URL + '/'), limited_host(self.storage)

    def test_key_is_type_and_address_without_trailing_slash(self):
        with patch.dict(PROVIDERS, LIMITED):
            local, again, limited = self.hosts()
            self.assertEqual(host_key(local), ('ollama', URL))
            self.assertEqual(host_key(dict(hostname=URL + '/', provider=None)), ('ollama', URL))
            self.assertEqual(host_key(again), host_key(local))
            self.assertEqual(host_key(limited), ('limited', URL))
            for record in (local, again, limited):
                self.assertEqual(host_key(self.context.connection(record)), host_key(record))
            self.assertEqual(model_key(local, 'model:latest'), (('ollama', URL), 'model'))

    def test_catalog_keeps_lists_and_details_apart_for_types_at_one_address(self):
        catalog = self.context.services.catalog
        with patch.dict(PROVIDERS, LIMITED), provider_patch('list_models', side_effect=by_kind) as tags, \
                provider_patch('show_model', side_effect=lambda provider, name, **kw: {'capabilities': [provider.kind]}) as show:
            local, again, limited = (self.context.connection(h) for h in self.hosts())
            self.assertEqual([name for name, details in catalog.models(local)], ['shared', 'only-ollama'])
            self.assertEqual([name for name, details in catalog.models(limited)], ['shared', 'only-limited'])
            self.assertEqual(catalog.details(local, 'shared'), {'capabilities': ['ollama']})
            self.assertEqual(catalog.details(limited, 'shared'), {'capabilities': ['limited']})
            self.assertEqual(catalog.cached_details(local, 'shared'), {'capabilities': ['ollama']})
            self.assertEqual((tags.call_count, show.call_count), (2, 2))
            # Refreshing one leaves the other's cache alone.
            catalog.invalidate(limited)
            catalog.models(local)
            self.assertEqual(catalog.cached_details(local, 'shared'), {'capabilities': ['ollama']})
            self.assertIsNone(catalog.cached_details(limited, 'shared'))
            self.assertEqual(tags.call_count, 2)
            # One destination typed with and without the slash is one cache entry.
            catalog.models(OllamaProvider('http://models/'))
            catalog.models(OllamaProvider('http://models'))
            self.assertEqual(tags.call_count, 3)

    def test_reservation_blocks_the_same_type_at_the_address_and_no_other(self):
        models = self.context.services.models
        with patch.dict(PROVIDERS, LIMITED):
            local, again, limited = self.hosts()
            controller = SimpleNamespace(services=self.context.services)
            self.assertTrue(models.reserve(local, 'model'))
            try:
                # A second record of the type is the same server, however its address was typed.
                self.assertFalse(models.reserve(again, 'model:latest'))
                with self.assertRaisesRegex(ValueError, 'unloading'):
                    ChatController.check_target(controller, again, 'model')
                with self.assertRaisesRegex(ValueError, 'Wait for the model operation'):
                    with models.using(self.context.connection(again), 'model'):
                        pass
                self.assertEqual(ChatController.check_target(controller, limited, 'model'), URL)
                with models.using(self.context.connection(limited), 'model'):
                    self.assertTrue(models.busy(limited, 'model'))
                    self.assertFalse(models.busy(local, 'model'))
                    self.assertFalse(models.reserve(limited, 'model'))
                self.assertTrue(models.reserve(limited, 'model'))
                models.release(limited, 'model')
            finally:
                models.release(local, 'model')
            self.assertEqual(models.reserved, set())
            with models.using(self.context.connection(again), 'model'):
                self.assertTrue(models.busy(local, 'model'))
                self.assertFalse(models.busy(limited, 'model'))

    def test_window_counts_a_request_as_busy_only_for_its_own_type(self):
        from src.window import TureenWindow
        with patch.dict(PROVIDERS, LIMITED):
            local, again, limited = self.hosts()
            request = SimpleNamespace(connection=self.context.connection(limited),
                                      settings=SimpleNamespace(target=ModelTarget(limited['id'], URL, 'model')))
            window = SimpleNamespace(context=self.context, tabs=lambda: [SimpleNamespace(request=request)])
            self.assertTrue(TureenWindow.is_model_busy(window, limited, 'model'))
            self.assertFalse(TureenWindow.is_model_busy(window, local, 'model'))
            request.connection = self.context.connection(again)
            self.assertTrue(TureenWindow.is_model_busy(window, local, 'model:latest'))
            self.assertFalse(TureenWindow.is_model_busy(window, limited, 'model'))

    def test_validators_check_each_target_against_its_own_type(self):
        with patch.dict(PROVIDERS, LIMITED), provider_patch('list_models', side_effect=by_kind), \
                provider_patch('show_model', return_value={'capabilities': ['completion']}):
            local, again, limited = self.hosts()
            def compare(first, second):
                targets = [dict(host_id=local['id'], model=first), dict(host_id=limited['id'], model=second)]
                return prepare_run(self.context, ComparisonRun(), 'question', [], RequestSettings(), targets, None, 0)
            def converse(first, second):
                people = [RequestSettings(target=ModelTarget(local['id'], None, first)),
                          RequestSettings(target=ModelTarget(limited['id'], None, second))]
                return validate_participants(self.context, people, Gio.Cancellable())
            for check in (compare, converse):
                with self.subTest(check=check.__name__):
                    for first, second in (('only-limited', 'shared'), ('shared', 'only-ollama')):
                        with self.assertRaisesRegex(ValueError, 'no longer available'):
                            check(first, second)
            self.assertEqual(len(compare('only-ollama', 'only-limited')['targets']), 2)
            # One model on two types at one address is two targets.
            self.assertEqual(len(compare('shared', 'shared')['targets']), 2)
            resolved, connections = converse('only-ollama', 'only-limited')
            self.assertEqual([host_key(c) for c in connections], [('ollama', URL), ('limited', URL)])
            self.assertEqual([s.target.url for s in resolved], [URL, URL])

    def test_embedding_host_is_the_record_of_the_stored_type_that_can_embed(self):
        knowledge = self.context.knowledge
        with patch.dict(PROVIDERS, {**LIMITED, **OPTIONAL_KEY}):
            # Listed before the Ollama record at the address; the first cannot embed.
            self.storage.save_host('Limited', 'http://embed:11434', provider='limited')
            other = self.storage.save_host('Other', 'http://embed:11434', provider='optional_key')
            # A stored host without a type was built on an Ollama host, and no other type answers for it.
            for kind in (None, 'ollama', 'limited'):
                with self.assertRaisesRegex(ValueError, 'embedding host was removed'):
                    knowledge.require_host('http://embed:11434', kind)
            self.assertEqual(knowledge.require_host('http://embed:11434', 'optional_key').host_id, other['id'])
            embed = self.storage.add_host('Embedding host', 'http://embed:11434')
            for address in ('http://embed:11434', 'http://embed:11434/'):
                connection = knowledge.require_host(address)
                self.assertEqual((connection.kind, connection.host_id), ('ollama', embed['id']))
                self.assertEqual(knowledge.require_host(address, 'optional_key').host_id, other['id'])

if __name__ == '__main__':
    unittest.main()
