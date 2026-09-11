import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from gi.repository import Gio
from src import ollama


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.do_POST()

    def do_DELETE(self):
        self.do_POST()

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        self.server.received.append((self.path, json.loads(raw) if raw else {}))
        self.server.headers_received.append(dict(self.headers))
        self.server.started.set()
        if self.server.mode == 'headers':
            self.server.release.wait(5)
            return
        self.send_response(self.server.status or (400 if self.server.mode == 'error' else 200))
        if self.server.redirect_location:
            self.send_header('Location', self.server.redirect_location)
        self.end_headers()
        try:
            if self.server.mode == 'error':
                self.wfile.write(b'{"error":"pull this model first"}')
            elif self.server.mode == 'json':
                self.wfile.write(json.dumps(self.server.payload).encode())
            elif self.server.mode == 'malformed':
                self.wfile.write(b'not json\n')
            elif self.server.mode == 'stall':
                self.wfile.write(b'{"response":"partial"}\n')
                self.wfile.flush()
                self.server.release.wait(5)
            elif self.server.mode == 'pull':
                self.wfile.write(b'{"status":"success"}\n')
            elif self.server.mode == 'details':
                self.wfile.write(b'{"capabilities":["thinking"]}')
            elif self.server.mode == 'ps':
                self.wfile.write(b'{"models":[{"name":"test","size_vram":0,"context_length":4096}]}')
            elif self.server.mode == 'unload':
                self.wfile.write(b'{"done":true,"done_reason":"unload"}')
            elif self.server.mode == 'empty':
                pass
            else:
                payload = '{"response":"café"}\n\n{"done":true,"prompt_eval_cached_count":0}'
                for byte in payload.encode('utf-8'):
                    self.wfile.write(bytes([byte]))
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


class Server:
    def __init__(self, mode='stream'):
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.mode = mode
        self.server.received = []
        self.server.headers_received = []
        self.server.status = None
        self.server.redirect_location = None
        self.server.started = threading.Event()
        self.server.release = threading.Event()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host = f'http://127.0.0.1:{self.server.server_port}'

    def close(self):
        self.server.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class TransportTests(unittest.TestCase):
    def setUp(self):
        ollama.resume()
        self.fixture = Server()
        self.addCleanup(self.fixture.close)

    def test_fragmented_unicode_and_final_line_without_newline(self):
        chunks = list(ollama.generate(self.fixture.host, 'test', 'hello'))
        self.assertEqual(chunks, [{'response': 'café'}, {'done': True, 'prompt_eval_cached_count': 0}])

    def test_errors_keep_server_detail(self):
        self.fixture.server.mode = 'error'
        calls = [lambda: ollama.show_model(self.fixture.host, 'test'),
                 lambda: ollama.get_version(self.fixture.host),
                 lambda: ollama.delete_model(self.fixture.host, 'test'),
                 lambda: list(ollama.generate(self.fixture.host, 'test', 'hello'))]
        for call in calls:
            with self.assertRaisesRegex(ollama.OllamaError, 'pull this model first'):
                call()

    def test_show_uses_model_and_chat_does_not_mutate_history(self):
        self.fixture.server.mode = 'details'
        ollama.show_model(self.fixture.host, 'test')
        self.assertEqual(self.fixture.server.received[-1][1], {'model': 'test', 'verbose': False})
        self.fixture.server.mode = 'stream'
        history = [{'role': 'user', 'content': 'hello'}]
        list(ollama.chat(self.fixture.host, 'test', history, images=['aGVsbG8=']))
        self.assertNotIn('images', history[0])

    def test_thinking_serialization(self):
        for value in (None, False, True, 'low', 'medium', 'high', 'max'):
            list(ollama.generate(self.fixture.host, 'test', 'hello', thinking=value))
            body = self.fixture.server.received[-1][1]
            if value is None:
                self.assertNotIn('think', body)
            else:
                self.assertEqual(body['think'], value)

    def test_structured_formats_and_keep_alive_on_both_endpoints(self):
        for endpoint in ('generate', 'chat'):
            for output_format in (None, 'json', {}, {'type': 'object'}):
                for keep_alive in (None, 0, 300, 1800, -1, 23):
                    args = dict(host=self.fixture.host, model='test', format=output_format, keep_alive=keep_alive)
                    if endpoint == 'generate':
                        list(ollama.generate(prompt='test', **args))
                    else:
                        list(ollama.chat(messages=[{'role': 'user', 'content': 'test'}], **args))
                    path, body = self.fixture.server.received[-1]
                    self.assertEqual(path, '/api/' + endpoint)
                    for key, value in (('format', output_format), ('keep_alive', keep_alive)):
                        if value is None:
                            self.assertNotIn(key, body)
                        else:
                            self.assertEqual(body[key], value)
                        self.assertNotIn(key, body.get('options', {}))

    def test_running_models_and_unload(self):
        self.fixture.server.mode = 'ps'
        models = ollama.fetch_running_models(self.fixture.host)
        self.assertEqual(models[0]['size_vram'], 0)
        self.assertEqual(self.fixture.server.received[-1][0], '/api/ps')
        self.fixture.server.mode = 'unload'
        self.assertEqual(ollama.unload_model(self.fixture.host, 'test')['done_reason'], 'unload')
        self.assertEqual(self.fixture.server.received[-1],
                         ('/api/generate', {'model': 'test', 'stream': False, 'keep_alive': 0}))
        self.fixture.server.mode = 'error'
        for call in (ollama.fetch_running_models, lambda host: ollama.unload_model(host, 'test')):
            with self.assertRaisesRegex(ollama.OllamaError, 'pull this model first'):
                call(self.fixture.host)
        cancel = Gio.Cancellable()
        cancel.cancel()
        with self.assertRaises(ollama.RequestCancelled):
            ollama.unload_model(self.fixture.host, 'test', cancellable=cancel)

    def test_malformed_stream_fails(self):
        self.fixture.server.mode = 'malformed'
        with self.assertRaisesRegex(ollama.OllamaError, 'Invalid JSON'):
            list(ollama.generate(self.fixture.host, 'test', 'hello'))

    def test_pull_requires_success(self):
        self.fixture.server.mode = 'pull'
        self.assertEqual(list(ollama.pull(self.fixture.host, 'test')), [{'status': 'success'}])
        self.fixture.server.mode = 'empty'
        with self.assertRaisesRegex(ollama.OllamaError, 'before completion'):
            list(ollama.pull(self.fixture.host, 'test'))

    def test_cancellation_interrupts_connect_and_stream_reads(self):
        for mode in ('headers', 'stall'):
            with self.subTest(mode=mode):
                self.fixture.server.mode = mode
                self.fixture.server.started.clear()
                cancel = Gio.Cancellable()
                errors = []
                def run():
                    try:
                        list(ollama.generate(self.fixture.host, 'test', 'hello', cancellable=cancel))
                    except Exception as exc:
                        errors.append(exc)
                thread = threading.Thread(target=run, daemon=True)
                thread.start()
                self.assertTrue(self.fixture.server.started.wait(2))
                time.sleep(0.05)
                cancel.cancel()
                thread.join(2)
                self.assertFalse(thread.is_alive())
                self.assertIsInstance(errors[0], ollama.RequestCancelled)

    def test_timeout_and_cancelled_queued_request(self):
        self.fixture.server.mode = 'headers'
        with self.assertRaises(ollama.OllamaError):
            list(ollama.generate(self.fixture.host, 'test', 'hello', timeout=1))
        cancel = Gio.Cancellable()
        cancel.cancel()
        before = len(self.fixture.server.received)
        with self.assertRaises(ollama.RequestCancelled):
            list(ollama.generate(self.fixture.host, 'test', 'hello', cancellable=cancel))
        self.assertEqual(len(self.fixture.server.received), before)
