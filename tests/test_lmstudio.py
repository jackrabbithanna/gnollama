"""The LM Studio host type against a loopback server that answers with a recorded model list."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from gi.repository import Gdk, Gio
from src.app_context import AppContext
from src.comparison import ComparisonRun, prepare_run
from src.host_manager import HostEditDialog
from src.model_conversation import validate_participants
from src.providers import PROVIDERS, LMStudioProvider, OpenAICompatibleProvider, ProviderError, supports
from src.providers.openai import fingerprint
from src.records import RequestSettings, WireOptions
from src.tab import GenerationTab
from test_openai import ANSWER, USER, Handler, Keys, Server
from test_ui import pump_until, wait_for_model

GEMMA, K2 = 'google/gemma-4-12b-qat', 'k2-horizon-3.7b'
EMBEDDING, NOMIC = 'text-embedding-embeddinggemma-2', 'text-embedding-nomic-embed-text-v1.5'
PLAIN = 'plain'
ALL_LEVELS = [None, False, True, 'low', 'medium', 'high', 'max']
SCHEMA = {'type': 'object', 'properties': {'a': {'type': 'string'}}}


def recorded(name):
    return json.loads((Path(__file__).parent / 'fixtures' / name).read_text())


class Loopback:
    """A server that lists the recorded models, and the provider under test bound to it."""
    RECORDED = 'lmstudio_models.json'
    KIND = LMStudioProvider
    MODEL = K2

    def setUp(self):
        self.fixture = Server()
        self.server = self.fixture.server
        self.server.models = recorded(self.RECORDED)
        self.addCleanup(self.fixture.close)
        self.locate()

    def locate(self):
        self.url = self.fixture.host

    def provider(self, **kwargs):
        return self.KIND(self.url, **kwargs)

    def entry(self, name):
        models = self.server.models.get('models') or self.server.models.get('data')
        return next(model for model in models if name in (model.get('key'), model.get('id')))

    def body(self, **kwargs):
        return self.provider().request_body('test', USER, **kwargs)


class KeyChecks:
    """What the base class promises about a key holds for a named type and its own requests."""
    def calls(self, provider):
        return (provider.list_models, lambda: provider.show_model(self.MODEL),
                lambda: list(provider.chat(self.MODEL, USER)))

    def keyed(self):
        return self.provider(host_id='host', credential_id='reference', credentials=Keys({'reference': 'sk-secret'}))

    def test_key_goes_with_every_request_and_never_follows_a_redirect(self):
        for call in self.calls(self.keyed()):
            call()
        self.assertEqual([request[2] for request in self.server.received], ['Bearer sk-secret'] * 3)
        self.assertNotIn('sk-secret', repr(self.keyed()))
        other = Server()
        self.addCleanup(other.close)

        class Redirect(Handler):
            def do_POST(self):
                self.send_response(307)
                self.send_header('Location', other.host + self.path)
                self.end_headers()
        self.server.RequestHandlerClass = Redirect
        for call in self.calls(self.keyed()):
            with self.assertRaisesRegex(ProviderError, 'HTTP Error 307'):
                call()
        self.assertEqual(other.server.received, [])

    def test_rejected_key_is_explained(self):
        self.server.status, self.server.body = 401, b'{"error": {"message": "No auth credentials found"}}'
        for call in self.calls(self.keyed()):
            with self.assertRaisesRegex(ProviderError, 'rejected the API key, or requires one') as caught:
                call()
            self.assertEqual(caught.exception.status, 401)


class HostFixture(Loopback):
    """A saved host of the type under test, and chat tabs on it."""
    NAME = 'Studio'

    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.context = AppContext(self.temp.name, credentials=Keys())
        self.storage = self.context.storage
        self.host = self.save_host()
        self.tabs = []
        self.addCleanup(self.close)

    def save_host(self):
        return self.storage.save_host(self.NAME, self.url, True, provider=self.KIND.kind)

    def close(self):
        for tab in self.tabs:
            tab.draft.close(discard=True)
            tab.chat_input.cancel_fetches()
            if tab.request:
                tab.request.cancellable.cancel()
        pump_until(lambda: self.context.services.idle and all(t.request is None for t in self.tabs))
        pump_until(lambda: self.storage.writer.idle)
        self.context.shutdown()
        self.temp.cleanup()

    def make_tab(self, model):
        tab = GenerationTab(mode='chat', chat_id=self.storage.create_chat()['id'], context=self.context)
        self.tabs.append(tab)
        wait_for_model(tab, model)
        return tab

    def select(self, tab, model):
        tab.chat_input.select_model(model)
        wait_for_model(tab, model)
        return tab.chat_input

    def send(self, tab, text):
        self.server.lines = ANSWER
        tab.chat_input.entry.set_text(text)
        tab.on_send_clicked()
        pump_until(lambda: tab.request is None and self.storage.writer.idle)
        return self.server.received[-1]

    def shown(self, composer):
        return (composer.image_support, composer.tool_support, composer.capability_notice.get_text(),
                composer.attach_button.get_sensitive(), composer._thinking_values)

    def targets(self, *models):
        return [dict(host_id=self.host['id'], model=model) for model in models]

    def prepare(self, targets, images=(), **wire):
        return prepare_run(self.context, ComparisonRun(), 'question', list(images),
                           RequestSettings(wire=WireOptions(**wire)), targets, None, 0)

    def validate(self, targets, **wire):
        people = [RequestSettings.from_stored(dict(target, **wire)) for target in targets]
        return validate_participants(self.context, people, Gio.Cancellable())


class ModelTests(Loopback, KeyChecks, unittest.TestCase):
    def test_list_leaves_out_decision_models_and_holds_what_the_model_manager_shows(self):
        for suffix in ('', '/', '/v1', '/v1/'):
            with self.subTest(suffix=suffix):
                models = LMStudioProvider(self.url + suffix).list_models()
                self.assertEqual(self.server.received[-1][0], '/api/v1/models')
                self.assertEqual([model['name'] for model in models], [GEMMA, EMBEDDING, K2, NOMIC])
        self.assertEqual(models[0], {
            'name': GEMMA, 'size': 7151067268,
            'digest': fingerprint(GEMMA, 'google/gemma-4-12b-qat@q4_0', 'Q4_0', 7151067268),
            'details': {'format': 'gguf', 'family': 'gemma4', 'parameter_size': '12B', 'quantization_level': 'Q4_0'}})
        # What the server leaves out or sends as null is left out, not shown as a value.
        self.assertEqual(models[3]['details'], {'format': 'gguf', 'quantization_level': 'Q4_K_M'})
        self.assertEqual(self.provider().probe(), 'Connected\nAvailable models: 4')
        self.server.models = {'data': []}
        with self.assertRaisesRegex(ProviderError, 'did not return a model list'):
            self.provider().list_models()

    def test_details_say_only_what_the_server_said(self):
        show = self.provider().show_model
        self.assertEqual(show(GEMMA), {
            'details': {'format': 'gguf', 'family': 'gemma4', 'parameter_size': '12B', 'quantization_level': 'Q4_0'},
            'supports': {'vision': True, 'tools': True, 'thinking': True}, 'thinking_levels': [False, True],
            'max_context_length': 262144})
        self.assertEqual(self.server.received[-1][0], '/api/v1/models')
        loaded = show(K2)
        self.assertEqual(loaded['supports'], {'vision': False, 'tools': False, 'thinking': True})
        # A loaded model has the size it was loaded with, beside the most it could have.
        self.assertEqual((loaded['context_length'], loaded['max_context_length']), (67584, 524288))
        self.assertNotIn('capabilities', loaded)
        embedding = show(EMBEDDING)
        self.assertEqual(embedding['capabilities'], ['embedding'])
        self.assertFalse({'supports', 'thinking_levels', 'context_length'} & set(embedding))
        for missing in ('elephant-1-decision', 'gone'):
            with self.subTest(model=missing), self.assertRaisesRegex(ProviderError, 'no longer available'):
                show(missing)

    def test_silence_is_unknown_and_other_levels_are_ignored(self):
        k2 = self.entry(K2)
        del k2['capabilities']['reasoning'], k2['capabilities']['vision']
        k2['type'] = 'something-new'
        details = self.provider().show_model(K2)
        self.assertEqual(details['supports'], {'tools': False})
        self.assertNotIn('thinking_levels', details)
        self.assertEqual([supports(details, name) for name in ('vision', 'tools', 'thinking')], [None, False, None])
        del k2['capabilities']
        self.assertFalse({'supports', 'thinking_levels', 'capabilities'} & set(self.provider().show_model(K2)))
        self.entry(GEMMA)['capabilities']['reasoning'] = {
            'allowed_options': ['off', 'low', 'medium', 'high', 'turbo', 'none', 7], 'default': 'low'}
        self.assertEqual(self.provider().show_model(GEMMA)['thinking_levels'], [False, 'low', 'medium', 'high'])
        self.entry(GEMMA)['capabilities']['reasoning'] = {'default': 'on'}
        self.assertNotIn('thinking_levels', self.provider().show_model(GEMMA))

    def test_fingerprint_follows_the_fields_that_identify_the_files(self):
        def digests():
            return {model['name']: model['digest'] for model in self.provider().list_models()}
        first = digests()
        self.assertEqual(first, digests())
        self.assertEqual(len(set(first.values())), 4)
        self.assertRegex(first[K2], '^[0-9a-f]{64}$')
        # Loading a model, or a new context size, is the same model.
        self.entry(GEMMA)['loaded_instances'] = copy.deepcopy(self.entry(K2)['loaded_instances'])
        self.entry(K2)['loaded_instances'] = []
        self.entry(K2)['max_context_length'] = 1
        self.assertEqual(first, digests())

        def change(model):
            model['selected_variant'] = 'google/gemma-4-12b-qat@q8_0'

        def requantize(model):
            model['quantization']['name'] = 'Q8_0'

        def resize(model):
            model['size_bytes'] += 1
        for edit in (change, requantize, resize):
            with self.subTest(edit=edit.__name__):
                saved = copy.deepcopy(self.server.models)
                edit(self.entry(GEMMA))
                changed = digests()
                self.assertNotEqual(changed[GEMMA], first[GEMMA])
                self.assertEqual(changed[K2], first[K2])
                self.server.models = saved

    def test_thinking_settings_are_sent_as_the_efforts_the_server_takes(self):
        self.assertNotIn('reasoning_effort', self.body())
        for setting, sent in ((False, 'none'), (True, 'medium'), ('low', 'low'), ('medium', 'medium'),
                              ('high', 'high'), ('max', 'xhigh')):
            with self.subTest(setting=setting):
                self.assertEqual(self.body(thinking=setting)['reasoning_effort'], sent)
        for refused in ('none', 'xhigh', 'on', 'off', 'minimal', 1, 0, ['low']):
            with self.subTest(thinking=refused), self.assertRaisesRegex(ProviderError, 'Invalid thinking setting'):
                self.body(thinking=refused)

    def test_json_mode_is_a_schema_and_sampling_extras_are_sent(self):
        mode = {'type': 'json_schema', 'json_schema': {'name': 'response', 'schema': {'type': 'object'}}}
        body = self.body(format='json')
        self.assertEqual(body['response_format'], mode)
        self.assertIsNot(body['response_format'], LMStudioProvider.json_mode)
        self.assertEqual(self.body(format=SCHEMA)['response_format'],
                         {'type': 'json_schema', 'json_schema': {'name': 'response', 'schema': SCHEMA}})
        self.assertNotIn('response_format', self.body())
        body = self.body(options={'temperature': 0.5, 'top_k': 40, 'min_p': 0.05, 'num_predict': 64})
        self.assertEqual({name: body[name] for name in ('temperature', 'top_k', 'min_p', 'max_tokens')},
                         {'temperature': 0.5, 'top_k': 40, 'min_p': 0.05, 'max_tokens': 64})
        with self.assertRaisesRegex(ProviderError, r'does not accept: num_ctx\.$'):
            self.body(options={'num_ctx': 4096, 'top_k': 1})
        with self.assertRaisesRegex(ProviderError, 'keep_alive'):
            self.body(keep_alive=300)

    def test_chat_goes_through_chat_completions_with_or_without_the_suffix(self):
        for suffix in ('', '/v1'):
            chunks = list(LMStudioProvider(self.url + suffix).chat(K2, USER, thinking=False, format='json',
                                                                   options={'top_k': 40}))
            self.assertEqual(chunks[-1]['done_reason'], 'stop')
            path, body, authorization = self.server.received[-1]
            self.assertEqual((path, authorization), ('/v1/chat/completions', None))
            self.assertEqual((body['model'], body['reasoning_effort'], body['top_k']), (K2, 'none', 40))
            self.assertEqual(body['response_format']['type'], 'json_schema')

    def test_class_declares_what_the_host_editor_and_panel_read(self):
        cls, caps = LMStudioProvider, LMStudioProvider.capabilities
        self.assertEqual((cls.kind, cls.label, cls.default_url, cls.default_name, cls.key_url, cls.url_example),
                         ('lmstudio', 'LM Studio', 'http://localhost:1234', 'LM Studio', '', 'http://localhost:1234'))
        self.assertEqual((cls.fixed_url, cls.requires_key, cls.key_optional), (None, False, True))
        offered = {name for name, value in vars(caps).items() if value is True}
        self.assertEqual(offered, {'thinking_levels', 'structured_output', 'tools', 'logprobs', 'thinking_text',
                                   'sampling_extras'})
        # A type that reports its models does not carry the generic type's warning.
        self.assertEqual(caps.options_notice, '')
        self.assertTrue(OpenAICompatibleProvider.capabilities.options_notice)
        self.assertIs(PROVIDERS['lmstudio'], cls)
        for name in ('generate', 'embed', 'pull', 'delete', 'running', 'unload'):
            with self.subTest(operation=name), self.assertRaises(NotImplementedError):
                getattr(self.provider(), name)('test')


@unittest.skipUnless(Gdk.Display.get_default(), 'GTK smoke tests require a display')
class HostTests(HostFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        # A model the server says nothing about, as some chat models are listed.
        plain = {key: value for key, value in self.entry(K2).items() if key != 'capabilities'}
        self.server.models['models'].append(dict(plain, key=PLAIN, loaded_instances=[]))

    def test_composer_shows_yes_no_and_unknown(self):
        tab = self.make_tab(GEMMA)
        self.assertEqual(self.server.received[0][0], '/api/v1/models')
        self.assertEqual(self.shown(tab.chat_input), (True, True, '', True, [None, False, True]))
        # The summary says what a model lacks, and the disabled button says why.
        self.assertEqual(self.shown(self.select(tab, K2)), (False, False, '', False, [None, False, True]))
        self.assertEqual(tab.chat_input.attach_button.get_tooltip_text(), 'This model does not support images.')
        # Reporting no training for tools is advice: the option stays, with a notice.
        panel = tab.options_panel
        self.assertIs(panel.tool_support, False)
        panel.tools_check.set_active(True)
        panel.update_tools_notice()
        self.assertIn('reports no tool support; you can still test the API', panel.tools_notice.get_text())
        self.assertTrue(panel.tools_check.get_sensitive())
        panel.tools_check.set_active(False)
        self.assertFalse(panel.provider_notice.get_visible())
        self.assertEqual(self.shown(self.select(tab, PLAIN)), (None, None, '', True, ALL_LEVELS))
        self.assertEqual(tab.chat_input.attach_button.get_tooltip_text(), 'Attach Image')
        self.assertIsNone(panel.tool_support)

    def test_embedding_models_are_never_in_the_chat_list(self):
        composer = self.make_tab(GEMMA).chat_input
        model = composer.model_dropdown.get_model()
        names = [model.get_string(index) for index in range(model.get_n_items())]
        # The list says which they are, so no details are asked for first.
        self.assertEqual([name for name in names if name in (GEMMA, EMBEDDING, K2, NOMIC)], [GEMMA, K2])
        listed = {tag['name']: tag.get('capabilities') for tag in self.provider().list_models()}
        self.assertEqual(listed, {GEMMA: None, EMBEDDING: ['embedding'], K2: None, NOMIC: ['embedding'], PLAIN: None})

    def test_tab_sends_the_thinking_setting_the_composer_offers(self):
        tab = self.make_tab(GEMMA)
        composer = self.select(tab, K2)
        for index, sent in ((1, 'none'), (2, 'medium')):
            composer.thinking_dropdown.set_selected(index)
            path, body, authorization = self.send(tab, 'hello')
            self.assertEqual((path, body['model'], body['reasoning_effort']), ('/v1/chat/completions', K2, sent))
        composer.thinking_dropdown.set_selected(0)
        self.assertNotIn('reasoning_effort', self.send(tab, 'again')[1])

    def test_a_thinking_level_the_model_lacks_is_refused(self):
        both = self.targets(GEMMA, K2)
        for check in (self.prepare, self.validate):
            for thinking in ('low', 'high', 'max'):
                with self.subTest(check=check.__name__, thinking=thinking), \
                        self.assertRaisesRegex(ValueError, 'thinking level is incompatible'):
                    check(both, thinking=thinking)
            for thinking in (None, False, True):
                check(both, thinking=thinking)
            # Nothing is known of this one, so nothing is refused for it.
            listed = copy.deepcopy(self.server.models)
            del self.entry(GEMMA)['capabilities']['reasoning']
            self.context.services.catalog.invalidate()
            check(self.targets(PLAIN, GEMMA), thinking='max')
            self.server.models = listed
            self.context.services.catalog.invalidate()
            with self.assertRaisesRegex(ValueError, 'Embedding models cannot'):
                check(self.targets(GEMMA, EMBEDDING))
        self.prepare(both, options={'top_k': 5, 'min_p': 0.1}, format='json')

    def test_host_editor_offers_the_type_with_its_address_and_an_optional_key(self):
        dialog = HostEditDialog()
        model = dialog.provider_dropdown.get_model()
        labels = [model.get_string(index) for index in range(model.get_n_items())]
        self.assertEqual(labels[2:5], ['OpenAI-Compatible Server', 'LM Studio', 'OpenRouter'])
        dialog.provider_dropdown.set_selected(list(PROVIDERS).index('lmstudio'))
        self.assertEqual((dialog.hostname_entry.get_text(), dialog.name_entry.get_text()),
                         ('http://localhost:1234', 'LM Studio'))
        self.assertTrue(dialog.hostname_entry.get_editable())
        self.assertEqual((dialog.key_box.get_visible(), dialog.key_link.get_visible()), (True, False))
        self.assertEqual(dialog.key_hint.get_text(), 'Optional. Stored securely in your desktop keyring.')
        self.assertTrue(dialog.get_response_enabled('save'))


if __name__ == '__main__':
    unittest.main()
