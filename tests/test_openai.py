"""The OpenAI-compatible provider against a loopback server, and its request translation."""
import base64
import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from gi.repository import Gdk, Gio
from src.app_context import AppContext
from src.comparison import ComparisonRun, prepare_run
from src.credentials import CredentialError, CredentialStore
from src.model_conversation import validate_participants
from src.providers import (CLOUD_URL, OllamaCloudProvider, OllamaProvider, OpenAICompatibleProvider,
                           ProviderError, RequestCancelled)
from src.providers.openai import chat_body, wire_messages
from src.records import RequestSettings, WireOptions
from src.session import api_messages
from src.tab import GenerationTab
from src.tool_calling import inspect_calls, result_messages
from src.widgets.chat_input import ChatInput
from test_ui import pump_until, wait_for_model

TOOLS = [{'type': 'function', 'function': {'name': 'lookup', 'parameters': {
    'type': 'object', 'properties': {'q': {'type': 'string'}}, 'required': ['q']}}}]
PNG = base64.b64encode(b'\x89PNG\r\n\x1a\n' + b'\0' * 16).decode()
JPEG = base64.b64encode(b'\xff\xd8\xff\xe0' + b'\0' * 16).decode()
GIF = base64.b64encode(b'GIF89a' + b'\0' * 16).decode()
WEBP = base64.b64encode(b'RIFF\x10\0\0\0WEBPVP8 ' + b'\0' * 8).decode()
USER = [{'role': 'user', 'content': 'hello'}]


def event(delta=None, finish=None, **extra):
    choices = [] if delta is None and finish is None else [dict(index=0, delta=delta or {}, finish_reason=finish)]
    return b'data: ' + json.dumps(dict(id='c1', object='chat.completion.chunk', choices=choices, **extra)).encode()


DONE = b'data: [DONE]'
ANSWER = [event({'role': 'assistant', 'content': ''}), event({'content': 'Hel'}), event({'content': 'lo'}),
          event({}, 'stop'), event(usage={'prompt_tokens': 7, 'completion_tokens': 2, 'total_tokens': 9,
                                          'prompt_tokens_details': {'cached_tokens': 3}}), DONE]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.do_POST()

    def do_POST(self):
        server = self.server
        raw = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        server.received.append((self.path, json.loads(raw) if raw else None, self.headers.get('Authorization')))
        server.started.set()
        self.send_response(server.status)
        self.end_headers()
        try:
            if server.status != 200:
                self.wfile.write(server.body)
            elif self.path.endswith('/models'):
                self.wfile.write(json.dumps(server.models).encode())
            else:
                for line in server.lines:
                    self.wfile.write(line + b'\n\n')
                    self.wfile.flush()
                # Comments keep the stream open; writing one to a closed socket fails.
                while server.stall and not server.release.wait(0.02):
                    self.wfile.write(b': waiting\n\n')
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            server.dropped.set()


class Server:
    """A loopback OpenAI-compatible server that sends the lines it is given."""
    def __init__(self, lines=ANSWER):
        self.server = server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.lines, server.status, server.body, server.stall = list(lines), 200, b'', False
        server.models = {'object': 'list', 'data': [{'id': 'test', 'object': 'model'}, {'id': 'other'}]}
        server.received = []
        server.started, server.release, server.dropped = threading.Event(), threading.Event(), threading.Event()
        self.thread = threading.Thread(target=server.serve_forever, daemon=True)
        self.thread.start()
        self.host = f'http://127.0.0.1:{server.server_port}'

    def close(self):
        self.server.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class Keys(CredentialStore):
    """Saved keys without a keyring."""
    def __init__(self, saved=None):
        super().__init__()
        self.saved = saved or {}

    def lookup(self, host_id, reference, cancellable=None):
        if self.has_session(host_id):
            return self._session[host_id]
        if reference in self.saved:
            return self.saved[reference]
        raise CredentialError('An API key is required')


class LoopbackTests(unittest.TestCase):
    def setUp(self):
        self.fixture = Server()
        self.server = self.fixture.server
        self.addCleanup(self.fixture.close)

    def provider(self, **kwargs):
        return OpenAICompatibleProvider(self.fixture.host, **kwargs)

    def chat(self, lines=None, provider=None, **kwargs):
        if lines is not None:
            self.server.lines = lines
        return list((provider or self.provider()).chat('test', USER, **kwargs))

    def test_streams_content_usage_and_ignores_comments(self):
        lines = [b': OPENROUTER PROCESSING', b'event: message', b'id: 1', b'retry: 100'] + ANSWER[:2] + [
            b': keep-alive'] + ANSWER[2:]
        self.assertEqual(self.chat(lines), [
            {'message': {'content': 'Hel', 'thinking': ''}}, {'message': {'content': 'lo', 'thinking': ''}},
            {'message': {'content': '', 'thinking': ''}, 'done': True, 'done_reason': 'stop',
             'prompt_eval_count': 7, 'eval_count': 2, 'prompt_eval_cached_count': 3}])
        path, body, authorization = self.server.received[-1]
        self.assertEqual(path, '/v1/chat/completions')
        self.assertEqual(body, {'model': 'test', 'messages': USER, 'stream': True,
                                'stream_options': {'include_usage': True}})
        self.assertIsNone(authorization)

    def test_stream_without_usage_or_done_marker_still_completes(self):
        chunks = self.chat([b'data:' + ANSWER[1][6:], event({'content': '!'}, 'length')])
        self.assertEqual(''.join(c['message']['content'] for c in chunks), 'Hel!')
        self.assertEqual(chunks[-1], {'message': {'content': '', 'thinking': ''}, 'done': True, 'done_reason': 'length'})

    def test_stream_that_ends_without_a_finish_reason_is_not_complete(self):
        self.assertEqual(self.chat(ANSWER[:3] + [DONE]), [
            {'message': {'content': 'Hel', 'thinking': ''}}, {'message': {'content': 'lo', 'thinking': ''}}])
        partial = self.chat([event({'tool_calls': [{'index': 0, 'id': 'c', 'function': {'name': 'lookup', 'arguments': '{"q'}}]})])
        self.assertEqual(partial, [{'message': {'content': '', 'thinking': '', 'tool_calls': [
            {'id': 'c', 'function': {'name': 'lookup', 'arguments': '{"q'}}]}}])
        self.assertEqual(inspect_calls(partial[0]['message']['tool_calls'], TOOLS, 'failed')['state'], 'incomplete')

    def test_thinking_deltas_under_either_name(self):
        chunks = self.chat([event({'reasoning_content': 'A'}), event({'reasoning': 'B'}),
                            event({'content': 'ok', 'reasoning': None}), event({}, 'stop'), DONE])
        self.assertEqual([(c['message']['thinking'], c['message']['content']) for c in chunks],
                         [('A', ''), ('B', ''), ('', 'ok'), ('', '')])

    def test_tool_call_deltas_are_assembled_into_whole_calls(self):
        def call(index, **fields):
            function = {k: fields.pop(k) for k in ('name', 'arguments') if k in fields}
            return event({'tool_calls': [dict(index=index, function=function, **fields)]})
        chunks = self.chat([
            event({'role': 'assistant', 'content': None}),
            call(0, id='call_a', type='function', name='lookup', arguments=''),
            call(0, arguments='{"q": "ca'), call(1, id='call_b', type='function', name='lookup', arguments='{"q"'),
            call(0, arguments='fé"}'), call(2, id='call_c', name='ping', arguments=''),
            event({}, 'tool_calls'), DONE])
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0]['done_reason'], 'tool_calls')
        calls = chunks[0]['message']['tool_calls']
        self.assertEqual(calls, [
            {'id': 'call_a', 'function': {'name': 'lookup', 'arguments': {'q': 'café'}}},
            {'id': 'call_b', 'function': {'name': 'lookup', 'arguments': '{"q"'}},
            {'id': 'call_c', 'function': {'name': 'ping', 'arguments': {}}}])
        round = inspect_calls(calls, TOOLS, 'complete')
        self.assertEqual([c['status'] for c in round['validation']], ['valid', 'malformed', 'unknown'])
        self.assertEqual(round['state'], 'invalid')

    def test_whole_calls_without_an_index_stay_separate(self):
        chunks = self.chat([event({'tool_calls': [
            {'id': 'a', 'function': {'name': 'lookup', 'arguments': '{"q": "1"}'}},
            {'id': 'b', 'function': {'name': 'lookup', 'arguments': '{"q": "2"}'}}]}), event({}, 'tool_calls')])
        self.assertEqual([c['function']['arguments'] for c in chunks[-1]['message']['tool_calls']],
                         [{'q': '1'}, {'q': '2'}])

    def test_logprobs_are_requested_and_delivered(self):
        tokens = [{'token': 'Hi', 'logprob': -0.1, 'bytes': [72, 105], 'top_logprobs': []}]
        chunks = self.chat([event({'content': 'Hi'}, logprobs=None),
                            b'data: ' + json.dumps({'choices': [{'delta': {'content': ''}, 'logprobs': {'content': tokens}}]}).encode(),
                            event({}, 'stop')], logprobs=True, top_logprobs=3)
        self.assertNotIn('logprobs', chunks[0])
        self.assertEqual(chunks[1]['logprobs'], tokens)
        body = self.server.received[-1][1]
        self.assertEqual((body['logprobs'], body['top_logprobs']), (True, 3))

    def test_mid_stream_error_raises_its_message(self):
        seen = []
        self.server.lines = ANSWER[:2] + [b'data: {"error": {"message": "Provider overloaded", "code": 502}}', DONE]
        with self.assertRaisesRegex(ProviderError, '^Provider overloaded$'):
            for chunk in self.provider().chat('test', USER):
                seen.append(chunk)
        self.assertEqual(seen, [{'message': {'content': 'Hel', 'thinking': ''}}])
        with self.assertRaisesRegex(ProviderError, 'Invalid JSON received from the server'):
            self.chat([b'data: not json'])

    def test_cancellation_stops_the_read_and_closes_the_socket(self):
        self.server.lines, self.server.stall = ANSWER[:2], True
        cancel, seen, errors = Gio.Cancellable(), [], []

        def run():
            try:
                for chunk in self.provider().chat('test', USER, cancellable=cancel):
                    seen.append(chunk)
            except Exception as exc:
                errors.append(exc)
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 3
        while not seen and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(seen, [{'message': {'content': 'Hel', 'thinking': ''}}])
        cancel.cancel()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(errors[0], RequestCancelled)
        self.assertTrue(self.server.dropped.wait(3), 'the server still has a connection')

    def test_abandoning_the_response_closes_the_socket(self):
        self.server.lines, self.server.stall = ANSWER[:2], True
        stream = self.provider().chat('test', USER)
        self.assertEqual(next(stream)['message']['content'], 'Hel')
        stream.close()
        self.assertTrue(self.server.dropped.wait(3))

    def test_error_statuses_are_explained_without_naming_a_vendor(self):
        calls = (lambda: self.chat(), lambda: self.provider().list_models(), lambda: self.provider().probe())
        self.server.body = b'{"error": {"message": "Model not loaded", "type": "invalid_request_error", "code": null}}'
        for status, expected in ((401, 'rejected the API key'), (403, 'rejected the API key'),
                                 (429, 'usage or rate limit'), (400, '^Model not loaded$'), (500, '^Model not loaded$')):
            self.server.status = status
            for call in calls:
                with self.subTest(status=status), self.assertRaisesRegex(ProviderError, expected) as caught:
                    call()
                self.assertEqual(caught.exception.status, status)
                self.assertNotIn('Ollama', str(caught.exception))
        for body, expected in ((b'{"error": "plain text"}', '^plain text$'), (b'<html>', '^HTTP Error 502'),
                               (b'{"error": {"code": 5}}', r'^\{"code": 5\}$')):
            self.server.status, self.server.body = 502, body
            with self.subTest(body=body), self.assertRaisesRegex(ProviderError, expected):
                self.chat()

    def test_key_is_sent_only_when_the_host_has_one_and_never_shown(self):
        keys = Keys({'reference': 'sk-secret'})
        saved = self.provider(host_id='host', credential_id='reference', credentials=keys)
        self.chat(provider=saved)
        saved.list_models()
        self.assertEqual([r[2] for r in self.server.received], ['Bearer sk-secret'] * 2)
        self.assertNotIn('sk-secret', repr(saved))

        keys.set_session('other', 'sk-session')
        self.chat(provider=self.provider(host_id='other', credentials=keys))
        self.assertEqual(self.server.received[-1][2], 'Bearer sk-session')

        for bare in (self.provider(), self.provider(host_id='keyless', credentials=keys)):
            self.chat(provider=bare)
            self.assertIsNone(self.server.received[-1][2])

        # A server that repeats the key in an error does not get it onto the screen.
        self.server.status, self.server.body = 400, b'{"error": {"message": "Bad key sk-secret given"}}'
        with self.assertRaisesRegex(ProviderError, r'^Bad key \[redacted\] given$'):
            self.chat(provider=saved)
        self.server.status = 200
        with self.assertRaisesRegex(ProviderError, r'^Bad key \[redacted\]$'):
            self.chat([b'data: {"error": {"message": "Bad key sk-secret"}}'], provider=saved)

        before = len(self.server.received)
        with self.assertRaisesRegex(ProviderError, 'API key is required'):
            self.chat(provider=self.provider(host_id='host', credential_id='lost', credentials=keys))
        self.assertEqual(len(self.server.received), before)

    def test_key_does_not_follow_a_redirect(self):
        other = Server()
        self.addCleanup(other.close)

        class Redirect(Handler):
            def do_POST(self):
                self.server.received.append((self.path, None, self.headers.get('Authorization')))
                self.send_response(307)
                self.send_header('Location', other.host + self.path)
                self.end_headers()
        self.server.RequestHandlerClass = Redirect
        with self.assertRaisesRegex(ProviderError, 'HTTP Error 307'):
            self.chat(provider=self.provider(host_id='host', credential_id='reference',
                                             credentials=Keys({'reference': 'sk-secret'})))
        self.assertEqual(other.server.received, [])

    def test_models_probe_details_and_base_url_forms(self):
        for suffix in ('', '/', '/v1', '/v1/', '/api', '/api/v1'):
            with self.subTest(suffix=suffix):
                provider = OpenAICompatibleProvider(self.fixture.host + suffix)
                self.assertEqual(provider.list_models(), [{'name': 'test'}, {'name': 'other'}])
                self.assertEqual(self.server.received[-1][0], ('/api' if 'api' in suffix else '') + '/v1/models')
        self.assertEqual(self.provider().probe(), 'Connected\nAvailable models: 2')
        before = len(self.server.received)
        details = self.provider().show_model('test')
        self.assertEqual(details, {'thinking_levels': ['low', 'medium', 'high']})
        self.assertEqual(len(self.server.received), before)
        self.server.models = {'models': []}
        with self.assertRaisesRegex(ProviderError, 'did not return a model list'):
            self.provider().list_models()
        with self.assertRaises(ProviderError):
            OpenAICompatibleProvider('localhost:1234').list_models()

    def test_class_declares_what_the_host_editor_and_panel_read(self):
        cls, caps = OpenAICompatibleProvider, OpenAICompatibleProvider.capabilities
        self.assertEqual((cls.kind, cls.label, cls.default_url, cls.default_name, cls.key_url),
                         ('openai', 'OpenAI-Compatible Server', '', '', ''))
        self.assertEqual((cls.fixed_url, cls.requires_key, cls.key_optional), (None, False, True))
        offered = {name for name, value in vars(caps).items() if value is True}
        self.assertEqual(offered, {'thinking_levels', 'structured_output', 'tools', 'logprobs', 'thinking_text'})
        self.assertIn('not checked', caps.options_notice)
        for name in ('generate', 'embed', 'pull', 'delete', 'running', 'unload'):
            with self.subTest(operation=name), self.assertRaises(NotImplementedError):
                getattr(self.provider(), name)('test')


class TranslationTests(unittest.TestCase):
    def body(self, messages=USER, **kwargs):
        return chat_body('test', messages, **kwargs)

    def test_options(self):
        body = self.body(options={'temperature': 0.5, 'top_p': 0.9, 'seed': 7, 'stop': ['END'], 'num_predict': 64})
        self.assertEqual({k: body[k] for k in body if k not in ('model', 'messages', 'stream', 'stream_options')},
                         {'temperature': 0.5, 'top_p': 0.9, 'seed': 7, 'stop': ['END'], 'max_tokens': 64})
        for unlimited in (-1, -2):
            self.assertNotIn('max_tokens', self.body(options={'num_predict': unlimited}))
        self.assertEqual(self.body(options={'num_predict': 0})['max_tokens'], 0)

    def test_options_the_api_lacks_are_refused_not_dropped(self):
        for options, named in (({'top_k': 5}, 'top_k'), ({'min_p': 0.1}, 'min_p'), ({'num_ctx': 4096}, 'num_ctx'),
                               ({'temperature': 1, 'mirostat': 2, 'top_k': 1}, 'mirostat, top_k')):
            with self.subTest(options=options), self.assertRaisesRegex(ProviderError, f'does not accept: {named}\\.$'):
                self.body(options=options)
        for keep_alive in (0, 300, -1):
            with self.subTest(keep_alive=keep_alive), self.assertRaisesRegex(ProviderError, 'keep_alive'):
                self.body(keep_alive=keep_alive)
        # The refusal happens before anything is sent.
        provider = OpenAICompatibleProvider('http://127.0.0.1:9')
        with self.assertRaisesRegex(ProviderError, 'top_k'):
            list(provider.chat('test', USER, options={'top_k': 5}))

    def test_format(self):
        self.assertNotIn('response_format', self.body())
        self.assertEqual(self.body(format='json')['response_format'], {'type': 'json_object'})
        schema = {'type': 'object', 'properties': {'a': {'type': 'string'}}}
        body = self.body(format=schema)
        self.assertEqual(body['response_format'],
                         {'type': 'json_schema', 'json_schema': {'name': 'response', 'schema': schema}})
        self.assertIsNot(body['response_format']['json_schema']['schema'], schema)
        with self.assertRaisesRegex(ProviderError, 'Invalid output format'):
            self.body(format='yaml')

    def test_thinking(self):
        self.assertNotIn('reasoning_effort', self.body(thinking=None))
        for level in ('low', 'medium', 'high'):
            self.assertEqual(self.body(thinking=level)['reasoning_effort'], level)
        for refused in (True, False, 'max', 'none', 1):
            with self.subTest(thinking=refused), self.assertRaisesRegex(ProviderError, 'Low, Medium, and High'):
                self.body(thinking=refused)

    def test_logprobs_and_tools(self):
        body = self.body(top_logprobs=5)
        self.assertFalse({'logprobs', 'top_logprobs', 'tools'} & set(body))
        body = self.body(logprobs=True, top_logprobs=5, tools=TOOLS)
        self.assertEqual((body['logprobs'], body['top_logprobs'], body['tools']), (True, 5, TOOLS))
        self.assertIsNot(body['tools'], TOOLS)
        with self.assertRaisesRegex(ProviderError, 'between 0 and 20'):
            self.body(logprobs=True, top_logprobs=21)

    def test_images_become_data_urls_of_the_right_type(self):
        history = [{'role': 'system', 'content': 'Be brief.'},
                   {'role': 'user', 'content': 'look', 'images': [PNG, JPEG, GIF, WEBP, 'AAAA']},
                   {'role': 'assistant', 'content': 'seen', 'thinking': 'hidden'},
                   {'role': 'user', 'content': '', 'images': [JPEG]},
                   {'role': 'user', 'content': 'and this'}]
        before = json.dumps(history)
        sent = wire_messages(history, images=[PNG])
        self.assertEqual(json.dumps(history), before)
        self.assertEqual(sent[0], {'role': 'system', 'content': 'Be brief.'})
        self.assertEqual(sent[1]['content'][0], {'type': 'text', 'text': 'look'})
        self.assertEqual([part['image_url']['url'].split(',')[0] for part in sent[1]['content'][1:]],
                         ['data:image/' + kind + ';base64' for kind in ('png', 'jpeg', 'gif', 'webp', 'png')])
        self.assertEqual(sent[1]['content'][2], {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,' + JPEG}})
        self.assertEqual(sent[2], {'role': 'assistant', 'content': 'seen'})
        self.assertEqual(sent[3]['content'], [{'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,' + JPEG}}])
        # The images argument belongs to the last user message, as it does for Ollama.
        self.assertEqual(sent[4]['content'], [{'type': 'text', 'text': 'and this'},
                                              {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + PNG}}])
        self.assertEqual(wire_messages(history[:3], images=[PNG])[2], {'role': 'assistant', 'content': 'seen'})

    def test_tool_history_with_ids(self):
        history = [{'role': 'user', 'content': 'find'},
                   {'role': 'assistant', 'content': '', 'thinking': 'hm', 'tool_calls': [
                       {'id': 'call_a', 'function': {'name': 'lookup', 'arguments': {'q': 'café'}}},
                       {'id': 'call_b', 'function': {'index': 1, 'name': 'ping', 'arguments': {}}}]},
                   {'role': 'tool', 'content': 'pong', 'tool_name': 'ping', 'tool_call_id': 'call_b'},
                   {'role': 'tool', 'content': 'found', 'tool_name': 'lookup', 'tool_call_id': 'call_a'}]
        sent = wire_messages(history)
        self.assertEqual(sent[1], {'role': 'assistant', 'content': '', 'tool_calls': [
            {'id': 'call_a', 'type': 'function', 'function': {'name': 'lookup', 'arguments': '{"q": "caf\\u00e9"}'}},
            {'id': 'call_b', 'type': 'function', 'function': {'name': 'ping', 'arguments': '{}'}}]})
        self.assertEqual(sent[2:], [{'role': 'tool', 'content': 'pong', 'tool_call_id': 'call_b'},
                                    {'role': 'tool', 'content': 'found', 'tool_call_id': 'call_a'}])

    def test_tool_rounds_stored_without_ids_get_matching_stable_ones(self):
        def round(*names):
            message = {'role': 'assistant', 'content': '', 'tool_calls': [
                {'function': {'name': name, 'arguments': {'q': name}}} for name in names]}
            message['response_metadata'] = {'status': 'complete', 'tool_round': dict(
                inspect_calls(message['tool_calls'], TOOLS, 'complete'), results=['result of ' + n for n in names])}
            return [message] + result_messages(message)
        # An Ollama conversation: three rounds, the second with an id of its own.
        history = [{'role': 'user', 'content': 'go', 'images': [PNG]}] + round('lookup', 'ping', 'lookup')
        second = round('ping')
        second[0]['tool_calls'][0]['id'] = second[1]['tool_call_id'] = 'call_x'
        history += second + round('lookup') + [{'role': 'user', 'content': 'thanks'}]
        sent = wire_messages(api_messages(history))
        self.assertEqual(sent, wire_messages(api_messages(history)))
        ids = [[call['id'] for call in m.get('tool_calls', [])] or m.get('tool_call_id') for m in sent]
        self.assertEqual(ids, [None, ['call00001', 'call00002', 'call00003'], 'call00001', 'call00002', 'call00003',
                               ['call_x'], 'call_x', ['call00004'], 'call00004', None])
        self.assertEqual([m['content'] for m in sent[2:5]], ['result of lookup', 'result of ping', 'result of lookup'])
        self.assertTrue(all(set(m) <= {'role', 'content', 'tool_calls', 'tool_call_id'} for m in sent))
        # Earlier turns keep their ids when the conversation grows.
        self.assertEqual(wire_messages(api_messages(history[:4]))[1]['tool_calls'], sent[1]['tool_calls'])
        # Results stored out of order still find their own call by name.
        swapped = wire_messages([history[1], history[3], history[2], history[4]])
        self.assertEqual([m['tool_call_id'] for m in swapped[1:]], ['call00002', 'call00001', 'call00003'])


@unittest.skipUnless(Gdk.Display.get_default(), 'GTK smoke tests require a display')
class HostTests(unittest.TestCase):
    def setUp(self):
        self.fixture = Server()
        self.server = self.fixture.server
        self.temp = tempfile.TemporaryDirectory()
        self.context = AppContext(self.temp.name)
        self.storage = self.context.storage
        self.host = self.storage.save_host('Loopback', self.fixture.host, True, provider='openai')
        self.tabs = []

    def tearDown(self):
        for tab in self.tabs:
            tab.draft.close(discard=True)
            tab.chat_input.cancel_fetches()
            if tab.request:
                tab.request.cancellable.cancel()
        pump_until(lambda: self.context.services.idle and all(t.request is None for t in self.tabs))
        pump_until(lambda: self.storage.writer.idle)
        self.context.shutdown()
        self.fixture.close()
        self.temp.cleanup()

    def make_tab(self):
        tab = GenerationTab(mode='chat', chat_id=self.storage.create_chat()['id'], context=self.context)
        self.tabs.append(tab)
        wait_for_model(tab)
        return tab

    def send(self, tab, text):
        tab.chat_input.entry.set_text(text)
        tab.on_send_clicked()
        pump_until(lambda: tab.request is None and self.storage.writer.idle)
        return self.storage.get_chat(tab.strategy.chat_id)['messages']

    def test_chat_tab_sends_through_chat_completions(self):
        tab = self.make_tab()
        composer = tab.chat_input
        self.assertEqual(self.server.received[0][0], '/v1/models')
        self.assertEqual(composer._thinking_values, [None, 'low', 'medium', 'high'])
        # Nothing is known about the model, so nothing is said and nothing is blocked.
        self.assertFalse(composer.capability_notice.get_visible() or composer.model_summary_cell.get_visible())
        self.assertIsNone(composer.tool_support)
        self.assertTrue(composer.attach_button.get_sensitive())
        composer.thinking_dropdown.set_selected(3)
        self.server.lines = [event({'reasoning_content': 'Thinking. '})] + ANSWER
        history = self.send(tab, 'first')
        self.assertEqual([m['content'] for m in history], ['first', 'Hello'])
        answer = history[-1]
        self.assertEqual(answer['thinking_content'], 'Thinking. ')
        self.assertEqual(answer['response_metadata']['status'], 'complete')
        self.assertEqual(answer['response_metadata']['metrics'], {
            'prompt_eval_count': 7, 'eval_count': 2, 'prompt_eval_cached_count': 3, 'done_reason': 'stop'})
        path, body, authorization = self.server.received[-1]
        self.assertEqual(path, '/v1/chat/completions')
        self.assertEqual(body['messages'], [{'role': 'user', 'content': 'first'}])
        self.assertEqual(body['reasoning_effort'], 'high')
        self.assertIsNone(authorization)
        self.assertFalse({'options', 'think', 'keep_alive', 'format'} & set(body))
        # The header names the host, and the statistics hold only what was reported or measured here.
        bubble = tab.message_list.list_box.get_last_child()
        self.assertEqual(bubble.header.get_label(), 'Loopback ({0})'.format(composer.get_selected_model()))
        self.assertEqual((answer['api_details']['host_id'], answer['api_details']['host_name']),
                         (self.host['id'], 'Loopback'))
        self.assertIn('first_token_seconds', answer['response_metadata'])
        self.assertRegex(bubble._stats_label.get_text(),
                         r'^Total \(measured locally\): \d+\.\d\ds \| Prompt: 7 tokens \| Cached: 3 tokens \| '
                         r'Generated: 2 tokens \| Tokens/s \(measured locally\): \d+\.\d\d \| Finish: stop$')

        self.server.lines = ANSWER
        self.send(tab, 'second')
        self.assertEqual(self.server.received[-1][1]['messages'], [
            {'role': 'user', 'content': 'first'}, {'role': 'assistant', 'content': 'Hello'},
            {'role': 'user', 'content': 'second'}])

    def test_early_end_and_server_error_are_saved_as_failed(self):
        tab = self.make_tab()
        self.server.lines = ANSWER[:3]
        failed = self.send(tab, 'first')[-1]
        self.assertEqual((failed['content'], failed['response_metadata']['status']), ('Hello', 'failed'))
        self.assertIn('ended before completion', failed['response_metadata']['error'])
        self.server.status, self.server.body = 429, b'{"error": {"message": "slow down"}}'
        failed = self.send(tab, 'second')[-1]
        self.assertIn('usage or rate limit', failed['response_metadata']['error'])

    def test_thinking_levels_come_from_the_model_details(self):
        widget = ChatInput()
        widget.services = self.context.services
        widget.load_thinking_val('max')
        widget._set_thinking_options({'thinking_levels': ['low', 'medium', 'high']})
        self.assertEqual(widget._thinking_values, [None, 'low', 'medium', 'high'])
        self.assertIsNone(widget.get_thinking_value())
        self.assertTrue(widget.thinking_dropdown.get_sensitive())
        widget.load_thinking_val('medium')
        # The listed levels decide, whatever the family or capability list says.
        widget._set_thinking_options({'thinking_levels': [False, 'high', 'max'], 'capabilities': ['completion'],
                                      'details': {'family': 'gpt-oss'}})
        self.assertEqual(widget._thinking_values, [None, False, 'high', 'max'])
        widget._set_thinking_options({'thinking_levels': []})
        self.assertEqual(widget._thinking_values, [None])
        self.assertFalse(widget.thinking_dropdown.get_sensitive())
        widget._set_thinking_options({'capabilities': ['thinking']})
        self.assertEqual(widget._thinking_values, [None, False, True, 'low', 'medium', 'high', 'max'])
        # A level the model lacked is not brought back, as with a family's levels.
        self.assertIsNone(widget.get_thinking_value())

    def test_empty_model_list_is_explained_for_each_kind_of_host(self):
        widget = ChatInput()
        for host, expected in ((None, 'Pull one in Manage Models'), (OllamaProvider('http://localhost:11434'), 'Pull one'),
                               (OllamaCloudProvider(CLOUD_URL), 'No cloud models'),
                               (OpenAICompatibleProvider(self.fixture.host), 'No models found on this server')):
            widget._host = host
            self.assertIn(expected, widget._empty_model_notice())

    def test_shared_and_participant_thinking_must_be_a_level_the_target_takes(self):
        local = next(h for h in self.storage.get_all_hosts() if h['id'] != self.host['id'])

        def models(connection, cancel=None):
            details = ({'thinking_levels': ['low', 'medium', 'high']} if connection.kind == 'openai'
                       else {'capabilities': ['completion', 'thinking']})
            return [('one', details), ('two', details)]
        targets = [dict(host_id=local['id'], model='one'), dict(host_id=self.host['id'], model='two')]
        with patch.object(self.context.services.catalog, 'models', side_effect=models):
            def prepare(**wire):
                return prepare_run(self.context, ComparisonRun(), 'question', [],
                                   RequestSettings(wire=WireOptions(**wire)), targets, None, 0)

            def validate(**wire):
                people = [RequestSettings.from_stored(dict(targets[0])),
                          RequestSettings.from_stored(dict(targets[1], **wire))]
                return validate_participants(self.context, people, Gio.Cancellable())
            for check in (prepare, validate):
                for thinking in (True, False, 'max'):
                    with self.subTest(check=check.__name__, thinking=thinking), \
                            self.assertRaisesRegex(ValueError, 'thinking level is incompatible'):
                        check(thinking=thinking)
                check(thinking='high')
                check()
            with self.assertRaisesRegex(ValueError, 'Loopback does not support: Top K'):
                prepare(options={'top_k': 5})
            self.assertEqual([c.kind for c in validate(thinking='low')[1]], ['ollama', 'openai'])


if __name__ == '__main__':
    unittest.main()
