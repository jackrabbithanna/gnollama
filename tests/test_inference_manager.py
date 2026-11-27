"""Ubuntu's Inference Manager host type, against a loopback server answering as the service did when tried.

The list, the streams and the errors are those the service (1.0.1) gave with smollm2 and qwen3-5 (0.8b)
installed; the other models are served in the same form. The catalogue is a cut-down copy of the one
the service keeps, and the providers' model manifests are copies of theirs.
"""
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from gi.repository import Gdk
from src.host_manager import HostEditDialog
from src.providers import PROVIDERS, InferenceManagerProvider, ProviderError, supports
from src.providers.inference_manager import catalogue, disk_size
from src.providers.openai import fingerprint
from test_lmstudio import HostFixture, Loopback, recorded
from test_openai import TOOLS, USER, Handler, Keys, vectors
from test_ui import pump_until

SMOL, QWEN, NOMIC, CLEF = ('smollm2/smollm2-135m', 'qwen3-5/qwen-3-5-0.8b-q4-k-m', 'nomic-embed-text-v1-5/nomic-embed-text-v1-5',
                           'clef-flash/clef-flash-9b')
UNKNOWN = 'someone/unlisted-1b'
ANSWERS = recorded('inference_manager_answers.json')
FIXTURES = os.path.join(os.path.dirname(__file__), 'fixtures')


def served(*names):
    """The model list, as the service gives it, of these models."""
    model = recorded('inference_manager_models.json')['data'][0]
    return {'object': 'list', 'data': [dict(model, id=name, owned_by=name.split('/')[0]) for name in names]}


def stream(name='stream'):
    """A streamed chat the service sent, as lines."""
    with open(os.path.join(FIXTURES, f'inference_manager_{name}.txt'), 'rb') as file:
        return [line for line in file.read().split(b'\n') if line.strip()]


class Service(Handler):
    """Answers the service's root page as it does, and everything else as the OpenAI-compatible server does."""
    def do_GET(self):
        if self.path == '/v1/':
            self.server.received.append((self.path, None, self.headers.get('Authorization')))
            answer = self.server.root
            self.send_response(answer['status'])
            self.end_headers()
            self.wfile.write(json.dumps(answer['body']).encode())
            return
        super().do_GET()


class Local(Loopback):
    RECORDED = 'inference_manager_models.json'
    KIND = InferenceManagerProvider
    MODEL = SMOL
    EMBEDDER = NOMIC

    def setUp(self):
        super().setUp()
        self.server.RequestHandlerClass = Service
        self.server.root = ANSWERS['root']
        self.server.models = served(SMOL, QWEN, NOMIC, CLEF, UNKNOWN)
        self.server.lines = stream()
        self.catalogue = os.path.join(FIXTURES, 'inference_manager_catalogue.json')
        self.snaps = os.path.join(FIXTURES, 'inference_manager_snaps')

    def provider(self, **kwargs):
        provider = self.KIND(self.url, **kwargs)
        object.__setattr__(provider, 'catalogue_path', self.catalogue)
        object.__setattr__(provider, 'snaps_path', self.snaps)
        return provider

    def answer_with(self, answer):
        self.server.status, self.server.body = answer['status'], json.dumps(answer['body']).encode()


class TypeTests(unittest.TestCase):
    def test_attributes_and_place(self):
        cls = InferenceManagerProvider
        self.assertIs(PROVIDERS['inference_manager'], cls)
        self.assertEqual(list(PROVIDERS).index('inference_manager'), list(PROVIDERS).index('lmstudio') + 1)
        self.assertEqual((cls.label, cls.default_name, cls.name_example),
                         ('Ubuntu Inference Manager',) * 3)
        self.assertEqual((cls.default_url, cls.url_example), ('http://localhost:8400',) * 2)
        self.assertEqual((cls.requires_key, cls.key_optional, cls.optional_suffix), (False, False, '/v1'))
        offered = {name for name, value in vars(cls.capabilities).items() if value is True}
        self.assertEqual(offered, {'embeddings', 'thinking_levels', 'structured_output', 'tools', 'logprobs',
                                   'sampling_extras', 'thinking_text', 'decisions', 'decision_images'})
        self.assertEqual(cls.capabilities.options_notice, '')
        self.assertEqual(cls('http://localhost:8400/v1')._address('/models'), 'http://localhost:8400/v1/models')

    def test_sizes(self):
        self.assertEqual([disk_size(text) for text in ('540M', '81M', '6.8G', '1T', '12', '', None, 'M', 7)],
                         [540 * 2 ** 20, 81 * 2 ** 20, int(6.8 * 2 ** 30), 2 ** 40, None, None, None, None, None])

    def test_a_catalogue_that_is_missing_or_not_as_expected_is_empty(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, 'onboarded-snaps.json')
            self.assertEqual(catalogue(path), {})
            for text in ('not json', '{}', '[1, {"snap": 2}, {"snap": "a", "models": "b"}]', '[{"snap": "a"}]'):
                with self.subTest(text=text):
                    with open(path, 'w') as file:
                        file.write(text)
                    self.assertEqual(catalogue(path), {})
            with open(path, 'w') as file:
                file.write('[{"snap": "a", "models": [{"name": "b"}, {"name": 3}, "c"]}]')
            self.assertEqual(list(catalogue(path)), [('a', 'b')])


class ModelTests(Local, unittest.TestCase):
    def test_list_keeps_ids_and_leaves_out_decision_models(self):
        for suffix in ('', '/', '/v1', '/v1/'):
            with self.subTest(suffix=suffix):
                self.url = self.fixture.host + suffix
                models = self.provider().list_models()
                self.assertEqual(self.server.received[-1][0], '/v1/models')
                self.assertEqual([model['name'] for model in models], [SMOL, QWEN, NOMIC, UNKNOWN])
        self.assertEqual(models[0], {'name': SMOL, 'size': 101 * 2 ** 20,
                                     'digest': fingerprint(SMOL, 'Q4_K_M', '101M'),
                                     'details': {'family': 'SmolLM2', 'quantization_level': 'Q4_K_M'}})
        self.assertEqual(models[2]['capabilities'], ['embedding'])
        # A model the catalogue does not list is still served, with nothing known of it.
        self.assertEqual(models[3], {'name': UNKNOWN, 'details': {}, 'digest': fingerprint(UNKNOWN, None, None)})

    def test_the_provider_is_taken_from_owned_by(self):
        self.server.models = served(SMOL)
        self.server.models['data'][0].update(id='other/smollm2-135m', owned_by='smollm2')
        self.assertEqual(self.provider().list_models()[0]['details']['family'], 'SmolLM2')

    def test_capabilities_come_from_the_catalogue(self):
        provider = self.provider()
        qwen = provider.show_model(QWEN)
        self.assertEqual(qwen['supports'], {'vision': True, 'tools': True, 'thinking': True})
        self.assertEqual(qwen['thinking_levels'], [False, True])
        self.assertEqual(qwen['details'], {'family': 'Qwen 3.5', 'quantization_level': 'Q4_K_M'})
        smol = provider.show_model(SMOL)
        self.assertEqual(smol['supports'], {'vision': False, 'tools': False, 'thinking': False})
        self.assertNotIn('thinking_levels', smol)
        self.assertEqual(provider.show_model(NOMIC)['capabilities'], ['embedding'])
        # Unmatched: nothing is known of it, and thinking can be turned on or off.
        self.assertEqual(provider.show_model(UNKNOWN), {'thinking_levels': [False, True]})
        self.assertIsNone(supports(provider.show_model(UNKNOWN), 'vision'))
        # No catalogue, as from a sandbox: the installed providers' manifests say the same, but for the family.
        self.catalogue = os.path.join(FIXTURES, 'missing.json')
        provider = self.provider()
        self.assertEqual(provider.show_model(QWEN), dict(qwen, details={'quantization_level': 'Q4_K_M'}))
        self.assertEqual(provider.show_model(SMOL)['supports'], smol['supports'])
        listed = {model['name']: model for model in provider.list_models()}
        self.assertEqual(listed[NOMIC]['capabilities'], ['embedding'])
        self.assertEqual(listed[NOMIC]['size'], 81 * 2 ** 20)
        self.assertEqual([model['name'] for model in provider.list_embedding_models()], [NOMIC])
        self.assertEqual(provider.embedding_context(NOMIC), 512)
        # Neither: every model is unknown, and every model may embed.
        self.snaps = os.path.join(FIXTURES, 'missing')
        self.assertIsNone(supports(self.provider().show_model(QWEN), 'vision'))
        self.assertEqual(len(self.provider().list_embedding_models()), 5)

    def test_a_served_alias_is_found_through_the_provider_manifests(self):
        provider = self.provider()
        # The 4b model is served under another alias; smollm2's manifest sets none, so its name is its alias.
        self.server.models = served('qwen3-5/qwen-3-5-4b-q4-k-xl', SMOL)
        models = provider.list_models()
        self.assertEqual([model['details'].get('quantization_level') for model in models], ['Q4_K_XL', 'Q4_K_M'])
        self.assertEqual(provider.show_model('qwen3-5/qwen-3-5-4b-q4-k-xl')['size'], 2910 * 2 ** 20)
        # An alias no manifest names, and a provider whose snap cannot be read, are unknown.
        for name in ('qwen3-5/qwen-3-5-1b-q8', 'qwen3-5/qwen3.5', 'nomic-embed-text-v1-5/alias', '../qwen3-5/x'):
            with self.subTest(model=name):
                self.assertNotIn('supports', provider.show_model(name))
        self.snaps = os.path.join(FIXTURES, 'missing')
        self.assertNotIn('supports', self.provider().show_model(QWEN))
        self.assertIn('supports', self.provider().show_model(SMOL))

    def test_thinking_and_tool_calls_as_the_service_streamed_them(self):
        self.server.lines = stream('thinking')
        chunks = list(self.provider().chat(QWEN, USER, thinking=True))
        thinking = ''.join(chunk.get('message', {}).get('thinking') or '' for chunk in chunks)
        self.assertTrue(thinking.startswith('Thinking Process:'))
        self.assertEqual(''.join(chunk.get('message', {}).get('content') or '' for chunk in chunks), '42')
        self.assertEqual(self.server.received[-1][1]['chat_template_kwargs'], {'enable_thinking': True})
        self.server.lines = stream('tool')
        calls = [call for chunk in self.provider().chat(QWEN, USER, tools=TOOLS)
                 for call in chunk.get('message', {}).get('tool_calls') or ()]
        self.assertEqual([(call['function']['name'], call['function']['arguments']) for call in calls],
                         [('get_weather', {'city': 'Paris'})])

    def test_unknown_capability_names_are_ignored(self):
        with tempfile.TemporaryDirectory() as folder:
            self.catalogue = os.path.join(folder, 'catalogue.json')
            with open(self.catalogue, 'w') as file:
                json.dump([{'snap': 'qwen3-5', 'model_name': 'Qwen', 'models': [
                    {'name': 'qwen3.5-0.8b', 'capabilities': ['text', 'vision', 'telepathy', 4]}]}], file)
            self.assertEqual(self.provider().show_model(QWEN)['supports'],
                             {'vision': True, 'tools': False, 'thinking': False})

    def test_embedding_models_are_those_the_catalogue_says_embed(self):
        models = self.provider().list_embedding_models()
        self.assertEqual([model['name'] for model in models], [NOMIC])
        # The server refuses more than one batch of llama.cpp's, whatever nomic itself takes.
        self.assertEqual(self.provider().embedding_context(NOMIC), 512)
        self.assertIsNone(self.provider().embedding_context(QWEN))
        self.assertIsNone(self.provider().embedding_context(UNKNOWN))
        self.server.embeddings = recorded('inference_manager_embeddings.json')
        self.assertEqual(self.provider().embed(NOMIC, ['a', 'b'])['embeddings'],
                         [row['embedding'] for row in self.server.embeddings['data']])
        self.server.embeddings = vectors([0.5, -0.5])
        self.assertEqual(self.provider().embed(NOMIC, 'text'), {'embeddings': [[0.5, -0.5]]})
        self.assertEqual(self.server.received[-1][:2], ('/v1/embeddings', {'model': NOMIC, 'input': ['text']}))

    def test_no_key_is_sent_or_asked_for(self):
        keyed = self.provider(host_id='host', credential_id=None, credentials=Keys())
        keyed.list_models()
        list(keyed.chat(SMOL, USER))
        keyed.probe()
        self.assertEqual({request[2] for request in self.server.received}, {None})
        self.assertFalse(InferenceManagerProvider.keeps_key({'provider': 'openai', 'hostname': self.url}, self.url))

    def test_a_streamed_chat_as_the_service_sent_it(self):
        chunks = list(self.provider().chat(SMOL, USER, thinking=False))
        self.assertEqual(''.join(chunk.get('message', {}).get('content', '') for chunk in chunks), 'Hello, welcome to our conversation.')
        self.assertTrue(chunks[-1]['done'])
        body = self.server.received[-1][1]
        self.assertEqual((body['model'], body['chat_template_kwargs'], body['stream']),
                         (SMOL, {'enable_thinking': False}, True))
        self.assertNotIn('reasoning_effort', body)
        with self.assertRaisesRegex(ProviderError, 'Invalid thinking setting'):
            self.provider().request_body(SMOL, USER, thinking='high')
        self.assertEqual(self.provider().request_body(SMOL, USER, format='json')['response_format']['type'],
                         'json_schema')

    def test_the_check_tells_inference_manager_from_another_server(self):
        self.assertEqual(self.provider().probe(), 'Connected\nAvailable models: 4')
        self.assertEqual(self.server.received[0][0], '/v1/')
        for root in ({'status': 404, 'body': {'error': 'Not found'}}, {'status': 200, 'body': {'data': []}},
                     {'status': 200, 'body': ['documentation-url']}):
            with self.subTest(root=root):
                self.server.root = root
                with self.assertRaisesRegex(ProviderError, 'not Inference Manager. Use the OpenAI-Compatible'):
                    self.provider().probe()
        # Nothing at the address is the connection error, as for any server.
        self.fixture.close()
        with self.assertRaises(ProviderError) as caught:
            self.provider().probe()
        self.assertNotIn('Inference Manager', str(caught.exception))

    def test_errors_as_the_service_gives_them(self):
        explained = {'unknown_model': 'no longer served. Its provider may be stopped',
                     'no_model': 'Request body must contain a model',
                     'too_large': 'Request body is too large',
                     'image': 'image input is not supported',
                     'embeddings': 'This server does not support embeddings'}
        for name, said in explained.items():
            with self.subTest(answer=name):
                self.answer_with(ANSWERS[name])
                with self.assertRaisesRegex(ProviderError, said):
                    list(self.provider().chat(SMOL, USER))
        self.answer_with({'status': 502, 'body': {'error': {'message': 'Bad gateway', 'type': 'server_error'}}})
        with self.assertRaisesRegex(ProviderError, 'provider did not answer. It may still be loading'):
            self.provider().list_models()
        # A stopped provider's models are no longer listed.
        self.server.status = 200
        self.server.models = ANSWERS['stopped_models']['body']
        self.assertEqual(self.provider().list_models(), [])


@unittest.skipUnless(Gdk.Display.get_default(), 'GTK tests require a display')
class HostTests(Local, HostFixture, unittest.TestCase):
    NAME = 'Inference'

    def setUp(self):
        super().setUp()
        # The saved host's provider reads the catalogue fixture.
        patcher = patch.object(InferenceManagerProvider, 'catalogue_path', self.catalogue)
        patcher.start()
        self.addCleanup(patcher.stop)
        snaps = patch.object(InferenceManagerProvider, 'snaps_path', self.snaps)
        snaps.start()
        self.addCleanup(snaps.stop)
        self.context.services.catalog.invalidate()

    def test_composer_shows_what_the_catalogue_says(self):
        tab = self.make_tab(QWEN)
        self.assertEqual(self.shown(tab.chat_input), (True, True, '', True, [None, False, True]))
        self.assertEqual(self.shown(self.select(tab, SMOL)), (False, False, '', False, [None]))
        model = tab.chat_input.model_dropdown.get_model()
        names = [model.get_string(index) for index in range(model.get_n_items())]
        self.assertEqual(names, [QWEN, SMOL, UNKNOWN])

    def test_an_empty_list_says_how_to_install_a_model(self):
        self.server.models = ANSWERS['stopped_models']['body']
        tab = self.make_empty_tab()
        self.assertIn('sudo inference install gemma4', tab.chat_input._model_list_notice)

    def make_empty_tab(self):
        from src.tab import GenerationTab
        tab = GenerationTab(mode='chat', chat_id=self.storage.create_chat()['id'], context=self.context)
        self.tabs.append(tab)
        pump_until(lambda: not tab.chat_input._models_loading)
        return tab

    def test_host_editor_hides_the_key(self):
        dialog = HostEditDialog()
        dialog.provider_dropdown.set_selected(list(PROVIDERS).index('inference_manager'))
        self.assertEqual((dialog.hostname_entry.get_text(), dialog.name_entry.get_text()),
                         ('http://localhost:8400', 'Ubuntu Inference Manager'))
        self.assertTrue(dialog.hostname_entry.get_editable())
        self.assertFalse(dialog.key_box.get_visible())
        self.assertTrue(dialog.get_response_enabled('save'))


if __name__ == '__main__':
    unittest.main()
