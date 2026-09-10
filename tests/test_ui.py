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
        tab = window.tab_view.get_nth_page(0).get_child()
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

    def test_chat_model_picker_filters_by_capabilities_and_reuses_details(self):
        widget = ChatInput()
        self.addCleanup(widget.cancel_fetches)
        responses = {
            'qwen3-embedding:0.6b': {'capabilities': ['embedding']},
            'custom-search-model': {'capabilities': ['embedding', 'vision']},
            'chat-embedding-helper': {'capabilities': ['completion', 'vision', 'tools']},
            'dual-purpose': {'capabilities': ['completion', 'embedding']},
            'legacy': {},
            'unavailable-details': None,
        }
        def details(host, model, **kwargs):
            if responses[model] is None:
                raise ollama.OllamaError('show is unavailable')
            return responses[model]
        with patch.object(ollama, 'fetch_models', return_value=list(responses)), \
                patch.object(ollama, 'show_model', side_effect=details) as show:
            widget.fetch_models('http://models')
            pump_until(lambda: widget.get_selected_model() is not None and session.worker.idle)
            model = widget.model_dropdown.get_model()
            self.assertEqual([model.get_string(i) for i in range(model.get_n_items())],
                             list(responses)[2:])
            self.assertIs(widget.image_support, True)
            self.assertIs(widget.tool_support, True)
            widget.select_model('dual-purpose')
            self.assertIs(widget.image_support, False)
            widget.select_model('unavailable-details')
            self.assertIsNone(widget.image_support)
            self.assertTrue(widget.send_button.get_sensitive())
            self.assertEqual(show.call_count, len(responses))

    def test_chat_model_refresh_preserves_selection_and_handles_embedding_only_host(self):
        widget = ChatInput()
        self.addCleanup(widget.cancel_fetches)
        responses = {'vectors': {'capabilities': ['embedding']},
                     'chat-one': {'capabilities': ['completion']},
                     'chat-two': {'capabilities': ['completion']}}
        with patch.object(ollama, 'fetch_models', side_effect=lambda *a, **kw: list(responses)), \
                patch.object(ollama, 'show_model', side_effect=lambda h, m, **kw: responses[m]):
            widget.pending_model_selection = 'chat-two'
            widget.fetch_models('http://models')
            pump_until(lambda: widget.get_selected_model() == 'chat-two' and session.worker.idle)
            widget.fetch_models('http://models')
            pump_until(lambda: widget.get_selected_model() == 'chat-two' and session.worker.idle)
            # A saved embedding selection must not silently switch to another model.
            widget.pending_model_selection = 'vectors'
            widget.fetch_models('http://models')
            pump_until(lambda: session.worker.idle)
            self.assertIsNone(widget.get_selected_model())
            self.assertFalse(widget.send_button.get_sensitive())
            widget.select_model('chat-one')
            # Refresh must invalidate cached capabilities even on the same host.
            responses['chat-one'] = {'capabilities': ['embedding']}
            del responses['chat-two']
            widget.fetch_models('http://models')
            # Worker completion can precede delivery of its GTK callback.
            pump_until(lambda: session.worker.idle and 'No chat models found' in widget.capability_notice.get_text())
            self.assertEqual(widget.model_dropdown.get_model().get_n_items(), 0)
            self.assertIsNone(widget.get_selected_model())
            self.assertFalse(widget.send_button.get_sensitive())
            self.assertFalse(widget.attach_button.get_sensitive())
            self.assertIn('No chat models found', widget.capability_notice.get_text())

    def test_chat_model_filter_ignores_cancelled_host_capabilities(self):
        widget = ChatInput()
        self.addCleanup(widget.cancel_fetches)
        started, release = threading.Event(), threading.Event()
        def details(host, model, **kwargs):
            if host == 'http://old':
                started.set()
                release.wait(2)
                return {'capabilities': ['embedding']}
            return {'capabilities': ['completion', 'vision']}
        with patch.object(ollama, 'fetch_models', return_value=['same-name', 'next-model']), \
                patch.object(ollama, 'show_model', side_effect=details) as show:
            try:
                widget.fetch_models('http://old')
                self.assertTrue(started.wait(1))
                widget.fetch_models('http://new')
                pump_until(lambda: widget.get_selected_model() == 'same-name')
            finally:
                release.set()
            pump_until(lambda: session.worker.idle)
            self.assertEqual(widget.get_selected_model(), 'same-name')
            self.assertIs(widget.image_support, True)
            self.assertEqual([c.args for c in show.call_args_list],
                             [('http://old', 'same-name'), ('http://new', 'same-name'),
                              ('http://new', 'next-model')])

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

    def test_structured_settings_validation_and_history_restore(self):
        import json
        tab = self.make_tab()
        panel = tab.options_panel
        panel.output_dropdown.set_selected(2)
        panel.schema_text = '{'
        tab.chat_input.entry.set_text('return count')
        tab.on_send_clicked()
        self.assertIsNone(tab.request)
        self.assertEqual(tab.chat_input.entry.get_text(), 'return count')
        panel.schema_text = json.dumps({'type': 'object', 'properties': {'count': {'type': 'integer'}}, 'required': ['count']})
        panel.keep_alive_dropdown.set_selected(1)
        def chat(**kwargs):
            self.assertEqual(kwargs['format']['type'], 'object')
            self.assertEqual(kwargs['keep_alive'], 0)
            yield {'message': {'content': '{"count":"two"}'}, 'done': True}
        with patch.object(ollama, 'chat', side_effect=chat):
            tab.on_send_clicked()
            pump_until(lambda: tab.request is None and self.storage.writer.idle)
        saved = self.storage.get_chat(tab.strategy.chat_id)
        self.assertEqual(saved['options']['keep_alive'], 0)
        self.assertEqual(saved['options']['output_mode'], 'schema')
        self.assertEqual(saved['messages'][-1]['response_metadata']['validation']['status'], 'schema_mismatch')
        restored = GenerationTab(mode='chat', chat_id=saved['id'], initial_history=saved['messages'], storage=self.storage)
        self.tabs.append(restored)
        self.assertEqual(restored.options_panel.schema_text, panel.schema_text)
        self.assertEqual(restored.options_panel.get_request_settings()['keep_alive'], 0)
        bubble = restored.message_list.list_box.get_last_child()
        self.assertIsNotNone(bubble.json_view)
        self.assertIn('does not match', bubble.json_view.status_label.get_text())
        restored.options_panel.output_dropdown.set_selected(0)
        self.assertIsNotNone(bubble.json_view)

    def test_schema_import_apply_and_json_export(self):
        import json
        from pathlib import Path
        from gi.repository import Gio
        from src.widgets.json_view import SchemaEditor, JsonResponseView, buffer_text
        from gi.repository import Adw
        path = Path(self.temp.name) / 'schema.json'
        path.write_text('{"type":"object"}')
        applied = []
        parent = Adw.Window()
        self.windows.append(parent)
        parent.present()
        editor = SchemaEditor('', applied.append)
        editor.present(parent)
        dialog = unittest.mock.Mock()
        dialog.open.side_effect = lambda parent, cancel, callback: callback(dialog, None)
        dialog.open_finish.return_value = Gio.File.new_for_path(str(path))
        with patch('src.widgets.json_view.Gtk.FileDialog', return_value=dialog):
            editor.import_schema()
            pump_until(lambda: bool(buffer_text(editor.editor)))
        editor.apply_schema()
        self.assertEqual(json.loads(applied[0]), {'type': 'object'})
        invalid = SchemaEditor('{', applied.append)
        invalid.apply_schema()
        self.assertTrue(invalid.error_label.get_visible())
        self.assertEqual(len(applied), 1)
        view = JsonResponseView()
        raw = '{"value": 3}'
        view.update(raw)
        view.finish({'status': 'valid'})
        self.assertTrue(view.save_json.get_sensitive())
        self.assertEqual(view.raw, raw)
        export_path = Path(self.temp.name) / 'output.json'
        dialog.save.side_effect = lambda parent, cancel, callback: callback(dialog, None)
        dialog.save_finish.return_value = Gio.File.new_for_path(str(export_path))
        with patch('src.widgets.json_view.Gtk.FileDialog', return_value=dialog):
            view.export()
            pump_until(lambda: export_path.exists() and export_path.stat().st_size > 0)
        self.assertEqual(json.loads(export_path.read_text()), {'value': 3})
        view.update('{')
        view.finish({'status': 'incomplete'})
        self.assertFalse(view.save_json.get_sensitive())
        self.assertEqual(buffer_text(view.view), '{')

    def test_vision_controls_and_text_only_history_preserve_images(self):
        from pathlib import Path
        import base64
        # A valid 1x1 PNG is also round-tripped through the image table.
        texture = Gdk.MemoryTexture.new(1, 1, Gdk.MemoryFormat.R8G8B8A8, GLib.Bytes.new(bytes([255, 0, 0, 255])), 4)
        png = base64.b64encode(texture.save_to_png_bytes().get_data()).decode('ascii')
        path = Path(self.temp.name) / 'image.png'
        path.write_bytes(base64.b64decode(png))
        tab = self.make_tab()
        tab.chat_input.set_model_details({'capabilities': ['vision']})
        tab.chat_input.selected_image_paths = [str(path)]
        tab.chat_input.update_image_preview()
        tab.chat_input.entry.set_text('describe this')
        requests = []
        def chat(**kwargs):
            requests.append(kwargs['messages'])
            yield {'message': {'content': 'a red pixel'}, 'done': True}
        with patch.object(ollama, 'chat', side_effect=chat):
            tab.on_send_clicked()
            pump_until(lambda: tab.request is None and self.storage.writer.idle)
            self.assertIn('images', requests[0][0])
            tab.chat_input.set_model_details({'capabilities': ['completion']})
            self.assertFalse(tab.chat_input.attach_button.get_sensitive())
            self.assertIn('only text history', tab.chat_input.capability_notice.get_text())
            tab.chat_input.entry.set_text('continue without vision')
            tab.on_send_clicked()
            pump_until(lambda: tab.request is None and self.storage.writer.idle)
            self.assertTrue(all('images' not in msg for msg in requests[-1]))
            saved = self.storage.get_chat(tab.strategy.chat_id)
            self.assertEqual(saved['messages'][0]['images'], [png])
            self.assertTrue(saved['messages'][-1]['response_metadata']['history_images_omitted'])
            tab.chat_input.selected_image_paths = [str(path)]
            tab.chat_input.update_image_preview()
            tab.chat_input.entry.set_text('keep draft')
            tab.on_send_clicked()
            self.assertEqual(tab.chat_input.entry.get_text(), 'keep draft')
            self.assertEqual(len(requests), 2)
            tab.chat_input.set_model_details({'capabilities': ['vision']})
            tab.on_send_clicked()
            pump_until(lambda: tab.request is None and self.storage.writer.idle)
            self.assertIn('images', requests[-1][0])
        tab.chat_input.set_model_details(None)
        self.assertTrue(tab.chat_input.attach_button.get_sensitive())
        self.assertIn('unknown', tab.chat_input.capability_notice.get_text())

    def test_native_tabs_sidebar_actions_and_deferred_close(self):
        window, first = self.make_window()
        second = window.new_chat_tab()
        self.tabs.append(second)
        pump_until(lambda: session.worker.idle and self.storage.writer.idle)
        page = window.tab_view.get_page(first)
        window.tab_view.reorder_page(page, 1)
        window.tab_view.set_selected_page(page)
        self.assertEqual(window.history_sidebar.get_selected_item().chat_id, first.strategy.chat_id)
        window.pin_chat(first.strategy.chat_id)
        pump_until(lambda: self.storage.writer.idle and window.chat_rows[first.strategy.chat_id].is_pinned)
        self.assertEqual(window.chat_rows[first.strategy.chat_id].get_section(), window.pinned_section)
        self.assertIs(window.open_chat_tab(self.storage.get_chat(first.strategy.chat_id)), first)
        self.assertEqual(window.tab_view.get_n_pages(), 2)
        window.update_tab_title(first.strategy.chat_id, 'Renamed')
        self.assertEqual(page.get_title(), 'Renamed')
        fixture = Server('stall')
        self.addCleanup(fixture.close)
        host = first.options_panel.get_selected_host()
        self.storage.update_host(host['id'], host['name'], fixture.host, True)
        first.update_hosts()
        pump_until(lambda: first.chat_input.get_selected_model() == 'test')
        first.chat_input.entry.set_text('save before close')
        first.on_send_clicked()
        pump_until(lambda: first.request and first.request.content == 'partial')
        self.assertTrue(page.get_loading())
        release = threading.Event()
        self.storage._submit(lambda: release.wait(3))
        window.tab_view.close_page(page)
        pump_until(lambda: first.request is None)
        self.assertEqual(window.tab_view.get_n_pages(), 2)
        release.set()
        pump_until(lambda: window.tab_view.get_n_pages() == 1)
        saved = self.storage.get_chat(first.strategy.chat_id)
        self.assertEqual(saved['messages'][-1]['response_metadata']['status'], 'stopped')
        window.delete_chat(first.strategy.chat_id)
        pump_until(lambda: self.storage.writer.idle)
        self.assertNotIn(first.strategy.chat_id, window.chat_rows)
        self.assertIsNone(self.storage.get_chat(first.strategy.chat_id))

    def test_running_models_refresh_unload_busy_and_close(self):
        from src.model_manager import ModelManagerDialog, unloading_models, model_key, running_model_subtitle
        self.stack.enter_context(patch.object(ollama, 'fetch_model_details', return_value=[]))
        calls = []
        def running(host, **kwargs):
            calls.append(host)
            return [{'name': 'test', 'size_vram': 0, 'context_length': 4096}]
        self.stack.enter_context(patch.object(ollama, 'fetch_running_models', side_effect=running))
        busy = True
        manager = ModelManagerDialog(self.storage, is_model_busy=lambda host, model: busy)
        self.windows.append(manager)
        manager.model_stack.set_visible_child_name('running')
        manager.present()
        pump_until(lambda: bool(manager._running_rows))
        row, host, model, button = manager._running_rows[0]
        self.assertFalse(button.get_sensitive())
        self.assertIn('Unavailable', running_model_subtitle({'size_vram': 0}))
        self.assertNotIn('VRAM: Unavailable', row.get_subtitle())
        busy = False
        manager.update_unload_buttons()
        gate = threading.Event()
        def unload(*args, **kwargs):
            gate.wait(2)
            return {'done': True}
        with patch.object(ollama, 'unload_model', side_effect=unload) as api:
            manager.on_unload_clicked(button, host, model)
            self.assertIn(model_key(host, model), unloading_models)
            self.assertFalse(button.get_sensitive())
            manager.on_unload_clicked(button, host, model)
            gate.set()
            pump_until(lambda: model_key(host, model) not in unloading_models and not manager._running_pending)
            self.assertEqual(api.call_count, 1)
        self.assertGreaterEqual(len(calls), 2)
        manager.close()
        pump_until(lambda: manager._poll_id is None and session.worker.idle)
        self.assertTrue(manager.requests.closed)

    def test_running_model_refresh_ignores_old_host_and_does_not_overlap(self):
        from src.model_manager import ModelManagerDialog
        self.stack.enter_context(patch.object(ollama, 'fetch_model_details', return_value=[]))
        old_host = self.storage.get_all_hosts()[0]
        new_host = self.storage.add_host('Other', 'http://other:11434', False)
        gate, started = threading.Event(), threading.Event()
        calls = []
        def running(host, **kwargs):
            calls.append(host)
            if host == old_host['hostname']:
                started.set()
                gate.wait(3)
                return [{'name': 'old'}]
            return [{'name': 'new'}]
        with patch.object(ollama, 'fetch_running_models', side_effect=running):
            manager = ModelManagerDialog(self.storage)
            self.windows.append(manager)
            manager.model_stack.set_visible_child_name('running')
            manager.present()
            pump_until(started.is_set)
            manager.refresh_running()
            self.assertEqual(calls.count(old_host['hostname']), 1)
            manager.host_dropdown.set_selected(1)
            pump_until(lambda: manager._running_rows and manager._running_rows[0][2] == 'new')
            gate.set()
            pump_until(lambda: session.worker.idle)
            self.assertEqual(manager._running_rows[0][2], 'new')
            manager.close()

    def test_adaptive_window_shortcuts_and_schema_layout(self):
        from gi.repository import Adw
        window, tab = self.make_window()
        window.set_default_size(400, 800)
        window.present()
        pump_until(lambda: window.get_mapped() and window.split_view.get_collapsed())
        self.assertLessEqual(window.get_width(), 400)
        window.lookup_action('toggle_sidebar').activate(None)
        self.assertTrue(window.split_view.get_show_sidebar())
        item = window.chat_rows[tab.strategy.chat_id]
        pump_until(lambda: self.storage.writer.idle)
        window.on_history_activated(window.history_sidebar, item.get_index())
        self.assertFalse(window.split_view.get_show_sidebar())
        tab.options_panel.open_settings()
        self.assertEqual(tab.options_panel._settings_dialog.get_title(), 'Chat Settings')
        tab.options_panel.output_dropdown.set_selected(2)
        tab.options_panel.keep_alive_dropdown.set_selected(5)
        tab.options_panel.keep_alive_entry.set_text('17')
        tab.options_panel.schema_text = '{"type":"object"}'
        self.assertEqual(tab.options_panel.get_request_settings()['keep_alive'], 17)
        pump_until(lambda: tab.options_panel._settings_dialog.get_mapped())
        tab.options_panel._settings_dialog.close()
        pump_until(lambda: tab.options_panel._settings_dialog is None)
        self.assertLessEqual(window.get_width(), 400)
        for scheme in (Adw.ColorScheme.FORCE_DARK, Adw.ColorScheme.FORCE_LIGHT):
            Adw.StyleManager.get_default().set_color_scheme(scheme)
        second = window.new_tab()
        self.tabs.append(second)
        window.lookup_action('previous_tab').activate(None)
        self.assertIs(window.tab_view.get_selected_page().get_child(), tab)
        window.lookup_action('next_tab').activate(None)
        self.assertIs(window.tab_view.get_selected_page().get_child(), second)
        window.lookup_action('close_tab').activate(None)
        pump_until(lambda: window.tab_view.get_n_pages() == 1)
        window.set_default_size(1000, 800)
        pump_until(lambda: not window.split_view.get_collapsed())
        self.assertTrue(window.split_view.get_show_sidebar())
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.DEFAULT)

    def test_keep_open_after_save_error_restarts_capability_fetches(self):
        window, tab = self.make_window()
        blocked = True
        def save():
            if blocked:
                raise OSError('disk full')
        with self.assertRaises(OSError):
            self.storage._submit(save).result(2)
        tab.chat_input.set_model_details(None, loading=True)
        window.request_shutdown()
        pump_until(lambda: window._save_error_dialog is not None)
        window._save_error_dialog.emit('response', 'keep')
        pump_until(lambda: tab.chat_input.get_selected_model() == 'test'
                   and not tab.chat_input.capabilities_loading and session.worker.idle)
        self.assertFalse(window._shutting_down)
        self.assertIs(tab.chat_input.image_support, False)
        blocked = False
        self.storage.writer.retry()
        pump_until(lambda: self.storage.writer.idle)

    def test_selecting_schema_opens_editor_and_empty_send_preserves_prompt(self):
        from src.widgets.json_view import buffer_text
        window, tab = self.make_window()
        window.present()
        panel = tab.options_panel
        self.assertIsNone(panel._settings_dialog)
        pump_until(lambda: panel.output_dropdown.get_mapped())
        tab.chat_input.entry.set_text('Return JSON with count equal to 7.')
        response = iter([{'message': {'content': '{"count":7}'}, 'done': True}])
        with patch.object(ollama, 'chat', return_value=response) as chat:
            panel.output_dropdown.set_selected(2)
            pump_until(lambda: panel._schema_dialog is not None)
            editor = panel._schema_dialog
            self.assertEqual(buffer_text(editor.editor), '')
            self.assertFalse(editor.error_label.get_visible())
            self.assertIsNone(tab.message_list.list_box.get_first_child())
            chat.assert_not_called()
            editor.close()
            pump_until(lambda: panel._schema_dialog is None)
            tab.on_send_clicked()
            pump_until(lambda: panel._schema_dialog is not None)
            editor = panel._schema_dialog
            self.assertIn('Paste or import', editor.error_label.get_text())
            self.assertNotIn('Expecting value', editor.error_label.get_text())
            self.assertIsNone(tab.request)
            self.assertEqual(tab.chat_input.entry.get_text(), 'Return JSON with count equal to 7.')
            self.assertIsNone(tab.message_list.list_box.get_first_child())
            chat.assert_not_called()
            editor.editor.get_buffer().set_text('{')
            editor.apply_schema()
            self.assertIs(panel._schema_dialog, editor)
            self.assertEqual(panel.schema_text, '')
            self.assertIn('Invalid schema JSON', editor.error_label.get_text())
            editor.editor.get_buffer().set_text('{"type":"object","properties":{"count":{"type":"integer"}},"required":["count"]}')
            editor.apply_schema()
            pump_until(lambda: panel._schema_dialog is None)
            tab.on_send_clicked()
            pump_until(lambda: tab.request is None and self.storage.writer.idle)
            self.assertEqual(chat.call_count, 1)
            self.assertEqual(chat.call_args.kwargs['format']['required'], ['count'])
            self.assertEqual(self.storage.get_chat(tab.strategy.chat_id)['messages'][-1]['response_metadata']['validation']['status'], 'valid')

    def test_restoring_schema_settings_does_not_open_editor(self):
        window, tab = self.make_window()
        window.present()
        panel = tab.options_panel
        self.assertIsNone(panel._settings_dialog)
        pump_until(lambda: panel.output_dropdown.get_mapped())
        for text in ('{"type":"object"}', ''):
            panel.output_dropdown.set_selected(0)
            with patch.object(panel, 'edit_schema') as edit:
                panel.load_options({'output_mode': 'schema', 'schema_text': text})
                pump_until(lambda: True)
                edit.assert_not_called()
            self.assertEqual(panel.schema_text, text)
            self.assertTrue(panel.schema_button.get_visible())
        panel.output_dropdown.set_selected(0)
        panel.schema_text = '{'
        panel.output_dropdown.set_selected(2)
        tab.chat_input.entry.set_text('Keep this prompt')
        tab.on_send_clicked()
        pump_until(lambda: panel._schema_dialog is not None)
        self.assertIn('Invalid schema JSON', panel._schema_dialog.error_label.get_text())
        self.assertIsNone(tab.message_list.list_box.get_first_child())
        self.assertEqual(tab.chat_input.entry.get_text(), 'Keep this prompt')
        panel._schema_dialog.close()
        pump_until(lambda: panel._schema_dialog is None)
