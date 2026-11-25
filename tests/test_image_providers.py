"""Image generation of every host type, against a loopback server with recorded answers; no display needed."""
import base64
import copy
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from gi.repository import Gio
from src import image_generation
from src.credentials import CredentialError, CredentialStore
from src.providers import (LMStudioProvider, OllamaCloudProvider, OllamaProvider, OpenAICompatibleProvider,
                           OpenRouterProvider, ProviderError, RequestCancelled, generates)
from src.providers import imaging
from src.providers.openai import fingerprint
from src.providers.vercel import VercelProvider
from src.services import Services

FIXTURES = Path(__file__).parent / 'fixtures'
ROUTER = json.loads((FIXTURES / 'images_openrouter.json').read_text())
GATEWAY = json.loads((FIXTURES / 'images_vercel.json').read_text())
PNG = ROUTER['answer']['data'][0]['b64_json']
JPEG = base64.b64encode(b'\xff\xd8\xff\xe0' + b'\0' * 16).decode()
PROMPT = 'A small red apple on a white table'
FREE, MAI, GEMINI, SEEDREAM, VECTOR, MUSE = (
    'recraft/recraft-v4.1-flash-v2:free', 'microsoft/mai-image-2.7-flash', 'google/gemini-3.1-flash-image',
    'bytedance-seed/seedream-5-5', 'recraft/recraft-v4.1-vector', 'meta/muse-image')
KLEIN, MUSE_GATEWAY, SEEDREAM_GATEWAY, MAI_GATEWAY = (
    'bfl/flux-3-image-klein', 'meta/muse-image-1.0', 'bytedance/seedream-5.5', 'microsoft/mai-image-2.7-flash')


class Keys(CredentialStore):
    def __init__(self, saved=None):
        super().__init__()
        self.saved = saved or {}

    def lookup(self, host_id, reference, cancellable=None):
        if reference in self.saved:
            return self.saved[reference]
        raise CredentialError('An API key is required')


class Imager(BaseHTTPRequestHandler):
    """Lists models as the recorded lists do, and answers every request to generate with `server.answer`."""
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.do_POST()

    def do_POST(self):
        server = self.server
        raw = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        server.received.append((self.path, json.loads(raw) if raw else None, self.headers.get('Authorization')))
        server.started.set()
        if server.stall:
            server.release.wait(5)
        # Only a request to generate fails; the model list it reads first is always there.
        status = server.status if self.command == 'POST' else 200
        self.send_response(status)
        self.end_headers()
        if status != 200:
            answer = server.body
        else:
            answer = next((listed for suffix, listed in server.lists.items() if self.path.endswith(suffix)),
                          server.answer)
        try:
            self.wfile.write(answer if isinstance(answer, bytes) else json.dumps(answer).encode())
        except (BrokenPipeError, ConnectionResetError):
            pass


class Served:
    """A provider of the type under test, bound to a loopback server."""
    KIND: type = OpenRouterProvider
    PREFIX = ''
    RECORDED: dict = {}

    def setUp(self):
        self.server = server = ThreadingHTTPServer(('127.0.0.1', 0), Imager)
        server.status, server.body, server.stall, server.received = 200, b'', False, []
        server.started, server.release = threading.Event(), threading.Event()
        server.lists = self.lists()
        server.answer = self.answer()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.addCleanup(server.release.set)
        self.url = f'http://127.0.0.1:{server.server_port}' + self.PREFIX
        if self.PREFIX:
            moved = patch.object(self.KIND, 'fixed_url', self.url)
            moved.start()
            self.addCleanup(moved.stop)

    def lists(self):
        return {}

    def answer(self):
        return {}

    def provider(self, **kwargs):
        return self.KIND(self.url, **kwargs)

    def keyed(self):
        return self.provider(host_id='host', credential_id='reference', credentials=Keys({'reference': 'sk-secret'}))

    def sent(self):
        return self.server.received[-1][1]

    def paths(self):
        return [request[0] for request in self.server.received]


class Generating(Served):
    """What every type that generates images must do alike."""
    MODEL = ''

    def generate(self, model=None, options=None, references=None, provider=None, **kwargs):
        return (provider or self.provider()).generate_image(model or self.MODEL, PROMPT, options or {}, references,
                                                            **kwargs)

    def test_every_error_reads_as_what_the_server_said(self):
        for name, error in self.RECORDED['errors'].items():
            with self.subTest(error=name):
                self.server.status, self.server.body = error['status'], json.dumps(error['body']).encode()
                with self.assertRaises(ProviderError) as caught:
                    self.generate(provider=self.keyed())
                self.assertEqual((str(caught.exception), caught.exception.status), (error['says'], error['status']))
        # Without words in the body, the status says what happened.
        self.server.status, self.server.body = 502, b'<html>Bad Gateway</html>'
        with self.assertRaisesRegex(ProviderError, 'HTTP Error 502') as caught:
            self.generate()
        self.assertEqual(caught.exception.status, 502)

    def test_key_goes_with_the_request_and_is_kept_out_of_errors(self):
        self.generate(provider=self.keyed())
        self.assertEqual({request[2] for request in self.server.received}, {'Bearer sk-secret'})
        self.server.status, self.server.body = 400, b'{"error": {"message": "HTTP 400: {\\"detail\\": \\"no sk-secret\\"}"}}'
        with self.assertRaises(ProviderError) as caught:
            self.generate(provider=self.keyed())
        self.assertEqual(str(caught.exception), 'no [redacted]')
        self.server.status = 401
        with self.assertRaisesRegex(ProviderError, 'rejected the API key, or requires one'):
            self.generate(provider=self.keyed())

    def test_cancellation_stops_a_request_that_waits_and_one_not_yet_sent(self):
        stopped = Gio.Cancellable()
        stopped.cancel()
        with self.assertRaises(RequestCancelled):
            self.generate(cancellable=stopped)
        self.assertEqual(self.server.received, [])
        self.server.stall = True
        cancel, errors = Gio.Cancellable(), []

        def run():
            try:
                self.generate(cancellable=cancel)
            except Exception as exc:
                errors.append(exc)
        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(self.server.started.wait(3))
        cancel.cancel()
        thread.join(3)
        self.server.release.set()
        self.assertFalse(thread.is_alive())
        self.assertEqual([type(error) for error in errors], [RequestCancelled])

    def test_image_models_stay_out_of_the_chat_pickers(self):
        provider = self.provider()
        self.assertTrue(provider.capabilities.image_generation and provider.capabilities.image_references)
        entries = provider.list_image_models()
        self.assertTrue(entries)
        for entry in entries:
            self.assertIn('image', entry['capabilities'])
            self.assertFalse(generates(entry))
            self.assertTrue(entry['name'] and entry['digest'])

    def test_a_setting_the_type_cannot_send_is_refused_before_sending(self):
        with self.assertRaisesRegex(ProviderError, 'does not accept: steps'):
            self.generate(options={'steps': 20})
        self.assertFalse([path for path in self.paths() if not path.endswith('models')])


class OpenRouterTests(Generating, unittest.TestCase):
    KIND = OpenRouterProvider
    PREFIX = '/api'
    RECORDED = ROUTER
    MODEL = FREE

    def lists(self):
        return {'/images/models': ROUTER['models'], '/models?output_modalities=decisions': {'data': []},
                '/embeddings/models': {'data': []}, '/models': {'data': [{'id': 'chat/model'}]}}

    def answer(self):
        return ROUTER['answer']

    def test_models_come_from_the_image_list_with_its_settings(self):
        provider = self.provider()
        entries = {entry['name']: entry for entry in provider.list_image_models()}
        self.assertEqual(self.paths(), ['/api/v1/images/models'])
        self.assertEqual(list(entries), [FREE, MAI, GEMINI, SEEDREAM, VECTOR, MUSE])
        recorded = {model['id']: model for model in ROUTER['models']['data']}
        self.assertEqual(entries[FREE], {
            'name': FREE, 'digest': fingerprint(FREE, recorded[FREE]['created']), 'capabilities': ['image'],
            'image_parameters': recorded[FREE]['supported_parameters']})
        self.assertEqual(entries[MAI]['capabilities'], ['image', 'vision'])
        self.assertEqual(entries[MUSE]['image_parameters'], {})
        # What the application offers of them.
        free = provider.image_parameters(entries[FREE])
        self.assertEqual([option.name for option in free.options], ['aspect_ratio', 'n'])
        self.assertEqual((free.count(), free.references, free.streaming), ((1, 6), 0, False))
        seedream = provider.image_parameters(entries[SEEDREAM])
        self.assertEqual([option.name for option in seedream.options],
                         ['aspect_ratio', 'resolution', 'output_format', 'background', 'n', 'seed'])
        self.assertEqual((seedream.references, seedream.get('seed').kind), (10, 'integer'))
        gemini = provider.image_parameters(entries[GEMINI])
        self.assertEqual((gemini.get('resolution').values, gemini.references), (('512', '1K', '2K', '4K'), 14))
        self.assertEqual(provider.image_parameters(provider.list_image_models()[1]).references, 5)
        # A model that streams says so.
        streaming = copy.deepcopy(ROUTER['models'])
        streaming['data'][0]['supports_streaming'] = True
        self.server.lists['/images/models'] = streaming
        entry = provider.list_image_models()[0]
        self.assertIs(entry['image_streaming'], True)
        self.assertTrue(provider.image_parameters(entry).streaming)

    def test_request_goes_to_the_images_endpoint_and_the_answer_comes_back_whole(self):
        answer = self.generate(options={'aspect_ratio': '1:1', 'n': 1})
        self.assertEqual(answer, ROUTER['answer'])
        self.assertEqual(self.paths(), ['/api/v1/images'])
        self.assertEqual(self.sent(), {'model': FREE, 'prompt': PROMPT, 'aspect_ratio': '1:1', 'n': 1,
                                       'response_format': 'b64_json', 'provider': {'require_parameters': True}})
        result = image_generation.parse_result(answer)
        self.assertEqual(result.images, (image_generation.GeneratedImage(PNG, 'image/png'),))
        self.assertEqual((result.cost, result.text), (0.0, ''))
        self.assertEqual(result.extra['usage']['completion_tokens_details']['image_tokens'], 4175)

    def test_only_the_settings_chosen_are_sent(self):
        # Defaults such as PNG output are chosen before a request reaches the provider.
        self.generate(SEEDREAM)
        self.assertEqual(self.sent(), {'model': SEEDREAM, 'prompt': PROMPT,
                                       'response_format': 'b64_json', 'provider': {'require_parameters': True}})
        self.generate(SEEDREAM, {'output_format': 'jpeg', 'seed': 7})
        self.assertEqual((self.sent()['output_format'], self.sent()['seed']), ('jpeg', 7))
        with self.assertRaisesRegex(ProviderError, 'does not accept: size'):
            self.generate(SEEDREAM, {'size': '1024x1024'})

    def test_references_go_as_image_parts_of_data_urls(self):
        self.generate(MAI, references=[PNG, 'data:image/jpeg;base64,' + JPEG])
        self.assertEqual(self.sent()['input_references'], [
            {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + PNG}},
            {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,' + JPEG}}])
        self.generate(MAI, references=[])
        self.assertNotIn('input_references', self.sent())

    def test_an_image_model_is_described_after_the_other_lists(self):
        provider = self.provider()
        self.assertEqual(provider.show_model(MAI), {
            'capabilities': ['image', 'vision'],
            'image_parameters': ROUTER['models']['data'][1]['supported_parameters']})
        self.assertEqual(self.paths(), ['/api/v1/models', '/api/v1/embeddings/models',
                                        '/api/v1/models?output_modalities=decisions', '/api/v1/images/models'])
        self.assertFalse(generates(provider.show_model(FREE)))
        with self.assertRaisesRegex(ProviderError, 'no longer available'):
            provider.show_model('gone')


class VercelTests(Generating, unittest.TestCase):
    KIND = VercelProvider
    PREFIX = '/gateway'
    RECORDED = GATEWAY
    MODEL = KLEIN

    def lists(self):
        return {'/models': GATEWAY['models']}

    def answer(self):
        return GATEWAY['images']

    def test_image_models_and_chat_models_that_make_images_are_listed_with_their_route(self):
        provider = self.provider()
        entries = {entry['name']: entry for entry in provider.list_image_models()}
        self.assertEqual(self.paths(), ['/gateway/v1/models'])
        # A chat model that makes no images, and an embedding model, are not.
        self.assertEqual(list(entries), [KLEIN, MUSE_GATEWAY, SEEDREAM_GATEWAY, MAI_GATEWAY, GEMINI])
        recorded = {model['id']: model for model in GATEWAY['models']['data']}
        shape = {'aspect_ratio': {'type': 'enum', 'values': ['1:1', '16:9', '9:16', '4:3', '3:4']}}
        count = dict(shape, n={'type': 'range', 'min': 1, 'max': 4})
        self.assertEqual(entries[KLEIN], {
            'name': KLEIN, 'digest': fingerprint(KLEIN, recorded[KLEIN]['released']), 'capabilities': ['image'],
            'image_parameters': count, 'route': 'images', 'pricing': {'image': '0.009'}})
        references = {'input_references': {'type': 'range', 'min': 0, 'max': 16}}
        # Images taken as input make references; files alone do not.
        self.assertEqual(entries[MUSE_GATEWAY]['image_parameters'], dict(count, **references))
        self.assertEqual(entries[MUSE_GATEWAY]['capabilities'], ['image', 'vision'])
        self.assertEqual(entries[SEEDREAM_GATEWAY]['image_parameters'], count)
        # Without tags, and priced by token.
        self.assertEqual((entries[MAI_GATEWAY]['image_parameters'], entries[MAI_GATEWAY]['pricing']),
                         (count, {'prompt': '0.00000175', 'completion': '0.00001'}))
        self.assertEqual(entries[GEMINI], {
            'name': GEMINI, 'digest': fingerprint(GEMINI, recorded[GEMINI]['released']),
            'capabilities': ['image', 'vision'], 'route': 'chat',
            'image_parameters': dict(shape, input_references={'type': 'range', 'min': 0, 'max': 14}),
            'pricing': {'prompt': '0.0000005', 'completion': '0.000003', 'input_cache_read': '0.00000005'}})
        klein, gemini = provider.image_parameters(entries[KLEIN]), provider.image_parameters(entries[GEMINI])
        self.assertEqual(([option.name for option in klein.options], klein.count(), klein.references),
                         (['aspect_ratio', 'n'], (1, 4), 0))
        self.assertEqual(([option.name for option in gemini.options], gemini.count(), gemini.references),
                         (['aspect_ratio'], (1, 1), 14))
        # The chat model is described as one; an image-only model after every other list.
        self.assertIn('completion', provider.show_model(GEMINI)['capabilities'])
        self.assertEqual(provider.show_model(KLEIN), {key: value for key, value in entries[KLEIN].items()
                                                       if key not in ('name', 'digest')})

    def test_an_image_only_model_is_asked_through_the_images_api(self):
        answer = self.generate(options={'n': 2})
        self.assertEqual(answer, GATEWAY['images'])
        self.assertEqual(self.paths(), ['/gateway/v1/models', '/gateway/v1/images/generations'])
        self.assertEqual(self.sent(), {'model': KLEIN, 'prompt': PROMPT, 'n': 2, 'response_format': 'b64_json'})
        result = image_generation.parse_result(answer)
        self.assertEqual(result.images, (image_generation.GeneratedImage(PNG, 'image/png'),) * 2)
        self.assertIsNone(result.cost)
        self.generate()
        self.assertEqual(self.sent(), {'model': KLEIN, 'prompt': PROMPT, 'response_format': 'b64_json'})
        self.generate(options={'aspect_ratio': '16:9'})
        self.assertEqual(self.sent()['aspect_ratio'], '16:9')
        with self.assertRaisesRegex(ProviderError, 'does not accept: seed'):
            self.generate(options={'seed': 1})

    def test_references_to_an_image_only_model_go_to_the_edits_api(self):
        self.generate(MUSE_GATEWAY, {'n': 1}, [PNG, 'data:image/jpeg;base64,' + JPEG])
        self.assertEqual(self.paths()[-1], '/gateway/v1/images/edits')
        self.assertEqual(self.sent(), {'model': MUSE_GATEWAY, 'prompt': PROMPT, 'n': 1, 'images': [
            {'image_url': 'data:image/png;base64,' + PNG}, {'image_url': 'data:image/jpeg;base64,' + JPEG}]})

    def test_a_chat_model_is_asked_through_chat_and_may_answer_without_an_image(self):
        self.server.answer = GATEWAY['chat']
        answer = self.generate(GEMINI)
        self.assertEqual(self.paths(), ['/gateway/v1/models', '/gateway/v1/chat/completions'])
        self.assertEqual(self.sent(), {'model': GEMINI, 'messages': [{'role': 'user', 'content': PROMPT}],
                                       'modalities': ['text', 'image'], 'stream': False})
        result = image_generation.parse_result(answer)
        self.assertEqual((result.images, result.text), ((image_generation.GeneratedImage(PNG, 'image/png'),),
                                                        'Here is a small red apple on a white table.'))
        self.generate(GEMINI, references=[PNG])
        self.assertEqual(self.sent()['messages'], [{'role': 'user', 'content': [
            {'type': 'text', 'text': PROMPT},
            {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + PNG}}]}])
        self.server.answer = GATEWAY['chat_text']
        result = image_generation.parse_result(self.generate(GEMINI))
        self.assertEqual((result.images, result.text), ((), "I can't create that image, but I can describe it."))
        self.generate(GEMINI, {'aspect_ratio': '9:16'})
        self.assertEqual(self.sent()['image_config'], {'aspect_ratio': '9:16'})
        # Chat takes no count.
        sent = len(self.server.received)
        with self.assertRaisesRegex(ProviderError, 'does not accept: n'):
            self.generate(GEMINI, {'n': 2})
        self.assertEqual(self.paths()[sent:], ['/gateway/v1/models'])


class UnavailableTests(Served, unittest.TestCase):
    KIND = OllamaProvider

    def test_types_without_image_models_offer_nothing(self):
        for kind in (OllamaProvider, OllamaCloudProvider, LMStudioProvider, OpenAICompatibleProvider):
            with self.subTest(kind=kind.kind):
                provider = kind(self.url)
                self.assertFalse(provider.capabilities.image_generation or provider.capabilities.image_references)
                self.assertIn('does not offer image generation', image_generation.check_request(
                    PROMPT, [], {}, provider.capabilities, image_generation.ImageParameters()))
                self.assertEqual(provider.image_parameters({'image_parameters': {'n': {'type': 'range', 'min': 1,
                                                                                         'max': 4}}}),
                                 image_generation.ImageParameters())
                with self.assertRaises(NotImplementedError):
                    provider.list_image_models()
                with self.assertRaises(NotImplementedError):
                    provider.generate_image('model', PROMPT, {})
        self.assertEqual(self.server.received, [])

    def test_capability_records_combine_with_the_new_flags(self):
        router, ollama = OpenRouterProvider.capabilities, OllamaProvider.capabilities
        self.assertTrue(router.both(router).image_generation and router.both(router).image_references)
        self.assertFalse(router.both(ollama).image_generation or ollama.both(router).image_references)


class HelperTests(unittest.TestCase):
    def test_validation_errors_name_the_setting_they_are_about(self):
        readable = imaging.readable
        self.assertEqual(readable({'message': 'Bad', 'param': 'size'}), {'message': 'size: Bad', 'param': 'size'})
        issues = json.dumps([{'path': ['n'], 'message': 'Too big'}, {'path': [], 'message': 'Odd'}])
        self.assertEqual(readable({'name': 'ZodError', 'message': issues})['message'], 'n: Too big\nOdd')
        for left in (None, 'text', {'message': '[1, 2]'}, {'message': 'plain', 'param': {'modelId': 'x'}},
                     {'code': 3}):
            self.assertEqual(readable(left), left)

    def test_the_fixture_image_is_a_png(self):
        self.assertTrue(base64.b64decode(PNG).startswith(b'\x89PNG'))
        self.assertEqual(image_generation.media_type(PNG), 'image/png')


class CatalogTests(Served, unittest.TestCase):
    """The shared catalog reads an image model list once for everyone who asks."""
    PREFIX = '/api'

    def lists(self):
        return {'/images/models': ROUTER['models'], '/models': {'data': [{'id': 'chat/model'}]}}

    def setUp(self):
        super().setUp()
        self.services = Services()
        self.addCleanup(self.services.shutdown)

    def test_image_lists_are_cached_apart_from_chat_lists_and_refreshed_on_request(self):
        catalog, provider = self.services.catalog, self.provider()
        first = catalog.request_image_models(provider).result(3)
        self.assertEqual(first, provider.list_image_models())
        del self.server.received[:]
        self.assertEqual(catalog.request_image_models(provider).result(3), first)
        self.assertEqual(self.server.received, [])
        self.assertEqual([entry['name'] for entry in catalog.request_models(provider).result(3)], ['chat/model'])
        self.assertEqual(catalog.request_image_models(provider, refresh=True).result(3), first)
        self.assertEqual(self.paths(), ['/api/v1/models', '/api/v1/images/models'])
        stopped = Gio.Cancellable()
        stopped.cancel()
        with self.assertRaises(RequestCancelled):
            catalog.request_image_models(provider, stopped).result(3)
        with self.assertRaises(NotImplementedError):
            catalog.request_image_models(OpenAICompatibleProvider(self.url)).result(3)


if __name__ == '__main__':
    unittest.main()
