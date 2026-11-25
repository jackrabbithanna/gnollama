"""The OpenAI host type against a loopback server that answers with a recorded model list and recorded streams."""
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from gi.repository import Gdk
from src.credentials import CredentialError
from src.host_manager import HostEditDialog
from src.providers import PROVIDERS, ProviderError, generates, supports
from src.providers.openai import fingerprint
from src.providers.openai_platform import URL, OpenAIProvider, base_name, input_items, kind, thinking_levels
from src.records import WireOptions
from test_lmstudio import SCHEMA, HostFixture, KeyChecks, Loopback
from test_openai import PNG, TOOLS, USER, vectors
from test_ui import pump_until

NANO, MINI41, LUNA, SOL, GPT5, LATEST = 'gpt-5.4-nano', 'gpt-4.1-mini', 'gpt-6-luna', 'gpt-6.2-sol', 'gpt-5', 'chat-latest'
EMBEDDER, IMAGE = 'text-embedding-3-small', 'gpt-image-1-mini'
FIXTURES = Path(__file__).parent / 'fixtures'
MODEL_HINT = 'Not available for this model.'


def recorded_stream(name):
    """The nonblank lines of a stream recorded from the API."""
    return [line for line in (FIXTURES / f'openai_responses_{name}.txt').read_bytes().split(b'\n') if line.strip()]


def event(kind, **fields):
    return b'data: ' + json.dumps(dict(type=kind, **fields)).encode()


COMPLETED = event('response.completed', response={'status': 'completed', 'usage': {
    'input_tokens': 7, 'output_tokens': 2, 'input_tokens_details': {'cached_tokens': 3}}})
ANSWER = [b'event: response.output_text.delta', event('response.output_text.delta', delta='Hel'),
          event('response.output_text.delta', delta='lo'), COMPLETED]


class Platform(Loopback):
    """The one address the type accepts, moved to the loopback server."""
    RECORDED = 'openai_models.json'
    KIND = OpenAIProvider
    MODEL = NANO
    EMBEDDER = EMBEDDER
    NAME = 'OpenAI'

    def locate(self):
        self.url = self.fixture.host + '/openai'
        moved = patch.object(OpenAIProvider, 'fixed_url', self.url)
        moved.start()
        self.addCleanup(moved.stop)
        self.server.lines = ANSWER

    def save_host(self):
        return self.storage.save_host(self.NAME, self.url, True, provider='openai_official',
                                      api_key='sk-openai-test', session_only=True)

    def body(self, model=NANO, **kwargs):
        return self.provider().request_body(model, USER, **kwargs)

    def chat(self, lines, model=NANO, **kwargs):
        self.server.lines = lines
        return list(self.provider().chat(model, USER, **kwargs))


class ModelTests(Platform, KeyChecks, unittest.TestCase):
    def test_one_list_is_split_by_what_each_model_does(self):
        provider = self.provider()
        listed = {model['id']: model for model in self.server.models['data']}
        chats = provider.list_models()
        self.assertEqual({request[0] for request in self.server.received}, {'/openai/v1/models'})
        names = [model['name'] for model in chats]
        self.assertEqual(len(names), 53)
        self.assertLessEqual({NANO, MINI41, LUNA, SOL, GPT5, LATEST, 'o3', 'gpt-5.3-codex', 'gpt-6-astra-healthcare',
                              'gpt-5.4-nano-2026-03-17'}, set(names))
        # Speech, transcription, realtime, moderation, and search are not chat models here.
        for left_out in ('tts-1', 'whisper-1', 'gpt-4o-mini-tts', 'gpt-4o-transcribe-diarize', 'gpt-audio',
                         'gpt-realtime-2.1-mini', 'gpt-live-1', 'omni-moderation-latest', 'gpt-5-search-api',
                         EMBEDDER, IMAGE):
            self.assertIn(left_out, listed)
            self.assertNotIn(left_out, names)
        self.assertEqual(chats[names.index(NANO)], {'name': NANO, 'digest': fingerprint(NANO, listed[NANO]['created'])})
        self.assertEqual([model['name'] for model in provider.list_embedding_models()],
                         ['text-embedding-3-small', 'text-embedding-ada-002', 'text-embedding-3-large'])
        self.assertEqual(provider.list_embedding_models()[0]['context_length'], 8191)
        self.assertEqual(provider.model_tag(EMBEDDER)['name'], EMBEDDER)
        self.assertEqual(provider.model_tag(NANO), chats[names.index(NANO)])
        self.assertIsNone(provider.model_tag(IMAGE))
        with self.assertRaisesRegex(ProviderError, 'no longer available'):
            provider.show_model('gone')
        with self.assertRaisesRegex(ProviderError, 'no longer available'):
            provider.show_model('whisper-1')
        with self.assertRaisesRegex(ProviderError, 'no longer available'):
            provider.embedding_context(NANO)
        self.assertEqual(provider.embedding_context(EMBEDDER), 8191)
        # A new release of the same name is another model.
        listed[NANO]['created'] += 1
        self.assertNotEqual(provider.model_tag(NANO)['digest'], chats[names.index(NANO)]['digest'])

    def test_names_are_read_without_their_date_variant_or_fine_tune(self):
        for name, base in (('gpt-5.4-nano-2026-03-17', NANO), ('gpt-6.2-astra-healthcare', 'gpt-6.2-astra'),
                           ('ft:gpt-4.1-mini:acme::abc123', MINI41), (NANO, NANO)):
            self.assertEqual(base_name(name), base)
        self.assertEqual([kind(name) for name in ('ft:gpt-4.1-mini:acme::abc123', 'chatgpt-image-latest',
                                                  'text-embedding-ada-002', 'gpt-realtime', 'dall-e-3', 'whisper-1')],
                         ['chat', 'image', 'embedding', None, None, None])

    def test_details_come_from_the_table(self):
        show = self.provider().show_model
        self.assertEqual(show(MINI41), {
            'capabilities': ['completion', 'vision', 'tools'],
            'supported_parameters': ['max_tokens', 'response_format', 'tools', 'temperature', 'top_p', 'logprobs'],
            'context_length': 1047576, 'max_completion_tokens': 32768, 'shutdown_date': '2027-01-14'})
        self.assertEqual(show(SOL), {
            'capabilities': ['completion', 'vision', 'tools', 'thinking'],
            'supported_parameters': ['max_tokens', 'response_format', 'tools', 'reasoning'],
            'thinking_levels': ['low', 'medium', 'high', 'max']})
        # A model with a shutdown date says so.
        self.assertEqual(show('o3-2025-04-16')['shutdown_date'], '2026-12-11')
        self.assertEqual(show(GPT5)['context_length'], 400000)
        for model in (NANO, LUNA, GPT5, LATEST):
            with self.subTest(model=model):
                details = show(model)
                self.assertEqual([supports(details, name) for name in ('vision', 'tools', 'thinking')], [True] * 3)
                self.assertTrue(generates(details))
        self.assertEqual(show(EMBEDDER), {'capabilities': ['embedding'], 'context_length': 8191})
        self.assertFalse(generates(show(EMBEDDER)))
        self.assertEqual(show(IMAGE)['capabilities'], ['image', 'vision'])

    def test_thinking_levels_follow_the_efforts_each_model_was_seen_to_take(self):
        show = self.provider().show_model
        expected = {NANO: [False, 'low', 'medium', 'high', 'max'],  # Max is its xhigh.
                    LUNA: [False, 'low', 'medium', 'high', 'max'],
                    SOL: ['low', 'medium', 'high', 'max'],  # No way to stop.
                    GPT5: ['low', 'medium', 'high'],  # Minimal is not offered.
                    'gpt-5-pro': ['high'], 'gpt-5.5-pro': ['medium', 'high', 'max'], LATEST: ['medium'],
                    'o3': ['low', 'medium', 'high'],
                    'gpt-6-astra-healthcare': ['low', 'medium', 'high', 'max']}
        for model, levels in expected.items():
            with self.subTest(model=model):
                self.assertEqual(show(model)['thinking_levels'], levels)
        self.assertNotIn('thinking_levels', show(MINI41))
        self.assertEqual(thinking_levels(('none', 'minimal', 'high')), [False, 'high'])

    def test_narrowing_leaves_out_what_a_model_refuses(self):
        provider = self.provider()

        def record(model):
            return provider.model_capabilities(provider.show_model(model))
        self.assertEqual(provider.capabilities.left_out, ('seed', 'stop'))
        for model in (NANO, MINI41, LUNA, SOL, GPT5):
            with self.subTest(model=model):
                self.assertLessEqual({'seed', 'stop'}, set(record(model).left_out))
        self.assertEqual(record(SOL).left_out, ('temperature', 'top_p', 'seed', 'stop'))
        self.assertFalse(record(SOL).logprobs)
        self.assertFalse(record(MINI41).thinking_levels or record(MINI41).thinking_text)
        self.assertTrue(record(MINI41).logprobs and record(NANO).logprobs and record(NANO).thinking_text)

    def test_sampling_on_a_reasoning_model_needs_thinking_off(self):
        provider, host = self.provider(), {'name': 'OpenAI'}

        def check(model, **wire):
            provider.check(WireOptions(**wire), host, model, provider.show_model(model))
        warm = {'options': {'temperature': 0.5}}
        for model, allowed in ((MINI41, (None, False)), (NANO, (None, False)), (LUNA, (False,))):
            for thinking in (None, False, 'low', 'max'):
                for wire in (warm, {'options': {'top_p': 0.5}}, {'logprobs': True}):
                    with self.subTest(model=model, thinking=thinking, wire=wire):
                        if model == MINI41 and thinking not in (None, False):
                            continue  # It does not think.
                        if thinking in allowed:
                            check(model, thinking=thinking, **wire)
                        else:
                            with self.assertRaisesRegex(ValueError, r'OpenAI \(.+\) takes Temperature, Top P, and '
                                                                    'logprobs only with thinking Off'):
                                check(model, thinking=thinking, **wire)
        # Where it never takes them, they are refused as any option a model lacks.
        with self.assertRaisesRegex(ValueError, 'does not support: Temperature'):
            check(SOL, thinking=False, **warm)
        with self.assertRaisesRegex(ValueError, 'does not support: Seed'):
            check(MINI41, options={'seed': 1})
        check(LUNA, thinking='max', options={'num_predict': 100})

    def test_request_is_a_responses_body_that_stores_nothing(self):
        body = self.body()
        self.assertEqual(body, {'model': NANO, 'input': [{'role': 'user', 'content': 'hello'}], 'stream': True,
                                'store': False, 'reasoning': {'summary': 'auto'}})
        self.assertNotIn('reasoning', self.body(MINI41))
        for model, setting, sent in ((NANO, False, 'none'), (NANO, 'low', 'low'), (NANO, 'max', 'xhigh'),
                                     (LUNA, 'max', 'max'), (SOL, 'high', 'high'), (LATEST, 'medium', 'medium')):
            with self.subTest(model=model, setting=setting):
                self.assertEqual(self.body(model, thinking=setting)['reasoning'], {'summary': 'auto', 'effort': sent})
        for model, refused in ((SOL, False), (GPT5, 'max'), (NANO, True), (NANO, 'xhigh'), (MINI41, 'low'),
                               (LATEST, 'low'), (NANO, 1)):
            with self.subTest(model=model, refused=refused), \
                    self.assertRaisesRegex(ProviderError, 'Invalid thinking setting'):
                self.body(model, thinking=refused)
        self.assertEqual(self.body(options={'temperature': 0.5, 'top_p': 0.9, 'num_predict': 64}),
                         dict(body, temperature=0.5, top_p=0.9, max_output_tokens=64))
        self.assertNotIn('max_output_tokens', self.body(options={'num_predict': -1}))
        with self.assertRaisesRegex(ProviderError, r'does not accept: seed, stop, keep_alive\.$'):
            self.body(options={'seed': 1, 'stop': ['x']}, keep_alive=60)
        self.assertEqual(self.body(format='json')['text'], {'format': {'type': 'json_object'}})
        self.assertEqual(self.body(format=SCHEMA)['text'], {'format': {
            'type': 'json_schema', 'name': 'response', 'schema': SCHEMA, 'strict': False}})
        with self.assertRaisesRegex(ProviderError, 'Invalid output format'):
            self.body(format='yaml')
        logprobs = self.body(MINI41, logprobs=True, top_logprobs=2)
        self.assertEqual((logprobs['include'], logprobs['top_logprobs']), (['message.output_text.logprobs'], 2))
        self.assertNotIn('top_logprobs', self.body(MINI41, logprobs=True))
        with self.assertRaisesRegex(ProviderError, 'between 0 and 20'):
            self.body(MINI41, logprobs=True, top_logprobs=21)
        # Strict mode would refuse a schema that leaves anything open, so it is off.
        self.assertEqual(self.body(tools=TOOLS)['tools'], [{'type': 'function', 'strict': False, 'name': 'lookup',
                                                            'parameters': TOOLS[0]['function']['parameters']}])
        with self.assertRaisesRegex(ProviderError, 'does not accept: web_search'):
            self.body(tools=[{'type': 'web_search'}])

    def test_history_becomes_input_items(self):
        history = [
            {'role': 'system', 'content': 'Be brief.'},
            {'role': 'user', 'content': 'Weather?', 'images': [PNG]},
            {'role': 'assistant', 'content': 'Checking.', 'tool_calls': [
                {'id': 'call_a', 'function': {'name': 'weather', 'arguments': {'city': 'Paris'}}},
                {'function': {'name': 'time', 'arguments': {}}}]},
            {'role': 'tool', 'content': 'Sunny', 'tool_name': 'weather'},
            {'role': 'tool', 'content': '12:00', 'tool_name': 'time'},
            {'role': 'assistant', 'content': ''},
            {'role': 'user', 'content': 'Thanks'}]
        self.assertEqual(input_items(history, images=[PNG]), [
            {'role': 'system', 'content': 'Be brief.'},
            {'role': 'user', 'content': [{'type': 'input_text', 'text': 'Weather?'},
                                         {'type': 'input_image', 'image_url': 'data:image/png;base64,' + PNG}]},
            {'role': 'assistant', 'content': 'Checking.'},
            {'type': 'function_call', 'call_id': 'call_a', 'name': 'weather', 'arguments': '{"city": "Paris"}'},
            {'type': 'function_call', 'call_id': 'call00001', 'name': 'time', 'arguments': '{}'},
            {'type': 'function_call_output', 'call_id': 'call_a', 'output': 'Sunny'},
            {'type': 'function_call_output', 'call_id': 'call00001', 'output': '12:00'},
            {'role': 'user', 'content': [{'type': 'input_text', 'text': 'Thanks'},
                                         {'type': 'input_image', 'image_url': 'data:image/png;base64,' + PNG}]}])

    def test_recorded_stream_with_a_reasoning_summary(self):
        chunks = self.chat(recorded_stream('think'), thinking='high')
        self.assertEqual(self.server.received[-1][0], '/openai/v1/responses')
        thinking = ''.join(chunk['message']['thinking'] for chunk in chunks)
        content = ''.join(chunk['message']['content'] for chunk in chunks)
        self.assertTrue(thinking.startswith('**'), thinking)
        self.assertIn('3', content)
        self.assertEqual({key: value for key, value in chunks[-1].items() if key != 'message'},
                         {'done': True, 'done_reason': 'stop', 'prompt_eval_count': 26, 'eval_count': 139,
                          'prompt_eval_cached_count': 0})
        self.assertNotIn('tool_calls', chunks[-1]['message'])

    def test_summary_parts_are_paragraphs(self):
        part = event('response.reasoning_summary_part.added', item_id='rs', summary_index=0)
        lines = [part, event('response.reasoning_summary_text.delta', delta='One.'),
                 event('response.reasoning_summary_part.added', item_id='rs', summary_index=1),
                 event('response.reasoning_summary_text.delta', delta='Two.'), COMPLETED]
        self.assertEqual(''.join(chunk['message']['thinking'] for chunk in self.chat(lines)), 'One.\n\nTwo.')

    def test_recorded_stream_with_two_tool_calls(self):
        chunks = self.chat(recorded_stream('tool'), tools=TOOLS)
        last = chunks[-1]
        self.assertEqual((last['done'], last['done_reason']), (True, 'tool_calls'))
        self.assertEqual(last['message']['tool_calls'], [
            {'function': {'name': 'get_weather', 'arguments': {'city': 'Paris'}},
             'id': 'call_7fa957f5a641490fb69c9d74c19ef60e'},
            {'function': {'name': 'get_weather', 'arguments': {'city': 'Rome'}},
             'id': 'call_a4baa714963e4053a749dc70d9d58982'}])
        # Arguments built from deltas alone, where the item never says them whole.
        lines = [event('response.output_item.added', output_index=0,
                       item={'type': 'function_call', 'call_id': 'c1', 'name': 'lookup', 'arguments': ''}),
                 event('response.function_call_arguments.delta', output_index=0, delta='{"q": '),
                 event('response.function_call_arguments.delta', output_index=0, delta='"x"}'), COMPLETED]
        self.assertEqual(self.chat(lines)[-1]['message']['tool_calls'],
                         [{'function': {'name': 'lookup', 'arguments': {'q': 'x'}}, 'id': 'c1'}])

    def test_recorded_logprobs_and_a_response_cut_short(self):
        chunks = self.chat(recorded_stream('logprobs'), model=MINI41, logprobs=True, top_logprobs=2)
        tokens = [token for chunk in chunks for token in chunk.get('logprobs', [])]
        self.assertEqual(''.join(token['token'] for token in tokens),
                         ''.join(chunk['message']['content'] for chunk in chunks))
        self.assertEqual(len(tokens[0]['top_logprobs']), 2)
        cut = self.chat(recorded_stream('cut'), model=MINI41, options={'num_predict': 16})
        self.assertEqual((cut[-1]['done_reason'], cut[-1]['eval_count']), ('length', 16))

    def test_failures_in_the_stream_and_an_early_end(self):
        failed = event('response.failed', response={'status': 'failed', 'error': {
            'code': 'server_error', 'message': 'The model had an error.'}})
        for lines, message in (([event('response.output_text.delta', delta='Hi'), failed], 'The model had an error.'),
                               ([event('error', code='rate_limit_exceeded', message='Slow down.')], 'Slow down.'),
                               ([b'data: [1, 2]'], 'Invalid JSON')):
            with self.subTest(message=message), self.assertRaisesRegex(ProviderError, message):
                self.chat(lines)
        # Text that arrived is handed over; the missing end is left to the caller to report.
        chunks = self.chat([event('response.output_text.delta', delta='Hi')])
        self.assertEqual([chunk['message']['content'] for chunk in chunks], ['Hi'])
        self.assertNotIn('done', chunks[-1])

    def test_errors_in_openais_words_where_they_are_not_about_the_key(self):
        cases = ((403, {'message': 'Your organization must be verified to use the model `gpt-image-1`.',
                        'type': 'invalid_request_error', 'code': None}, 'must be verified'),
                 (429, {'message': 'You exceeded your current quota, please check your plan and billing details.',
                        'type': 'insufficient_quota', 'code': 'insufficient_quota'}, 'exceeded your current quota'),
                 (429, {'message': 'Rate limit reached for requests', 'type': 'requests',
                        'code': 'rate_limit_exceeded'}, 'usage or rate limit'),
                 (404, {'message': 'The model `gone` does not exist or you do not have access to it.',
                        'code': 'model_not_found'}, 'does not exist'))
        for status, error, says in cases:
            with self.subTest(status=status, says=says):
                self.server.status, self.server.body = status, json.dumps({'error': error}).encode()
                with self.assertRaisesRegex(ProviderError, says) as caught:
                    list(self.keyed().chat(NANO, USER))
                self.assertEqual(caught.exception.status, status)

    def test_embeddings_and_probe(self):
        provider = self.keyed()
        self.server.embeddings = vectors([0.25, 0.5], [1, 0], order=[1, 0])
        self.assertEqual(provider.embed(EMBEDDER, ['a', 'b'], dimensions=2), {'embeddings': [[0.25, 0.5], [1, 0]]})
        self.assertEqual(self.server.received[-1], ('/openai/v1/embeddings', {
            'model': EMBEDDER, 'input': ['a', 'b'], 'dimensions': 2}, 'Bearer sk-secret'))
        self.assertEqual(provider.probe(), 'Connected\nAvailable models: 53')
        self.assertEqual(self.server.received[-1], ('/openai/v1/models', None, 'Bearer sk-secret'))

    def test_only_the_fixed_address_is_accepted(self):
        elsewhere = OpenAIProvider(self.fixture.host)
        for call in self.calls(elsewhere) + (elsewhere.probe,):
            with self.assertRaisesRegex(ProviderError, 'OpenAI requires'):
                call()
        self.assertEqual(self.server.received, [])
        with self.assertRaisesRegex(ValueError, 'OpenAI requires'):
            OpenAIProvider.check_url(self.fixture.host)
        OpenAIProvider.check_url(self.url)


class DeclarationTests(unittest.TestCase):
    def test_class_declares_what_the_host_editor_and_panel_read(self):
        cls, caps = OpenAIProvider, OpenAIProvider.capabilities
        self.assertEqual((cls.kind, cls.label, cls.default_name, cls.name_example, cls.url_example, cls.fixed_url),
                         ('openai_official', 'OpenAI', 'OpenAI', 'OpenAI', '', URL))
        self.assertEqual(URL, 'https://api.openai.com')
        self.assertEqual((cls.requires_key, cls.key_optional), (True, False))
        self.assertEqual(cls.key_url, 'https://platform.openai.com/api-keys')
        for endpoint in ('/responses', '/models', '/embeddings', '/images/generations'):
            self.assertEqual(cls(URL)._address(endpoint), 'https://api.openai.com/v1' + endpoint)
        offered = {name for name, value in vars(caps).items() if value is True}
        self.assertEqual(offered, {'embeddings', 'thinking_levels', 'structured_output', 'tools', 'logprobs',
                                   'thinking_text', 'image_generation', 'image_references', 'decisions',
                                   'decision_images'})
        self.assertEqual((caps.options_notice, caps.left_out), ('', ('seed', 'stop')))
        self.assertIs(PROVIDERS['openai_official'], cls)
        self.assertEqual(list(PROVIDERS).index('openai_official'), list(PROVIDERS).index('vercel') + 1)

    @unittest.skipUnless(Gdk.Display.get_default(), 'GTK smoke tests require a display')
    def test_host_editor_fixes_the_address_and_demands_a_key(self):
        dialog = HostEditDialog()
        model = dialog.provider_dropdown.get_model()
        index = list(PROVIDERS).index('openai_official')
        self.assertEqual(model.get_string(index), 'OpenAI')
        dialog.provider_dropdown.set_selected(index)
        self.assertEqual((dialog.hostname_entry.get_text(), dialog.name_entry.get_text()), (URL, 'OpenAI'))
        self.assertFalse(dialog.hostname_entry.get_editable())
        self.assertEqual((dialog.key_box.get_visible(), dialog.key_link.get_visible(), dialog.key_link.get_uri()),
                         (True, True, OpenAIProvider.key_url))
        self.assertFalse(dialog.get_response_enabled('save'))
        dialog.api_key_entry.set_text('sk-openai-test')
        self.assertTrue(dialog.get_response_enabled('save'))


@unittest.skipUnless(Gdk.Display.get_default(), 'GTK smoke tests require a display')
class HostTests(Platform, HostFixture, unittest.TestCase):
    def send(self, tab, text):
        self.server.lines = ANSWER
        tab.chat_input.entry.set_text(text)
        tab.on_send_clicked()
        pump_until(lambda: tab.request is None and self.storage.writer.idle)
        return self.server.received[-1]

    def test_host_needs_a_key_and_the_fixed_address(self):
        with self.assertRaises(CredentialError):
            self.storage.save_host('Keyless', self.url, provider='openai_official')
        with self.assertRaisesRegex(ValueError, 'OpenAI requires'):
            self.storage.save_host('Elsewhere', self.fixture.host, provider='openai_official',
                                   api_key='sk-openai-test', session_only=True)

    def test_composer_and_panel_show_what_the_table_says(self):
        tab = self.make_tab(NANO)
        self.assertEqual(self.server.received[0], ('/openai/v1/models', None, 'Bearer sk-openai-test'))
        self.assertEqual(self.shown(tab.chat_input), (True, True, '', True, [None, False, 'low', 'medium', 'high',
                                                                             'max']))
        panel = tab.options_panel

        def lacking():
            return {name for name, (label, hint) in panel.field_hints.items()
                    if not getattr(panel, name).get_parent().get_sensitive() and label.get_text() == MODEL_HINT}
        self.assertLessEqual({'seed_entry'}, lacking())
        self.assertNotIn('temperature_entry', lacking())
        self.assertEqual(self.shown(self.select(tab, SOL))[4], [None, 'low', 'medium', 'high', 'max'])
        self.assertLessEqual({'temperature_entry', 'top_p_entry', 'seed_entry'}, lacking())
        self.assertEqual(self.shown(self.select(tab, MINI41))[4], [None])

    def test_tab_sends_a_responses_request_with_the_effort(self):
        tab = self.make_tab(NANO)
        composer = tab.chat_input
        composer.thinking_dropdown.set_selected(composer._thinking_values.index('max'))
        path, body, authorization = self.send(tab, 'hello')
        self.assertEqual((path, authorization), ('/openai/v1/responses', 'Bearer sk-openai-test'))
        self.assertEqual((body['model'], body['reasoning'], body['store']),
                         (NANO, {'summary': 'auto', 'effort': 'xhigh'}, False))
        self.assertEqual(body['input'][-1], {'role': 'user', 'content': 'hello'})
        # The answer streamed back is shown, and the history sent next holds it.
        body = self.send(tab, 'again')[1]
        self.assertEqual(body['input'][-2:], [{'role': 'assistant', 'content': 'Hello'},
                                              {'role': 'user', 'content': 'again'}])
