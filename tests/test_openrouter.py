"""The OpenRouter host type against a loopback server that answers with a recorded catalog."""
import unittest
from unittest.mock import patch

from gi.repository import Gdk
from src.credentials import CredentialError
from src.host_manager import HostEditDialog
from src.providers import PROVIDERS, OpenRouterProvider, ProviderError, supports
from src.providers.openai import fingerprint
from src.providers.openrouter import thinking_levels
from test_lmstudio import SCHEMA, HostFixture, KeyChecks, Loopback
from test_openai import USER, Handler

PNG = 'iVBORw0KGgoAAAAAAAAAAAAAAAAAAAAA'
TEXT, VISION, EFFORTS = 'inference-net/schematron-v2-turbo', 'mistralai/pixtral-large-2502', 'thinkingmachines/inkling-2-mini'
MANDATORY, ALWAYS, SWITCH = 'google/gemini-4-neon', 'liquid/lfm-3-1.7b', 'tencent/hy4-flash'
AUDIO, ROUTER = 'google/lyria-3-pro-preview', 'openrouter/auto'
LISTED = [TEXT, VISION, EFFORTS, MANDATORY, ALWAYS, SWITCH, AUDIO, ROUTER]


class Catalog(Loopback):
    """The one address the type accepts, moved to the loopback server."""
    RECORDED = 'openrouter_models.json'
    KIND = OpenRouterProvider
    MODEL = EFFORTS
    NAME = 'Router'

    def locate(self):
        self.url = self.fixture.host + '/api'
        moved = patch.object(OpenRouterProvider, 'fixed_url', self.url)
        moved.start()
        self.addCleanup(moved.stop)

    def save_host(self):
        return self.storage.save_host(self.NAME, self.url, True, provider='openrouter',
                                      api_key='sk-or-test', session_only=True)


class ModelTests(Catalog, KeyChecks, unittest.TestCase):
    def test_list_is_read_without_a_key_and_holds_models_that_answer_in_text(self):
        models = self.provider().list_models()
        self.assertEqual(self.server.received[-1], ('/api/v1/models', None, None))
        self.assertEqual([model['name'] for model in models], LISTED)
        self.assertEqual(models[2], {'name': EFFORTS, 'digest': fingerprint('thinkingmachines/inkling-2-mini-20261120')})
        # Text beside another kind of output is still a chat model; no text is not.
        self.assertEqual(self.entry(AUDIO)['architecture']['output_modalities'], ['text', 'audio'])
        self.entry(AUDIO)['architecture']['output_modalities'] = ['audio']
        self.entry(VISION)['architecture']['output_modalities'] = ['image']
        self.assertEqual([model['name'] for model in self.provider().list_models()],
                         [name for name in LISTED if name not in (AUDIO, VISION)])
        with self.assertRaisesRegex(ProviderError, 'no longer available'):
            self.provider().show_model(VISION)
        self.assertEqual(self.provider().probe(), 'Connected\nAvailable models: 6')

    def test_details_are_a_complete_capability_list(self):
        show = self.provider().show_model
        text = show(TEXT)
        self.assertEqual(text, {'capabilities': ['completion'], 'reasoning': None, 'context_length': 128000,
                                'max_completion_tokens': 8192,
                                'supported_parameters': self.entry(TEXT)['supported_parameters']})
        self.assertEqual([supports(text, name) for name in ('vision', 'tools', 'thinking')], [False] * 3)
        self.assertEqual(show(VISION)['capabilities'], ['completion', 'vision'])
        efforts = show(EFFORTS)
        self.assertEqual(efforts['capabilities'], ['completion', 'vision', 'tools', 'thinking'])
        self.assertEqual(efforts['thinking_levels'], [False, 'low', 'medium', 'high', 'max'])
        # Phase 3b narrows the options from these two, so they arrive as the catalog gave them.
        self.assertEqual(efforts['reasoning'], {'mandatory': False, 'default_enabled': True, 'default_effort': 'high',
                                                'supported_efforts': ['max', 'high', 'medium', 'low', 'minimal', 'none']})
        self.assertIn('top_k', efforts['supported_parameters'])
        self.assertNotIn('top_k', show(MANDATORY)['supported_parameters'])
        router = show(ROUTER)
        self.assertEqual((router['context_length'], 'max_completion_tokens' in router), (2000000, False))

    def test_thinking_levels_for_each_kind_of_model(self):
        show = self.provider().show_model
        expected = {TEXT: None, VISION: None, AUDIO: None,
                    EFFORTS: [False, 'low', 'medium', 'high', 'max'],
                    MANDATORY: ['low', 'medium', 'high', 'max'],  # Its highest effort is xhigh.
                    ALWAYS: [True], SWITCH: [False, True],
                    ROUTER: [False, True]}  # No reasoning object, but it lists the parameter.
        for model, levels in expected.items():
            with self.subTest(model=model):
                details = show(model)
                self.assertEqual(details.get('thinking_levels'), levels)
                self.assertEqual('thinking' in details['capabilities'], levels is not None)
        for reasoning, levels in (
                ({'mandatory': True, 'supported_efforts': ['high', 'minimal', 'none']}, ['high']),
                ({'mandatory': False, 'supported_efforts': ['xhigh', 'max', 'minimal']}, ['max']),
                ({'mandatory': False, 'supported_efforts': None}, [False, 'low', 'medium', 'high', 'max']),
                ({'mandatory': True, 'supported_efforts': None}, ['low', 'medium', 'high', 'max']),
                ({'mandatory': False, 'supported_efforts': []}, [])):
            with self.subTest(reasoning=reasoning):
                self.assertEqual(thinking_levels(reasoning, []), levels)
        self.assertIsNone(thinking_levels(None, ['temperature']))
        self.assertEqual(thinking_levels(None, ['reasoning_effort']), [False, True])

    def test_fingerprint_is_the_permanent_slug(self):
        def digests():
            return {model['name']: model.get('digest') for model in self.provider().list_models()}
        first = digests()
        self.assertEqual(first, digests())
        self.assertEqual(len(set(first.values())), len(LISTED))
        self.entry(TEXT).update(context_length=1, name='Renamed', supported_parameters=[])
        self.assertEqual(first, digests())
        self.entry(TEXT)['canonical_slug'] += '-v2'
        changed = digests()
        self.assertNotEqual(changed[TEXT], first[TEXT])
        self.assertEqual(changed[VISION], first[VISION])
        del self.entry(TEXT)['canonical_slug']
        self.assertNotIn('digest', self.provider().list_models()[0])

    def test_thinking_settings_are_sent_as_the_reasoning_object(self):
        self.assertFalse({'reasoning', 'reasoning_effort'} & set(self.body()))
        for setting, sent in ((False, {'effort': 'none'}), (True, {'enabled': True}), ('low', {'effort': 'low'}),
                              ('medium', {'effort': 'medium'}), ('high', {'effort': 'high'}), ('max', {'effort': 'max'})):
            with self.subTest(setting=setting):
                body = self.body(thinking=setting)
                self.assertEqual(body['reasoning'], sent)
                self.assertNotIn('reasoning_effort', body)
        for refused in ('none', 'xhigh', 'minimal', 1, 0, {'effort': 'high'}):
            with self.subTest(thinking=refused), self.assertRaisesRegex(ProviderError, 'Invalid thinking setting'):
                self.body(thinking=refused)

    def test_json_mode_and_sampling_extras(self):
        self.assertEqual(self.body(format='json')['response_format'], {'type': 'json_object'})
        self.assertEqual(self.body(format=SCHEMA)['response_format'],
                         {'type': 'json_schema', 'json_schema': {'name': 'response', 'schema': SCHEMA}})
        body = self.body(options={'top_k': 40, 'min_p': 0.05, 'num_predict': 64})
        self.assertEqual((body['top_k'], body['min_p'], body['max_tokens']), (40, 0.05, 64))
        with self.assertRaisesRegex(ProviderError, r'does not accept: num_ctx\.$'):
            self.body(options={'num_ctx': 4096})

    def test_chat_address_and_no_header_naming_the_application(self):
        headers = []

        class Recording(Handler):
            def do_POST(self):
                headers.append({name.lower() for name in self.headers.keys()})
                super().do_POST()
        self.server.RequestHandlerClass = Recording
        list(self.keyed().chat(EFFORTS, USER))
        self.assertEqual(self.server.received[-1][0], '/api/v1/chat/completions')
        self.assertIn('authorization', headers[-1])
        self.assertFalse({'http-referer', 'x-title'} & headers[-1])

    def test_only_the_fixed_address_is_accepted(self):
        elsewhere = OpenRouterProvider(self.fixture.host)
        for call in self.calls(elsewhere):
            with self.assertRaisesRegex(ProviderError, 'OpenRouter requires'):
                call()
        self.assertEqual(self.server.received, [])
        with self.assertRaisesRegex(ValueError, 'OpenRouter requires'):
            OpenRouterProvider.check_url(self.fixture.host)
        OpenRouterProvider.check_url(self.url)


class DeclarationTests(unittest.TestCase):
    def test_class_declares_what_the_host_editor_and_panel_read(self):
        cls, caps = OpenRouterProvider, OpenRouterProvider.capabilities
        self.assertEqual((cls.kind, cls.label, cls.default_name, cls.key_url, cls.fixed_url),
                         ('openrouter', 'OpenRouter', 'OpenRouter', 'https://openrouter.ai/keys', 'https://openrouter.ai/api'))
        self.assertEqual((cls.requires_key, cls.key_optional), (True, False))
        self.assertEqual(cls(cls.fixed_url)._address('/chat/completions'), 'https://openrouter.ai/api/v1/chat/completions')
        self.assertEqual(cls(cls.fixed_url)._address('/models'), 'https://openrouter.ai/api/v1/models')
        offered = {name for name, value in vars(caps).items() if value is True}
        self.assertEqual(offered, {'thinking_levels', 'structured_output', 'tools', 'logprobs', 'thinking_text',
                                   'sampling_extras'})
        self.assertEqual(caps.options_notice, '')
        self.assertIs(PROVIDERS['openrouter'], cls)

    @unittest.skipUnless(Gdk.Display.get_default(), 'GTK smoke tests require a display')
    def test_host_editor_fixes_the_address_and_demands_a_key(self):
        dialog = HostEditDialog()
        dialog.provider_dropdown.set_selected(list(PROVIDERS).index('openrouter'))
        self.assertEqual((dialog.hostname_entry.get_text(), dialog.name_entry.get_text()),
                         ('https://openrouter.ai/api', 'OpenRouter'))
        self.assertFalse(dialog.hostname_entry.get_editable())
        self.assertEqual((dialog.key_box.get_visible(), dialog.key_link.get_visible(), dialog.key_link.get_uri()),
                         (True, True, 'https://openrouter.ai/keys'))
        self.assertEqual(dialog.key_hint.get_text(), 'Stored securely in your desktop keyring.')
        self.assertFalse(dialog.remove_key_check.get_visible())
        self.assertFalse(dialog.get_response_enabled('save'))
        dialog.api_key_entry.set_text('sk-or-test')
        self.assertTrue(dialog.get_response_enabled('save'))


@unittest.skipUnless(Gdk.Display.get_default(), 'GTK smoke tests require a display')
class HostTests(Catalog, HostFixture, unittest.TestCase):
    def test_host_needs_a_key_and_the_fixed_address(self):
        with self.assertRaises(CredentialError):
            self.storage.save_host('Keyless', self.url, provider='openrouter')
        with self.assertRaisesRegex(ValueError, 'OpenRouter requires'):
            self.storage.save_host('Elsewhere', self.fixture.host, provider='openrouter',
                                   api_key='sk-or-test', session_only=True)

    def test_composer_shows_what_the_catalog_says(self):
        tab = self.make_tab(TEXT)
        self.assertEqual(self.server.received[0], ('/api/v1/models', None, 'Bearer sk-or-test'))
        without = 'This model does not support images.'
        self.assertEqual(self.shown(tab.chat_input), (False, False, without, False, [None]))
        self.assertFalse(tab.chat_input.thinking_dropdown.get_sensitive())
        self.assertFalse(tab.options_panel.provider_notice.get_visible())
        for model, shown in ((VISION, (True, False, '', True, [None])),
                             (EFFORTS, (True, True, '', True, [None, False, 'low', 'medium', 'high', 'max'])),
                             (MANDATORY, (True, True, '', True, [None, 'low', 'medium', 'high', 'max'])),
                             (ALWAYS, (False, True, without, False, [None, True])),
                             (SWITCH, (True, True, '', True, [None, False, True])),
                             (ROUTER, (True, True, '', True, [None, False, True]))):
            with self.subTest(model=model):
                self.assertEqual(self.shown(self.select(tab, model)), shown)

    def test_tab_sends_the_key_and_the_reasoning_object(self):
        tab = self.make_tab(TEXT)
        composer = self.select(tab, EFFORTS)
        composer.thinking_dropdown.set_selected(composer._thinking_values.index('max'))
        path, body, authorization = self.send(tab, 'hello')
        self.assertEqual((path, authorization), ('/api/v1/chat/completions', 'Bearer sk-or-test'))
        self.assertEqual((body['model'], body['reasoning']), (EFFORTS, {'effort': 'max'}))
        composer = self.select(tab, ALWAYS)
        # Max is not a level of this model, so the control went back to the model's default.
        self.assertNotIn('reasoning', self.send(tab, 'again')[1])
        composer.thinking_dropdown.set_selected(1)
        self.assertEqual(self.send(tab, 'think')[1]['reasoning'], {'enabled': True})

    def test_what_a_model_lacks_is_refused_for_a_shared_setting(self):
        for check in (self.prepare, self.validate):
            with self.subTest(check=check.__name__):
                with self.assertRaisesRegex(ValueError, 'support the selected thinking option'):
                    check(self.targets(EFFORTS, TEXT), thinking='high')
                check(self.targets(EFFORTS, TEXT), thinking=False)
                # A model that must think is never told not to.
                with self.assertRaisesRegex(ValueError, 'thinking level is incompatible'):
                    check(self.targets(EFFORTS, MANDATORY), thinking=False)
                with self.assertRaisesRegex(ValueError, 'thinking level is incompatible'):
                    check(self.targets(EFFORTS, SWITCH), thinking='high')
                check(self.targets(EFFORTS, MANDATORY), thinking='max')
        with self.assertRaisesRegex(ValueError, 'must support the shared images'):
            self.prepare(self.targets(VISION, TEXT), images=[PNG])
        self.prepare(self.targets(VISION, EFFORTS), images=[PNG], options={'top_k': 5}, format='json')


if __name__ == '__main__':
    unittest.main()
