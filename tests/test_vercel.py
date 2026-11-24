"""The Vercel AI Gateway host type against a loopback server that answers with a recorded model list."""
import json
import unittest
from dataclasses import replace
from unittest.mock import patch

from gi.repository import Gdk
from src.credentials import CredentialError
from src.host_manager import HostEditDialog
from src.providers import PROVIDERS, ProviderError, supports
from src.providers.openai import fingerprint
from src.providers.vercel import URL, VercelProvider, thinking_levels
from test_lmstudio import SCHEMA, HostFixture, KeyChecks, Loopback
from test_openai import USER, Handler, vectors

QWEN, LIGHTNING, BEAM = 'alibaba/qwen3.7-flash', 'nvidia/nemotron-3.5-lightning-free', 'reflection/beam-free'
GLYPH, LING, NEMO = 'stealth/glyph-cluster', 'inclusionai/ling-3.0-flash', 'mistral/mistral-nemo'
R1, FABLE, SCALEDOWN = 'deepseek/deepseek-r1', 'anthropic/claude-fable-5', 'scaledown/scaledown-summarize-1'
LISTED = [QWEN, LIGHTNING, BEAM, GLYPH, LING, NEMO, R1, FABLE, SCALEDOWN]
EMBEDDER, IMAGE = 'alibaba/qwen3-embedding-0.6b', 'bfl/flux-2-flex'
CREDITS = {'balance': '20', 'token_pass': None, 'total_used': '0'}
MODEL_HINT = 'Not available for this model.'


class GatewayHandler(Handler):
    """Also answers the balance of the key, with a status of its own."""
    def do_POST(self):
        server = self.server
        if not self.path.endswith('/credits') or server.status != 200:
            return super().do_POST()
        server.received.append((self.path, None, self.headers.get('Authorization')))
        status, body = server.credits
        self.send_response(status)
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())


class Gateway(Loopback):
    """The one address the type accepts, moved to the loopback server."""
    RECORDED = 'vercel_models.json'
    KIND = VercelProvider
    MODEL = QWEN
    EMBEDDER = EMBEDDER
    NAME = 'Gateway'

    def locate(self):
        self.url = self.fixture.host + '/gateway'
        moved = patch.object(VercelProvider, 'fixed_url', self.url)
        moved.start()
        self.addCleanup(moved.stop)
        self.server.RequestHandlerClass = GatewayHandler
        self.server.credits = (200, CREDITS)

    def save_host(self):
        return self.storage.save_host(self.NAME, self.url, True, provider='vercel',
                                      api_key='sk-vercel-test', session_only=True)


class ModelTests(Gateway, KeyChecks, unittest.TestCase):
    def test_one_list_is_split_by_type_and_other_kinds_are_left_out(self):
        provider = self.provider()
        models = provider.list_models()
        self.assertEqual(self.server.received[-1], ('/gateway/v1/models', None, None))
        self.assertEqual([model['name'] for model in models], LISTED)
        self.assertEqual(models[0], {'name': QWEN, 'digest': fingerprint(QWEN, 1785196800)})
        embedders = provider.list_embedding_models()
        self.assertEqual(embedders, [{'name': EMBEDDER, 'digest': fingerprint(EMBEDDER, 1763078400),
                                      'context_length': 32768}])
        self.assertEqual({request[0] for request in self.server.received}, {'/gateway/v1/models'})
        self.assertEqual((provider.model_tag(QWEN), provider.model_tag(EMBEDDER)), (models[0], embedders[0]))
        for unknown in ('gone', IMAGE):
            with self.subTest(model=unknown):
                self.assertIsNone(provider.model_tag(unknown))
                with self.assertRaisesRegex(ProviderError, 'no longer available'):
                    provider.show_model(unknown)
                with self.assertRaisesRegex(ProviderError, 'no longer available'):
                    provider.embedding_context(unknown)
        # The type decides, not the name; and a new size is the same model, a new release another.
        self.entry(NEMO).update(type='embedding', context_window=1)
        self.entry(EMBEDDER)['type'] = 'image'
        self.assertEqual([model['name'] for model in provider.list_models()], [n for n in LISTED if n != NEMO])
        self.assertEqual(provider.list_embedding_models(), [dict(models[5], context_length=1)])
        self.entry(NEMO)['released'] += 1
        self.assertNotEqual(provider.model_tag(NEMO)['digest'], models[5]['digest'])

    def test_details_say_what_the_list_says_of_each_model(self):
        show = self.provider().show_model
        self.assertEqual(show(QWEN), {
            'capabilities': ['completion', 'vision', 'tools', 'thinking'],
            'supported_parameters': self.entry(QWEN)['supported_parameters'],
            'thinking_levels': [False, True, 'low', 'medium', 'high', 'max'],
            'context_length': 991000, 'max_completion_tokens': 64000,
            'pricing': {'prompt': '0.00000003', 'completion': '0.00000013', 'input_cache_read': '0.000000006',
                        'input_cache_write': '0.000000038'}})
        self.assertEqual(show(NEMO), {
            'capabilities': ['completion', 'tools'], 'supported_parameters': self.entry(NEMO)['supported_parameters'],
            'context_length': 131072, 'max_completion_tokens': 131072,
            'pricing': {'prompt': '0.000000029', 'completion': '0.00000003'}})
        expected = {QWEN: (True, True, True), LIGHTNING: (False, True, True), BEAM: (False, True, True),
                    GLYPH: (False, True, True), LING: (False, True, True), NEMO: (False, True, False),
                    R1: (False, True, True), FABLE: (True, True, True),
                    SCALEDOWN: (None, False, False)}  # It has no tags, so whether it sees images is unknown.
        for model, known in expected.items():
            with self.subTest(model=model):
                details = show(model)
                self.assertEqual(tuple(supports(details, name) for name in ('vision', 'tools', 'thinking')), known)
                self.assertEqual(details['supported_parameters'], self.entry(model)['supported_parameters'])
        self.assertEqual((show(FABLE)['context_length'], show(FABLE)['max_completion_tokens']), (1000000, 128000))
        self.assertEqual(show(SCALEDOWN)['max_completion_tokens'], 1000)
        self.entry(NEMO).update(context_window=0, max_tokens='8192')
        self.assertFalse({'context_length', 'max_completion_tokens'} & set(show(NEMO)))

    def test_thinking_levels_for_each_kind_of_model(self):
        show = self.provider().show_model
        expected = {QWEN: [False, True, 'low', 'medium', 'high', 'max'],  # Toggle, and efforts with none.
                    LIGHTNING: [False, True, 'low', 'medium', 'high'],
                    BEAM: ['low', 'medium', 'high', 'max'],  # Efforts without none: no way to stop.
                    GLYPH: ['low', 'medium', 'high', 'max'],  # Its highest effort is xhigh.
                    LING: [False, True],  # A toggle alone.
                    R1: [False, True],  # No options, but it lists the parameter.
                    NEMO: None}
        for model, levels in expected.items():
            with self.subTest(model=model):
                self.assertEqual(show(model).get('thinking_levels'), levels)
                self.assertEqual(thinking_levels(self.entry(model)), levels)
        for options, levels in (([{'type': 'effort', 'values': ['none', 'minimal', 'high']}], [False, 'high']),
                                ([{'type': 'effort', 'values': ['xhigh', 'max']}], ['max'])):
            with self.subTest(options=options):
                self.assertEqual(thinking_levels({'reasoning_options': options}), levels)

    def test_narrowing_follows_the_listed_parameters(self):
        provider = self.provider()

        def record(model):
            return provider.model_capabilities(provider.show_model(model))
        for model in LISTED:
            with self.subTest(model=model):
                narrowed = record(model)
                # No model lists either, so neither is offered.
                self.assertLessEqual({'top_p', 'seed'}, set(narrowed.left_out))
                self.assertFalse(narrowed.sampling_extras or narrowed.logprobs)
        self.assertEqual(record(QWEN), replace(provider.capabilities, left_out=('top_p', 'seed')))
        self.assertEqual(record(FABLE).left_out, ('temperature', 'top_p', 'seed'))
        self.assertFalse(record(GLYPH).structured_output)
        self.assertTrue(record(GLYPH).tools and record(GLYPH).thinking_levels)
        scaledown = record(SCALEDOWN)
        self.assertFalse(scaledown.tools or scaledown.structured_output or scaledown.thinking_levels)
        self.assertTrue(scaledown.embeddings)
        # An embedding model lists no parameters, so nothing is narrowed by it.
        self.assertIs(provider.model_capabilities(provider.show_model(EMBEDDER)), provider.capabilities)

    def test_thinking_settings_are_sent_as_the_reasoning_object(self):
        body = self.body()
        self.assertFalse({'reasoning', 'reasoning_effort', 'provider'} & set(body))
        self.assertEqual(body['stream_options'], {'include_usage': True})
        for setting, sent in ((False, {'effort': 'none'}), (True, {'enabled': True}), ('low', {'effort': 'low'}),
                              ('medium', {'effort': 'medium'}), ('high', {'effort': 'high'}), ('max', {'effort': 'max'})):
            with self.subTest(setting=setting):
                body = self.body(thinking=setting)
                self.assertEqual(body['reasoning'], sent)
                self.assertFalse({'reasoning_effort', 'provider'} & set(body))
        for refused in ('none', 'xhigh', 'minimal', 1, {'effort': 'high'}):
            with self.subTest(thinking=refused), self.assertRaisesRegex(ProviderError, 'Invalid thinking setting'):
                self.body(thinking=refused)
        self.assertEqual(self.body(format=SCHEMA)['response_format'],
                         {'type': 'json_schema', 'json_schema': {'name': 'response', 'schema': SCHEMA}})
        # The type does not offer Top K or Min P, so they are refused rather than sent and ignored.
        with self.assertRaisesRegex(ProviderError, r'does not accept: min_p, top_k\.$'):
            self.body(options={'top_k': 40, 'min_p': 0.05, 'temperature': 0.5})

    def test_probe_proves_the_key_before_counting_models(self):
        self.assertEqual(self.keyed().probe(), 'Connected\nAvailable models: 9')
        self.assertEqual(self.server.received, [('/gateway/v1/credits', None, 'Bearer sk-secret'),
                                                ('/gateway/v1/models', None, 'Bearer sk-secret')])
        # The model list is public, so only the balance shows a key was refused.
        del self.server.received[:]
        self.server.credits = (401, {'error': {'message': 'Authentication failed', 'type': 'authentication_error'}})
        with self.assertRaisesRegex(ProviderError, 'rejected the API key, or requires one') as caught:
            self.keyed().probe()
        self.assertEqual(caught.exception.status, 401)
        self.assertEqual([request[0] for request in self.server.received], ['/gateway/v1/credits'])

    def test_credit_and_free_tier_errors_are_the_gateways_own_words(self):
        for status, message, kind in (
                (402, 'Insufficient funds. Add credits to continue.', 'insufficient_funds'),
                (403, 'A valid payment method is required to use this model.', 'customer_verification_required')):
            with self.subTest(status=status):
                self.server.status = status
                self.server.body = json.dumps({'error': {'message': message, 'type': kind}}).encode()
                with self.assertRaises(ProviderError) as caught:
                    list(self.keyed().chat(QWEN, USER))
                self.assertEqual((str(caught.exception), caught.exception.status), (message, status))
                self.assertNotIn('API key', str(caught.exception))
        self.server.status, self.server.body = 429, b'{"error": {"message": "Rate limit exceeded"}}'
        with self.assertRaisesRegex(ProviderError, 'usage or rate limit'):
            self.keyed().embed(EMBEDDER, 'a')

    def test_embedding_model_details_and_call(self):
        provider = self.keyed()
        self.assertEqual(provider.show_model(EMBEDDER), {'capabilities': ['embedding'], 'context_length': 32768,
                                                         'pricing': {'prompt': '0.00000001'}})
        self.assertEqual(provider.embedding_context(EMBEDDER), 32768)
        self.server.embeddings = vectors([0.25, 0.5], [1, 0], order=[1, 0])
        self.assertEqual(provider.embed(EMBEDDER, ['a', 'b'], dimensions=2), {'embeddings': [[0.25, 0.5], [1, 0]]})
        self.assertEqual(self.server.received[-1], ('/gateway/v1/embeddings', {
            'model': EMBEDDER, 'input': ['a', 'b'], 'dimensions': 2}, 'Bearer sk-secret'))

    def test_chat_address_and_no_header_naming_the_application(self):
        headers = []

        class Recording(GatewayHandler):
            def do_POST(self):
                headers.append({name.lower() for name in self.headers.keys()})
                super().do_POST()
        self.server.RequestHandlerClass = Recording
        provider = self.keyed()
        list(provider.chat(QWEN, USER))
        provider.probe()
        self.assertEqual([request[0] for request in self.server.received],
                         ['/gateway/v1/chat/completions', '/gateway/v1/credits', '/gateway/v1/models'])
        for sent in headers:
            self.assertIn('authorization', sent)
            self.assertFalse({'http-referer', 'x-title'} & sent)

    def test_only_the_fixed_address_is_accepted(self):
        elsewhere = VercelProvider(self.fixture.host)
        for call in self.calls(elsewhere) + (elsewhere.probe,):
            with self.assertRaisesRegex(ProviderError, 'Vercel AI Gateway requires'):
                call()
        self.assertEqual(self.server.received, [])
        with self.assertRaisesRegex(ValueError, 'Vercel AI Gateway requires'):
            VercelProvider.check_url(self.fixture.host)
        VercelProvider.check_url(self.url)


class DeclarationTests(unittest.TestCase):
    def test_class_declares_what_the_host_editor_and_panel_read(self):
        cls, caps = VercelProvider, VercelProvider.capabilities
        self.assertEqual((cls.kind, cls.label, cls.default_name, cls.name_example, cls.url_example, cls.fixed_url),
                         ('vercel', 'Vercel AI Gateway', 'Vercel AI Gateway', 'Vercel AI Gateway', '', URL))
        self.assertEqual(URL, 'https://ai-gateway.vercel.sh')
        self.assertEqual((cls.requires_key, cls.key_optional), (True, False))
        self.assertTrue(cls.key_url.startswith('https://'))
        for endpoint in ('/chat/completions', '/models', '/credits', '/embeddings'):
            self.assertEqual(cls(URL)._address(endpoint), 'https://ai-gateway.vercel.sh/v1' + endpoint)
        offered = {name for name, value in vars(caps).items() if value is True}
        self.assertEqual(offered, {'embeddings', 'thinking_levels', 'structured_output', 'tools', 'thinking_text'})
        self.assertEqual((caps.options_notice, caps.left_out), ('', ()))
        self.assertIs(PROVIDERS['vercel'], cls)
        self.assertEqual(list(PROVIDERS).index('vercel'), list(PROVIDERS).index('openrouter') + 1)

    @unittest.skipUnless(Gdk.Display.get_default(), 'GTK smoke tests require a display')
    def test_host_editor_fixes_the_address_and_demands_a_key(self):
        dialog = HostEditDialog()
        model = dialog.provider_dropdown.get_model()
        index = list(PROVIDERS).index('vercel')
        self.assertEqual(model.get_string(index), 'Vercel AI Gateway')
        dialog.provider_dropdown.set_selected(index)
        self.assertEqual((dialog.hostname_entry.get_text(), dialog.name_entry.get_text()), (URL, 'Vercel AI Gateway'))
        self.assertFalse(dialog.hostname_entry.get_editable())
        self.assertEqual((dialog.key_box.get_visible(), dialog.key_link.get_visible(), dialog.key_link.get_uri()),
                         (True, True, VercelProvider.key_url))
        self.assertFalse(dialog.remove_key_check.get_visible())
        self.assertFalse(dialog.get_response_enabled('save'))
        dialog.api_key_entry.set_text('sk-vercel-test')
        self.assertTrue(dialog.get_response_enabled('save'))


@unittest.skipUnless(Gdk.Display.get_default(), 'GTK smoke tests require a display')
class HostTests(Gateway, HostFixture, unittest.TestCase):
    def test_host_needs_a_key_and_the_fixed_address(self):
        with self.assertRaises(CredentialError):
            self.storage.save_host('Keyless', self.url, provider='vercel')
        with self.assertRaisesRegex(ValueError, 'Vercel AI Gateway requires'):
            self.storage.save_host('Elsewhere', self.fixture.host, provider='vercel',
                                   api_key='sk-vercel-test', session_only=True)

    def test_composer_and_panel_show_what_the_list_says(self):
        tab = self.make_tab(QWEN)
        self.assertEqual(self.server.received[0], ('/gateway/v1/models', None, 'Bearer sk-vercel-test'))
        self.assertEqual(self.shown(tab.chat_input), (True, True, '', True, [None, False, True, 'low', 'medium',
                                                                             'high', 'max']))
        panel = tab.options_panel

        def lacking():
            return {name for name, (label, hint) in panel.field_hints.items()
                    if not getattr(panel, name).get_parent().get_sensitive() and label.get_text() == MODEL_HINT}
        self.assertEqual(lacking(), {'top_p_entry', 'seed_entry'})
        for model, shown in ((BEAM, (False, True, '', False, [None, 'low', 'medium', 'high', 'max'])),
                             (LING, (False, True, '', False, [None, False, True])),
                             (NEMO, (False, True, '', False, [None])),
                             (SCALEDOWN, (None, False, '', True, [None]))):
            with self.subTest(model=model):
                self.assertEqual(self.shown(self.select(tab, model)), shown)
        self.select(tab, FABLE)
        self.assertEqual(lacking(), {'temperature_entry', 'top_p_entry', 'seed_entry'})

    def test_tab_sends_the_key_and_the_reasoning_object(self):
        tab = self.make_tab(QWEN)
        composer = tab.chat_input
        composer.thinking_dropdown.set_selected(composer._thinking_values.index('max'))
        path, body, authorization = self.send(tab, 'hello')
        self.assertEqual((path, authorization), ('/gateway/v1/chat/completions', 'Bearer sk-vercel-test'))
        self.assertEqual((body['model'], body['reasoning']), (QWEN, {'effort': 'max'}))
        self.assertNotIn('provider', body)
        composer = self.select(tab, LING)
        for setting, sent in ((False, {'effort': 'none'}), (True, {'enabled': True})):
            composer.thinking_dropdown.set_selected(composer._thinking_values.index(setting))
            self.assertEqual(self.send(tab, 'think')[1]['reasoning'], sent)


class PricingTests(Gateway, unittest.TestCase):
    def test_prices_are_renamed_and_the_rest_left_out(self):
        show = self.provider().show_model
        # A per-search price, regional prices, and tiers are not per-token prices of this model.
        self.assertEqual(show(FABLE)['pricing'], {'prompt': '0.00001', 'completion': '0.00005',
                                                  'input_cache_read': '0.000001', 'input_cache_write': '0.0000125'})
        self.assertEqual(show(LIGHTNING)['pricing'], {'prompt': '0', 'completion': '0'})
        self.assertEqual(show(LING)['pricing'], {'prompt': '0.000000021', 'completion': '0.000000063',
                                                 'input_cache_read': '0.0000000042'})
        self.entry(NEMO)['pricing'] = {'input': 0.1, 'output': '0.00000003'}
        self.assertEqual(show(NEMO)['pricing'], {'completion': '0.00000003'})
        for absent in (None, {}, {'web_search': '10', 'input_tiers': []}):
            with self.subTest(pricing=absent):
                self.entry(NEMO)['pricing'] = absent
                self.assertNotIn('pricing', show(NEMO))


if __name__ == '__main__':
    unittest.main()
