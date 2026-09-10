import copy
import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from gi.repository import Gdk, Gio, Gtk
from src import ollama, session
from src.database import DatabaseManager
from src.session import RequestState, api_messages
from src.tab import GenerationTab
from src.tool_calling import EXAMPLE_TOOLS, InvalidTools, inspect_calls, parse_tools
from src.widgets.json_view import buffer_text
from src.widgets.tool_view import ToolsEditor
import test_ui
from test_session import settings
from test_transport import Server


def call(path='calculator.py', id=None, name='read_file'):
    result = {'function': {'index': 0, 'name': name, 'arguments': {'path': path}}}
    if id:
        result['id'] = id
    return result


class ToolProtocolTests(unittest.TestCase):
    def test_definitions_and_offline_schema_validation(self):
        self.assertEqual([tool['function']['name'] for tool in parse_tools(EXAMPLE_TOOLS)],
                         ['list_files', 'read_file', 'search_code', 'replace_in_file', 'run_command'])
        for text in ('', '[]', '{}', '[{"type":"other"}]', '[{"type":"function"}]', '{'):
            with self.subTest(text=text), self.assertRaises(InvalidTools):
                parse_tools(text)
        tool = parse_tools(EXAMPLE_TOOLS)[0]
        with self.assertRaisesRegex(InvalidTools, 'unique'):
            parse_tools(json.dumps([tool, tool]))
        tool['function']['parameters']['properties']['path'] = {'$ref': 'https://example.invalid/path.json'}
        with self.assertRaisesRegex(InvalidTools, 'self-contained'):
            parse_tools(json.dumps([tool]))

    def test_argument_checks_are_advisory_but_malformed_calls_cannot_continue(self):
        tools = parse_tools(EXAMPLE_TOOLS)
        checks = inspect_calls([call(), call(3), call(name='unknown')], tools, 'complete')
        self.assertEqual(checks['state'], 'pending')
        self.assertEqual([c['status'] for c in checks['validation']], ['valid', 'mismatch', 'unknown'])
        for malformed in ({}, 'raw', {'function': {'name': 'tool', 'arguments': '{}'}}, call(float('nan'))):
            self.assertEqual(inspect_calls([malformed], tools, 'complete')['state'], 'invalid')
        self.assertEqual(inspect_calls([call()], tools, 'stopped')['state'], 'incomplete')

    def test_stream_accumulation_history_fields_and_final_validation(self):
        config = settings()
        config.update(tools=parse_tools(EXAMPLE_TOOLS), format={'type': 'object'})
        state = RequestState(config, 'weather')
        first, second = call(id='one'), call('tests/test_calculator.py')
        state.consume({'message': {'thinking': 'look up', 'tool_calls': [first]}})
        first['function']['arguments']['path'] = 'mutated'
        state.consume({'message': {'thinking': ' both', 'tool_calls': [second]}, 'done': True})
        state.finish('complete')
        self.assertEqual(state.tool_calls[0]['function']['arguments']['path'], 'calculator.py')
        self.assertNotIn('validation', state.metadata)
        history = [{'role': 'assistant', 'content': '', 'thinking_content': state.thinking,
                    'tool_calls': state.tool_calls, 'response_metadata': state.metadata, 'api_details': {'private': True}},
                   {'role': 'tool', 'tool_name': 'read_file', 'tool_call_id': 'one',
                    'content': '', 'response_metadata': {'call_index': 0}}]
        wire = api_messages(history)
        self.assertEqual(wire[0], {'role': 'assistant', 'content': '', 'thinking': 'look up both', 'tool_calls': state.tool_calls})
        self.assertEqual(wire[1], {'role': 'tool', 'content': '', 'tool_name': 'read_file', 'tool_call_id': 'one'})
        history[0]['response_metadata']['status'] = 'stopped'
        self.assertNotIn('tool_calls', api_messages(history)[0])

    def test_transport_passes_tools_and_structured_output_together(self):
        fixture = Server()
        self.addCleanup(fixture.close)
        tools = parse_tools(EXAMPLE_TOOLS)
        original = copy.deepcopy(tools)
        messages = [{'role': 'user', 'content': 'weather'}]
        list(ollama.chat(fixture.host, 'test', messages, tools=tools, format={'type': 'object'}, thinking=True))
        path, payload = fixture.server.received[-1]
        self.assertEqual(path, '/api/chat')
        self.assertEqual(payload['tools'], original)
        self.assertEqual(payload['format'], {'type': 'object'})
        self.assertEqual(tools, original)
        list(ollama.chat(fixture.host, 'test', messages))
        self.assertNotIn('tools', fixture.server.received[-1][1])


@unittest.skipUnless(Gdk.Display.get_default(), 'GTK tests require a display')
class ToolUITests(unittest.TestCase):
    setUp = test_ui.UITests.setUp
    tearDown = test_ui.UITests.tearDown
    make_tab = test_ui.UITests.make_tab
    make_window = test_ui.UITests.make_window

    def settle(self, tab):
        test_ui.pump_until(lambda: tab.request is None and not tab._tool_busy and self.storage.writer.idle and session.worker.idle)

    def tool_tab(self, calls=None):
        tab = self.make_tab()
        tab.options_panel.tools_text = EXAMPLE_TOOLS
        tab.options_panel.tools_check.set_active(True)
        tab.chat_input.entry.set_text('weather')
        with patch.object(ollama, 'chat', return_value=iter([
            {'message': {'thinking': 'look up', 'tool_calls': calls if calls is not None else [call(id='a'), call('tests/test_calculator.py')]}, 'done': True}
        ])):
            tab.on_send_clicked()
            self.settle(tab)
        return tab

    def test_manual_round_restores_and_uses_current_settings(self):
        tab = self.tool_tab()
        pending = tab.strategy.pending_round
        self.assertIsNotNone(pending)
        self.assertIsNone(tab.request)
        self.assertFalse(tab.chat_input.send_button.get_sensitive())
        self.assertTrue(session.worker.idle)
        tab._save_tool_result(pending, 0, '')
        self.settle(tab)
        self.assertFalse(tab._tool_views[0].continue_button.get_sensitive())
        closed = []
        tab.close_session(lambda: closed.append(True))
        test_ui.pump_until(lambda: bool(closed))
        saved = self.storage.get_chat(tab.strategy.chat_id)
        restored = GenerationTab(mode='chat', chat_id=tab.strategy.chat_id, initial_history=saved['messages'], storage=self.storage)
        self.tabs.append(restored)
        test_ui.pump_until(lambda: restored.chat_input.get_selected_model() == 'test' and session.worker.idle)
        self.assertEqual(restored.options_panel.tools_text, EXAMPLE_TOOLS)
        self.assertEqual(restored.strategy.pending_round['response_metadata']['tool_round']['results'], ['', None])
        restored._save_tool_result(restored.strategy.pending_round, 1, '{"temperature": 18}')
        self.settle(restored)
        self.assertTrue(restored._tool_views[0].continue_button.get_sensitive())
        host = self.storage.add_host('other', 'http://other.example:11434')
        restored.update_hosts()
        index = next(i for i, h in enumerate(restored.options_panel.host_list) if h['id'] == host['id'])
        restored.options_panel.host_dropdown.set_selected(index)
        test_ui.pump_until(lambda: session.worker.idle and restored.chat_input.get_selected_model() == 'test')
        restored.chat_input.set_models(['other-model'])
        test_ui.pump_until(lambda: session.worker.idle)
        restored.options_panel.temperature_entry.set_text('0.2')
        restored.options_panel.output_dropdown.set_selected(2)
        restored.options_panel.schema_text = '{"type":"object","required":["summary"]}'
        restored.options_panel.tools_check.set_active(False)
        restored.chat_input.entry.set_text('draft stays')
        with patch.object(ollama, 'chat', return_value=iter([{'message': {'content': '{"summary":"done"}'}, 'done': True}])) as chat:
            restored.on_send_clicked(continuation=True)
            restored.on_send_clicked(continuation=True)
            self.settle(restored)
        chat.assert_called_once()
        args = chat.call_args.kwargs
        self.assertEqual(args['host'], 'http://other.example:11434')
        self.assertEqual(args['model'], 'other-model')
        self.assertEqual(args['options']['temperature'], 0.2)
        self.assertIsNone(args['tools'])
        self.assertEqual([m['role'] for m in args['messages']], ['user', 'assistant', 'tool', 'tool'])
        self.assertEqual(args['messages'][1]['thinking'], 'look up')
        self.assertEqual(args['messages'][2]['tool_call_id'], 'a')
        self.assertEqual(args['messages'][2]['content'], '')
        self.assertNotIn('tool_call_id', args['messages'][3])
        self.assertEqual(restored.chat_input.entry.get_text(), 'draft stays')
        history = self.storage.get_chat(restored.strategy.chat_id)['messages']
        self.assertEqual(len(history), 5)
        self.assertEqual(history[-1]['response_metadata']['validation']['status'], 'valid')
        self.assertIsNone(restored.strategy.pending_round)

    def test_successive_rounds_and_cancellation_results(self):
        tab = self.tool_tab([call(123), call(name='missing')])
        tab._save_tool_result(tab.strategy.pending_round, 0, 'first')
        self.settle(tab)
        with patch.object(ollama, 'chat') as chat:
            tab._cancel_tool_round()
            self.settle(tab)
            chat.assert_not_called()
        history = self.storage.get_chat(tab.strategy.chat_id)['messages']
        self.assertEqual(history[-2]['content'], 'first')
        self.assertIn('cancelled by user', history[-1]['content'])
        self.assertTrue(history[-1]['response_metadata']['cancelled'])
        self.assertIsNone(tab.strategy.pending_round)
        self.assertTrue(tab.chat_input.entry.get_sensitive())
        tab.chat_input.entry.set_text('try again')
        with patch.object(ollama, 'chat', return_value=iter([{'message': {'tool_calls': [call()]}, 'done': True}])):
            tab.on_send_clicked()
            self.settle(tab)
        tab._save_tool_result(tab.strategy.pending_round, 0, '20')
        self.settle(tab)
        with patch.object(ollama, 'chat', return_value=iter([{'message': {'tool_calls': [call('src/main.py')]}, 'done': True}])):
            tab.on_send_clicked(continuation=True)
            self.settle(tab)
        self.assertEqual(tab.strategy.pending_round['tool_calls'][0]['function']['arguments']['path'], 'src/main.py')
        self.assertEqual(len(tab._tool_views), 3)

    def test_result_editor_saves_empty_text_and_closes_with_tab(self):
        tab = self.tool_tab([call()])
        window = Gtk.Window(child=tab)
        self.windows.append(window)
        window.present()
        view = tab._tool_views[0]
        view.edit_result(view.buttons[0], 0)
        self.assertIsNotNone(view.dialog)
        view.dialog.apply_text()
        self.settle(tab)
        test_ui.pump_until(lambda: view.dialog is None)
        self.assertEqual(tab.strategy.pending_round['response_metadata']['tool_round']['results'], [''])
        view.edit_result(view.buttons[0], 0)
        closed = []
        tab.close_session(lambda: closed.append(True))
        test_ui.pump_until(lambda: closed and view.dialog is None)

    def test_definition_editor_import_export_and_automatic_open(self):
        tab = self.make_tab()
        window = Gtk.Window(child=tab)
        self.windows.append(window)
        window.present()
        tab.options_panel.advanced_expander.set_expanded(False)
        test_ui.pump_until(lambda: tab.options_panel.get_mapped())
        tab.options_panel.tools_check.set_active(True)
        test_ui.pump_until(lambda: tab.options_panel._tools_dialog is not None)
        editor = tab.options_panel._tools_dialog
        self.assertLessEqual(editor.get_child().measure(Gtk.Orientation.HORIZONTAL, -1).minimum, 360)
        tab.chat_input.entry.set_text('keep prompt')
        tab.on_send_clicked()
        self.assertEqual(tab.chat_input.entry.get_text(), 'keep prompt')
        self.assertTrue(editor.error_label.get_visible())
        editor.content_box.get_first_child().get_child_at_index(0).get_child().emit('clicked')
        self.assertEqual(buffer_text(editor.editor), EXAMPLE_TOOLS)
        editor.editor.get_buffer().set_text('')
        path = Path(self.temp.name) / 'tools.json'
        path.write_text(EXAMPLE_TOOLS)
        file_dialog = Mock()
        file_dialog.open.side_effect = lambda parent, cancel, callback: callback(file_dialog, None)
        file_dialog.open_finish.return_value = Gio.File.new_for_path(str(path))
        with patch.object(Gtk, 'FileDialog', return_value=file_dialog):
            editor.import_text()
            test_ui.pump_until(lambda: buffer_text(editor.editor) == EXAMPLE_TOOLS)
        output = Path(self.temp.name) / 'export.json'
        file_dialog.save.side_effect = lambda parent, cancel, callback: callback(file_dialog, None)
        file_dialog.save_finish.return_value = Gio.File.new_for_path(str(output))
        with patch.object(Gtk, 'FileDialog', return_value=file_dialog):
            editor.export_text('Tools', 'tools.json')
            test_ui.pump_until(lambda: output.exists() and output.stat().st_size > 0)
        self.assertEqual(parse_tools(output.read_text()), parse_tools(EXAMPLE_TOOLS))
        editor.apply_text()
        test_ui.pump_until(lambda: tab.options_panel._tools_dialog is None and self.storage.writer.idle)
        self.assertEqual(self.storage.get_chat(tab.strategy.chat_id)['options']['tools_text'], EXAMPLE_TOOLS)
        self.assertIn('Enabled tools: 5', tab.options_panel.tools_notice.get_text())
        self.assertIn('no tool support', tab.options_panel.tools_notice.get_text())
        other = self.make_tab()
        self.assertFalse(other.options_panel.tools_check.get_active())
        self.assertEqual(other.options_panel.tools_text, '')
        response = GenerationTab(mode='generate', storage=self.storage)
        self.tabs.append(response)
        self.assertFalse(response.options_panel.tools_box.get_visible())

    def test_save_failure_blocks_continuation_and_retry_does_not_duplicate(self):
        tab = self.tool_tab([call()])
        tab._save_tool_result(tab.strategy.pending_round, 0, '20')
        self.settle(tab)
        save = self.storage.db.save_chat
        blocked = True
        def saving(*args, **kwargs):
            if blocked:
                raise OSError('disk full')
            return save(*args, **kwargs)
        with patch.object(self.storage.db, 'save_chat', side_effect=saving), patch.object(ollama, 'chat', return_value=iter([{'message': {'content': 'done'}, 'done': True}])) as chat:
            try:
                tab.on_send_clicked(continuation=True)
                test_ui.pump_until(lambda: self.storage.writer.error is not None)
                chat.assert_not_called()
                tab.on_send_clicked(continuation=True)
                blocked = False
                self.storage.writer.retry()
                self.settle(tab)
                chat.assert_called_once()
            finally:
                blocked = False
                if self.storage.writer.error:
                    self.storage.writer.retry()
                self.settle(tab)
        history = self.storage.get_chat(tab.strategy.chat_id)['messages']
        self.assertEqual([m['role'] for m in history], ['user', 'assistant', 'tool', 'assistant'])

    def test_stopped_malformed_and_failed_continuations(self):
        tab = self.make_tab()
        tab.options_panel.tools_text = EXAMPLE_TOOLS
        tab.options_panel.tools_check.set_active(True)
        tab.chat_input.entry.set_text('weather')
        def interrupted(**kwargs):
            yield {'message': {'content': 'partial', 'tool_calls': [call()]}}
            raise ollama.RequestCancelled()
        with patch.object(ollama, 'chat', side_effect=interrupted):
            tab.on_send_clicked()
            self.settle(tab)
        self.assertIsNone(tab.strategy.pending_round)
        self.assertEqual(tab._tool_views[0].message['response_metadata']['tool_round']['state'], 'incomplete')
        wire = api_messages(tab.strategy.history)
        self.assertNotIn('tool_calls', wire[-1])
        tab.chat_input.entry.set_text('again')
        with patch.object(ollama, 'chat', return_value=iter([{'message': {'tool_calls': [{}]}, 'done': True}])):
            tab.on_send_clicked()
            self.settle(tab)
        self.assertIsNone(tab.strategy.pending_round)
        self.assertEqual(tab._tool_views[-1].message['response_metadata']['tool_round']['state'], 'invalid')
        another = self.tool_tab([call()])
        another._save_tool_result(another.strategy.pending_round, 0, '20')
        self.settle(another)
        with patch.object(ollama, 'chat', side_effect=ollama.OllamaError('server rejected combination')):
            another.on_send_clicked(continuation=True)
            self.settle(another)
        self.assertIsNone(another.strategy.pending_round)
        self.assertEqual(another.strategy.history[-1]['response_metadata']['status'], 'failed')
        self.assertEqual(sum(m['role'] == 'tool' for m in another.strategy.history), 1)

    def test_v4_migration_and_tool_state_merge_preserve_other_settings(self):
        path = str(Path(self.temp.name) / 'old.db')
        db = DatabaseManager(path)
        db.create_chat('old', 'old', 1, 1, 'model')
        db.save_chat('old', [{'role': 'assistant', 'content': 'answer', 'response_metadata': {'status': 'complete'}}],
                     options={'temperature': 0.7, 'schema_text': '{}'})
        with db._get_conn() as conn:
            for column in ('tool_calls', 'tool_name', 'tool_call_id'):
                conn.execute('ALTER TABLE messages DROP COLUMN ' + column)
            for table in ('knowledge_web_sources', 'knowledge_collection_documents', 'knowledge_collections', 'knowledge_chunks', 'knowledge_indexes', 'embedding_configs', 'knowledge_documents'):
                conn.execute('DROP TABLE ' + table)
            conn.execute('PRAGMA user_version = 4')
            conn.commit()
        db = DatabaseManager(path)
        db.save_tool_state('old', options={'tools_text': EXAMPLE_TOOLS, 'tools_enabled': True})
        saved = db.get_chat('old')
        self.assertEqual(saved['options']['temperature'], 0.7)
        self.assertEqual(saved['options']['schema_text'], '{}')
        self.assertEqual(saved['messages'][0]['content'], 'answer')
        with db._get_conn() as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 9)
        db.delete_chat('old')
        db.save_tool_state('old', messages=[], options={'tools_text': EXAMPLE_TOOLS})
        self.assertIsNone(db.get_chat('old'))

    def test_stop_and_close_while_results_wait_for_storage(self):
        import threading
        tab = self.tool_tab([call()])
        tab._save_tool_result(tab.strategy.pending_round, 0, '20')
        self.settle(tab)
        release = threading.Event()
        self.storage._submit(lambda: release.wait(3))
        closed = []
        with patch.object(ollama, 'chat') as chat:
            try:
                tab.on_send_clicked(continuation=True)
                tab.on_send_or_stop()
                tab.close_session(lambda: closed.append(True))
                self.assertFalse(closed)
                chat.assert_not_called()
            finally:
                release.set()
            test_ui.pump_until(lambda: bool(closed))
            self.settle(tab)
            chat.assert_not_called()
        history = self.storage.get_chat(tab.strategy.chat_id)['messages']
        self.assertEqual([m['role'] for m in history], ['user', 'assistant', 'tool', 'assistant'])
        self.assertEqual(history[-1]['response_metadata']['status'], 'stopped')

    def test_definitions_survive_empty_tab_close_and_cleanup(self):
        window, tab = self.make_window()
        tab.options_panel.tools_text = EXAMPLE_TOOLS
        tab.options_panel.tools_check.set_active(True)
        test_ui.pump_until(lambda: self.storage.writer.idle)
        window.close_tab(tab)
        test_ui.pump_until(lambda: tab._disposed)
        self.storage.cleanup_empty_chats().result(2)
        saved = self.storage.get_chat(tab.strategy.chat_id)
        self.assertEqual(saved['options']['tools_text'], EXAMPLE_TOOLS)
        self.assertEqual(saved['messages'], [])
