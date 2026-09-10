from contextlib import ExitStack
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from gi.repository import Gdk, GLib, Gtk
from src import ollama, session
from src.bubbles import AiBubble, format_statistics
from src.storage import ChatStorage
from src.tab import GenerationTab
from src.widgets.chat_input import ChatInput
from src.window import GnollamaWindow
from test_transport import Server


def pump_until(predicate, timeout=4):
    deadline = time.monotonic() + timeout
    context = GLib.MainContext.default()
    while time.monotonic() < deadline:
        while context.pending():
            context.iteration(False)
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError('Timed out waiting for GTK/background work')


@unittest.skipUnless(Gdk.Display.get_default(), 'GTK smoke tests require a display')
class UITests(unittest.TestCase):
    def setUp(self):
        ollama.resume()
        self.temp = tempfile.TemporaryDirectory()
        self.storage = ChatStorage(self.temp.name)
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(ollama, 'fetch_models', return_value=['test']))
        self.stack.enter_context(patch.object(ollama, 'show_model', return_value={'capabilities': ['thinking']}))
        self.tabs = []
        self.windows = []

    def tearDown(self):
        for tab in self.tabs:
            tab.chat_input.cancel_fetches()
            if tab.request:
                tab.request.cancellable.cancel()
        pump_until(lambda: session.worker.idle and all(t.request is None for t in self.tabs))
        pump_until(lambda: self.storage.writer.idle)
        if not self.storage.writer._closed:
            self.storage.writer.shutdown()
        for window in self.windows:
            window.destroy()
        self.stack.close()
        self.temp.cleanup()
        ollama.resume()

    def make_tab(self):
        chat = self.storage.create_chat()
        tab = GenerationTab(mode='chat', chat_id=chat['id'], storage=self.storage)
        self.tabs.append(tab)
        pump_until(lambda: tab.chat_input.get_selected_model() == 'test' and session.worker.idle)
        return tab

    def test_thinking_controls_and_saved_false(self):
        widget = ChatInput()
        widget.load_thinking_val(False)
        self.assertIs(widget.get_thinking_value(), False)
        widget._set_thinking_options({'details': {'family': 'gptoss'}, 'capabilities': ['thinking']})
        self.assertEqual(widget._thinking_values, [None, 'low', 'medium', 'high'])
        widget._set_thinking_options({'capabilities': ['completion']})
        self.assertEqual(widget._thinking_values, [None])
        widget._set_thinking_options(None)
        self.assertIn(False, widget._thinking_values)
        self.assertIsNone(widget.get_thinking_value())

    def test_statistics_distinguish_missing_zero_and_speed(self):
        absent = format_statistics({'eval_duration': 0})
        self.assertIn('Cached: Unavailable', absent)
        self.assertIn('Tokens/s: Unavailable', absent)
        present = format_statistics({'prompt_eval_cached_count': 0, 'eval_count': 20,
                                     'eval_duration': 2_000_000_000, 'done_reason': 'stop'})
        self.assertIn('Cached: 0', present)
        self.assertIn('Tokens/s: 10.00', present)
        self.assertIn('Finish: stop', present)

    def test_duplicate_send_stop_and_restore_partial(self):
        fixture = Server('stall')
        self.addCleanup(fixture.close)
        host = self.storage.get_all_hosts()[0]
        self.storage.update_host(host['id'], host['name'], fixture.host, True)
        tab = self.make_tab()
        tab.chat_input.entry.set_text('first')
        tab.on_send_clicked()
        state = tab.request
        tab.chat_input.entry.set_text('draft')
        tab.on_send_clicked()
        self.assertIs(tab.request, state)
        self.assertEqual(tab.chat_input.entry.get_text(), 'draft')
        pump_until(lambda: state.content == 'partial')
        tab.on_send_or_stop()
        pump_until(lambda: tab.request is None and self.storage.writer.idle)
        history = self.storage.get_chat(tab.strategy.chat_id)['messages']
        self.assertEqual(len(history), 2)
        self.assertEqual(history[-1]['content'], 'partial')
        self.assertEqual(history[-1]['response_metadata']['status'], 'stopped')
        restored = GenerationTab(mode='chat', chat_id=tab.strategy.chat_id,
                                 initial_history=history, storage=self.storage)
        self.tabs.append(restored)
        self.assertEqual(restored.strategy.history, history)

    def test_independent_tabs_complete_without_mixing(self):
        first, second = self.make_tab(), self.make_tab()
        def chat(**kwargs):
            yield {'message': {'content': kwargs['messages'][-1]['content']}, 'done': True,
                   'eval_count': 2, 'eval_duration': 1_000_000_000}
        with patch.object(ollama, 'chat', side_effect=chat):
            for tab, prompt in ((first, 'one'), (second, 'two')):
                tab.chat_input.entry.set_text(prompt)
                tab.on_send_clicked()
            pump_until(lambda: first.request is None and second.request is None and self.storage.writer.idle)
        for tab, expected in ((first, 'one'), (second, 'two')):
            saved = self.storage.get_chat(tab.strategy.chat_id)['messages']
            self.assertEqual(saved[-1]['content'], expected)
            self.assertEqual(saved[-1]['response_metadata']['metrics']['eval_count'], 2)

    def test_premature_end_is_saved_as_failed(self):
        tab = self.make_tab()
        with patch.object(ollama, 'chat', return_value=iter([{'message': {'content': 'partial'}}])):
            tab.chat_input.entry.set_text('test')
            tab.on_send_clicked()
            pump_until(lambda: tab.request is None and self.storage.writer.idle)
        message = self.storage.get_chat(tab.strategy.chat_id)['messages'][-1]
        self.assertEqual(message['content'], 'partial')
        self.assertEqual(message['response_metadata']['status'], 'failed')

    def test_invalid_options_and_attachments_preserve_prompt(self):
        tab = self.make_tab()
        tab.chat_input.entry.set_text('keep me')
        tab.options_panel.temperature_entry.set_text('nan')
        tab.on_send_clicked()
        self.assertIsNone(tab.request)
        self.assertEqual(tab.chat_input.entry.get_text(), 'keep me')
        tab.options_panel.temperature_entry.set_text('')
        tab.chat_input.selected_image_paths = ['/nonexistent/image.png']
        tab.on_send_clicked()
        self.assertIsNone(tab.request)
        self.assertEqual(tab.chat_input.entry.get_text(), 'keep me')

    def test_host_refresh_preserves_selected_host(self):
        tab = self.make_tab()
        selected = tab.options_panel.get_selected_host()
        self.storage.update_host(selected['id'], 'Renamed', 'http://127.0.0.1:1234', True)
        tab.update_hosts()
        self.assertEqual(tab.options_panel.get_selected_host()['name'], 'Renamed')
        self.assertEqual(tab.chat_input._host, 'http://127.0.0.1:1234')

    def test_close_tab_waits_for_stopped_answer_save(self):
        fixture = Server('stall')
        self.addCleanup(fixture.close)
        host = self.storage.get_all_hosts()[0]
        self.storage.update_host(host['id'], host['name'], fixture.host, True)
        tab = self.make_tab()
        tab.chat_input.entry.set_text('test')
        tab.on_send_clicked()
        pump_until(lambda: tab.request.content == 'partial')
        closed = []
        tab.close_session(lambda: closed.append(True))
        pump_until(lambda: bool(closed))
        self.assertEqual(self.storage.get_chat(tab.strategy.chat_id)['messages'][-1]['response_metadata']['status'], 'stopped')

    def make_window(self):
        with patch('src.window.ChatStorage', return_value=self.storage):
            window = GnollamaWindow()
        self.windows.append(window)
        tab = window.notebook.get_nth_page(0)
        self.tabs.append(tab)
        pump_until(lambda: tab.chat_input.get_selected_model() == 'test' and session.worker.idle)
        return window, tab

    def test_quit_cancels_stalled_request_and_drains_pending_save(self):
        fixture = Server('stall')
        self.addCleanup(fixture.close)
        host = self.storage.get_all_hosts()[0]
        self.storage.update_host(host['id'], host['name'], fixture.host, True)
        window, tab = self.make_window()
        tab.chat_input.entry.set_text('persist before quit')
        tab.on_send_clicked()
        pump_until(lambda: tab.request.content == 'partial')
        release = threading.Event()
        self.storage._submit(lambda: release.wait(3))
        with patch.object(session.worker, 'shutdown') as shutdown:
            self.assertTrue(window.on_close_request())
            pump_until(lambda: tab.request is None)
            self.assertFalse(window._allow_close)
            release.set()
            pump_until(lambda: window._allow_close)
            shutdown.assert_called_once_with(wait=False)
        saved = self.storage.get_chat(tab.strategy.chat_id)
        self.assertEqual(saved['messages'][-1]['content'], 'partial')
        self.assertEqual(saved['messages'][-1]['response_metadata']['status'], 'stopped')

    def test_save_failure_keeps_window_open_until_retry(self):
        window, tab = self.make_window()
        blocked = True
        def save():
            if blocked:
                raise OSError('disk full')
        future = self.storage._submit(save)
        with self.assertRaises(OSError):
            future.result(2)
        window.request_shutdown()
        pump_until(lambda: window._save_error_dialog is not None)
        self.assertFalse(window._allow_close)
        blocked = False
        with patch.object(session.worker, 'shutdown'):
            window._save_error_dialog.emit('response', 'retry')
            pump_until(lambda: window._allow_close)

    def test_quit_action_uses_window_shutdown_coordinator(self):
        from src.main import GnollamaApplication
        window, tab = self.make_window()
        app = GnollamaApplication('0.10.0')
        with patch.object(app, 'get_windows', return_value=[window]), patch.object(window, 'request_shutdown') as shutdown:
            app.lookup_action('quit').activate(None)
            shutdown.assert_called_once()
        self.assertEqual(app.version, '0.10.0')

    def test_stale_model_and_capability_results_are_ignored(self):
        widget = ChatInput()
        gate = threading.Event()
        started = threading.Event()
        def models(host, **kwargs):
            if host == 'http://old':
                started.set()
                gate.wait(2)
                return ['old-model']
            return ['new-model']
        with patch.object(ollama, 'fetch_models', side_effect=models):
            widget.fetch_models('http://old')
            self.assertTrue(started.wait(1))
            widget.fetch_models('http://new')
            pump_until(lambda: widget.get_selected_model() == 'new-model')
            gate.set()
            pump_until(lambda: session.worker.idle)
            self.assertEqual(widget.get_selected_model(), 'new-model')
        gate.clear()
        started.clear()
        def details(host, model, **kwargs):
            if model == 'old-model':
                started.set()
                gate.wait(2)
                return {'capabilities': ['completion']}
            return {'capabilities': ['thinking']}
        with patch.object(ollama, 'show_model', side_effect=details):
            widget.set_models(['old-model', 'new-model'])
            self.assertTrue(started.wait(1))
            widget.select_model('new-model')
            pump_until(lambda: 'max' in widget._thinking_values)
            gate.set()
            pump_until(lambda: session.worker.idle)
            self.assertIn('max', widget._thinking_values)
        widget.cancel_fetches()

    def test_generate_mode_and_model_pull_dialog_cancellation(self):
        fixture = Server('stream')
        self.addCleanup(fixture.close)
        host = self.storage.get_all_hosts()[0]
        self.storage.update_host(host['id'], host['name'], fixture.host, True)
        tab = GenerationTab(mode='generate', storage=self.storage)
        self.tabs.append(tab)
        pump_until(lambda: tab.chat_input.get_selected_model() == 'test')
        tab.chat_input.entry.set_text('single turn')
        tab.on_send_clicked()
        pump_until(lambda: tab.request is None)
        self.assertEqual(fixture.server.received[-1][0], '/api/generate')
        self.assertEqual(self.storage.get_all_chats(), [])
        from src.model_manager import PullModelDialog
        parent = Gtk.Window()
        self.windows.append(parent)
        dialog = PullModelDialog(parent, fixture.host)
        self.windows.append(dialog)
        fixture.server.mode = 'headers'
        fixture.server.started.clear()
        dialog.model_name_entry.set_text('test')
        dialog.on_pull_clicked(None)
        self.assertTrue(fixture.server.started.wait(1))
        dialog.on_cancel_clicked(None)
        pump_until(lambda: dialog.pull_future.done())
        self.assertTrue(dialog._pull_cancel.is_cancelled())
        self.assertTrue(dialog.requests.closed)
