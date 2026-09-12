"""Sequential exchanges and recovery must never duplicate a model request."""
import copy
import json
import tempfile
import threading
import unittest
from contextlib import ExitStack
from unittest.mock import patch
from gi.repository import Gdk
from src import ollama
from src.storage import ChatStorage
from src.model_conversation import ModelConversationController, conversation_messages
from src.export import export_json, export_markdown
from test_ui import pump_until
import test_ui


class ConversationTests(unittest.TestCase):
    def setUp(self):
        ollama.resume()
        self.temp = tempfile.TemporaryDirectory()
        self.storage = ChatStorage(self.temp.name)
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(ollama, 'fetch_models', return_value=['one', 'two']))
        self.stack.enter_context(patch.object(ollama, 'show_model', return_value={'capabilities': ['completion', 'thinking']}))
        host = self.storage.get_all_hosts()[0]
        self.participants = [dict(host_id=host['id'], model=model, options={'temperature': index / 2},
            system='system ' + model, thinking=None, keep_alive=None, logprobs=False, top_logprobs=None,
            show_stats=True) for index, model in enumerate(('one', 'two'))]
        self.controller = ModelConversationController(self.storage)

    def tearDown(self):
        self.controller.stop()
        pump_until(lambda: not self.controller.busy and self.storage.services.idle and self.storage.writer.idle)
        self.storage.writer.shutdown()
        self.storage.services.shutdown()
        self.storage.knowledge.shutdown()
        self.stack.close()
        self.temp.cleanup()

    def snapshot(self):
        return self.storage.export_snapshot(self.controller.id)

    def run_to_end(self, rounds=2, chat=None):
        calls = []
        def respond(**kwargs):
            calls.append(copy.deepcopy(kwargs['messages']))
            yield dict(message={'content': 'reply' + str(len(calls)), 'thinking': 'private'}, done=True)
        with patch.object(ollama, 'chat', side_effect=chat or respond):
            self.controller.start('  opening\n```text\ncode\n```', rounds, self.participants)
            pump_until(lambda: not self.controller.busy)
        return calls

    def test_exact_histories_and_round_count(self):
        calls = self.run_to_end()
        self.assertEqual(len(calls), 4)
        self.assertEqual([m['role'] for m in calls[2]], ['system', 'user', 'assistant', 'user'])
        self.assertEqual([m['content'] for m in calls[3]], ['system two', 'reply1', 'reply2', 'reply3'])
        self.assertEqual([m['role'] for m in calls[3]], ['system', 'user', 'assistant', 'user'])
        self.assertEqual(calls[0][1]['content'], '  opening\n```text\ncode\n```')
        self.assertNotIn('private', json.dumps(calls))
        snapshot = self.snapshot()
        self.assertEqual(snapshot['run']['status'], 'complete')
        self.assertEqual(snapshot['run']['next_turn'], 4)
        self.assertEqual(len(snapshot['messages']), 5)
        self.assertEqual(snapshot['messages'][1]['thinking_content'], 'private')
        self.assertEqual(snapshot['options']['participants'][1]['options']['temperature'], .5)

    def test_same_model_is_allowed_and_settings_stay_separate(self):
        self.participants[1]['model'] = 'one'
        self.run_to_end(1)
        settings = self.snapshot()['options']['participants']
        self.assertEqual([s['model'] for s in settings], ['one', 'one'])
        self.assertNotEqual(settings[0]['system'], settings[1]['system'])

    def test_pause_finishes_current_turn_and_resume_after_reopen(self):
        calls = []
        def respond(**kwargs):
            calls.append(kwargs['model'])
            yield dict(message={'content': kwargs['model']}, done=True)
        self.controller.started = lambda *a: self.controller.pause()
        with patch.object(ollama, 'chat', side_effect=respond):
            self.controller.start('opening', 1, self.participants)
            pump_until(lambda: not self.controller.busy)
            self.assertEqual(calls, ['one'])
            self.assertEqual(self.snapshot()['run']['status'], 'paused')
            controller = ModelConversationController(self.storage)
            controller.load(self.snapshot())
            self.controller = controller
            self.assertEqual(calls, ['one'])
            controller.resume()
            controller.resume()
            pump_until(lambda: not controller.busy)
        self.assertEqual(calls, ['one', 'two'])
        self.assertEqual(self.snapshot()['run']['status'], 'complete')

    def test_stop_cancels_and_never_hands_off_partial(self):
        def respond(**kwargs):
            yield dict(message={'content': 'partial'})
            kwargs['cancellable'].cancel()
            raise ollama.RequestCancelled()
        self.controller.chunk = lambda *a: self.controller.stop()
        with patch.object(ollama, 'chat', side_effect=respond) as api:
            self.controller.start('opening', 4, self.participants)
            pump_until(lambda: not self.controller.busy)
            self.controller.resume()
            self.assertEqual(api.call_count, 1)
        snapshot = self.snapshot()
        self.assertEqual(snapshot['run']['status'], 'stopped')
        self.assertEqual(snapshot['run']['next_turn'], 0)
        self.assertEqual(snapshot['messages'][1]['content'], 'partial')

    def test_failed_attempt_is_preserved_and_excluded_from_retry_history(self):
        def fail(**kwargs):
            yield dict(message={'content': 'bad partial'})
            raise ollama.OllamaError('offline')
        self.run_to_end(1, fail)
        self.assertEqual(self.snapshot()['run']['status'], 'failed')
        calls = []
        def respond(**kwargs):
            calls.append(copy.deepcopy(kwargs['messages']))
            yield dict(message={'content': 'good'}, done=True)
        with patch.object(ollama, 'chat', side_effect=respond):
            self.controller.resume()
            pump_until(lambda: not self.controller.busy)
        self.assertEqual(len(calls), 2)
        self.assertNotIn('bad partial', json.dumps(calls))
        self.assertEqual([a['status'] for a in self.snapshot()['attempts']], ['failed', 'complete', 'complete'])
        self.assertEqual([a['attempt'] for a in self.snapshot()['attempts']], [1, 2, 1])

    def test_empty_answer_does_not_advance(self):
        def empty(**kwargs):
            yield dict(message={'content': ' \n', 'thinking': 'thought'}, done=True)
        self.run_to_end(2, empty)
        self.assertEqual(self.snapshot()['run']['next_turn'], 0)
        self.assertEqual(self.snapshot()['run']['status'], 'failed')

    def test_output_limit_completion_can_handoff(self):
        def response(**kwargs):
            yield dict(message={'content': 'bounded'}, done=True, done_reason='length')
        self.run_to_end(1, response)
        self.assertEqual(self.snapshot()['run']['status'], 'complete')

    def test_draft_consumption_and_validation_failure(self):
        draft = dict(id='draft', mode='model_conversation', text='opening', settings={}, targets=[], revision=2)
        self.storage.db.save_draft(draft)
        broken = copy.deepcopy(self.participants)
        broken[0]['model'] = 'missing'
        self.controller.start('opening', 1, broken, 'draft', 2)
        pump_until(lambda: not self.controller.busy)
        self.assertIsNotNone(self.storage.get_draft('draft'))
        self.assertIsNone(self.controller.id)
        with patch.object(ollama, 'chat', return_value=iter([dict(message={'content': 'ok'}, done=True)])):
            self.controller.start('opening', 1, self.participants, 'draft', 2)
            pump_until(lambda: not self.controller.busy)
        self.assertIsNone(self.storage.get_draft('draft'))

    def test_save_failure_blocks_next_request_and_retry_is_idempotent(self):
        original = self.storage.db.finish_model_conversation_attempt
        failed = threading.Event()
        def fail_once(*args):
            if not failed.is_set():
                failed.set()
                raise OSError('disk full')
            return original(*args)
        with patch.object(self.storage.db, 'finish_model_conversation_attempt', side_effect=fail_once), patch.object(
                ollama, 'chat', side_effect=lambda **kw: iter([dict(message={'content': 'ok'}, done=True)])) as api:
            self.controller.start('opening', 1, self.participants)
            pump_until(lambda: self.storage.writer.error is not None)
            self.assertEqual(api.call_count, 1)
            self.assertTrue(self.controller.busy)
            self.storage.writer.retry()
            pump_until(lambda: not self.controller.busy)
            self.assertEqual(api.call_count, 2)
        snapshot = self.snapshot()
        attempt = snapshot['attempts'][0]
        original(attempt['id'], snapshot['messages'][1])
        self.assertEqual(len(self.snapshot()['messages']), 3)

    def test_recovery_marks_only_unfinished_work(self):
        self.run_to_end(1)
        snapshot = self.snapshot()
        with self.storage.db._get_conn() as conn:
            conn.execute("UPDATE model_conversation_runs SET status='running',next_turn=1 WHERE id=?", (snapshot['id'],))
            conn.execute("UPDATE model_conversation_attempts SET status='running' WHERE id=?", (snapshot['attempts'][1]['id'],))
            conn.commit()
        self.storage.db.interrupt_model_conversations()
        snapshot = self.snapshot()
        self.assertEqual(snapshot['run']['status'], 'interrupted')
        self.assertEqual([a['status'] for a in snapshot['attempts']], ['complete', 'interrupted'])
        self.assertEqual(snapshot['messages'][2]['response_metadata']['status'], 'interrupted')

    def test_exports_search_and_cascade_deletion(self):
        self.run_to_end(1)
        snapshot = self.snapshot()
        markdown = export_markdown(snapshot)
        self.assertIn('Round 1 · Model B', markdown)
        self.assertIn('system one', markdown)
        self.assertIn('"attempts"', export_json(snapshot))
        self.assertEqual(self.storage.list_history('reply2')[0]['id'], snapshot['id'])
        self.storage.db.delete_chat(snapshot['id'])
        self.assertIsNone(self.snapshot())
        with self.storage.db._get_conn() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM model_conversation_attempts').fetchone()[0], 0)

    def test_changed_host_blocks_resume_without_substitution(self):
        self.controller.started = lambda *a: self.controller.pause()
        self.run_to_end(1)
        host = self.storage.get_all_hosts()[0]
        self.storage.update_host(host['id'], host['name'], 'http://different.test:11434')
        with patch.object(ollama, 'chat') as api:
            self.controller.resume()
            pump_until(lambda: not self.controller.busy)
            self.assertFalse(api.called)
        self.assertIn('server address changed', self.controller.error)

    def test_pause_during_preparation_starts_no_requests(self):
        with patch.object(ollama, 'chat') as api:
            self.controller.start('opening', 2, self.participants)
            self.controller.pause()
            pump_until(lambda: not self.controller.busy)
            self.assertFalse(api.called)
        self.assertEqual(self.snapshot()['run']['status'], 'paused')
        self.assertEqual(self.snapshot()['attempts'], [])

    def test_paused_run_can_be_stopped_without_a_new_request(self):
        with patch.object(ollama, 'chat') as api:
            self.controller.start('opening', 2, self.participants)
            self.controller.pause()
            pump_until(lambda: not self.controller.busy)
            self.controller.stop()
            pump_until(lambda: not self.controller.busy)
            self.assertFalse(api.called)
        self.assertEqual(self.snapshot()['run']['status'], 'stopped')

    def test_pause_while_attempt_save_is_pending_discards_placeholder(self):
        original = self.storage.db.start_model_conversation_attempt
        entered, release = threading.Event(), threading.Event()
        def blocked(*args):
            entered.set()
            release.wait(3)
            return original(*args)
        with patch.object(self.storage.db, 'start_model_conversation_attempt', side_effect=blocked), patch.object(ollama, 'chat') as api:
            self.controller.start('opening', 1, self.participants)
            pump_until(entered.is_set)
            self.controller.pause()
            release.set()
            pump_until(lambda: not self.controller.busy)
            self.assertFalse(api.called)
        self.assertEqual(self.snapshot()['attempts'], [])
        self.assertEqual(len(self.snapshot()['messages']), 1)

    def test_final_turn_pause_completes_run(self):
        self.controller.started = lambda attempt, state: self.controller.pause() if attempt['turn'] == 1 else None
        self.run_to_end(1)
        self.assertEqual(self.snapshot()['run']['status'], 'complete')

    def test_interruption_saves_partial_and_retries_same_turn(self):
        def stalled(**kwargs):
            yield dict(message={'content': 'partial'})
            while not kwargs['cancellable'].is_cancelled():
                threading.Event().wait(.005)
            raise ollama.RequestCancelled()
        with patch.object(ollama, 'chat', side_effect=stalled) as api:
            self.controller.start('opening', 1, self.participants)
            pump_until(lambda: any(s.content for s in self.controller.states.values()))
            self.controller.stop(interrupted=True)
            pump_until(lambda: not self.controller.busy)
            self.assertEqual(api.call_count, 1)
        snapshot = self.snapshot()
        self.assertEqual(snapshot['run']['status'], 'interrupted')
        self.assertEqual(snapshot['messages'][1]['content'], 'partial')
        self.controller = ModelConversationController(self.storage)
        self.controller.load(snapshot)
        with patch.object(ollama, 'chat', side_effect=lambda **kw: iter([dict(message={'content': 'answer'}, done=True)])) as api:
            self.controller.resume()
            pump_until(lambda: not self.controller.busy)
            self.assertEqual(api.call_count, 2)
        self.assertEqual(self.snapshot()['run']['status'], 'complete')

    def test_deleted_run_is_never_recreated_by_completion(self):
        self.controller.started = lambda *a: self.storage.db.delete_chat(self.controller.id)
        self.run_to_end(1)
        self.assertIsNone(self.snapshot())

    def test_upgrade_from_version_11_preserves_existing_chat(self):
        from src.database import DatabaseManager
        chat = self.storage.create_chat()
        pump_until(lambda: self.storage.writer.idle)
        self.storage.db.append_messages(chat['id'], [dict(uid='existing', role='user', content='retained')], 0)
        with self.storage.db._get_conn() as conn:
            conn.execute('DROP TABLE model_conversation_attempts')
            conn.execute('DROP TABLE model_conversation_runs')
            conn.execute('PRAGMA user_version=11')
            conn.commit()
        upgraded = DatabaseManager(self.storage.db_path)
        self.assertEqual(upgraded.get_messages(chat['id'])[0]['content'], 'retained')
        with upgraded._get_conn() as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 12)


@unittest.skipUnless(Gdk.Display.get_default(), 'GTK tests require a display')
class ConversationUITests(unittest.TestCase):
    setUp = test_ui.UITests.setUp
    tearDown = test_ui.UITests.tearDown
    make_window = test_ui.UITests.make_window

    def make_conversation(self):
        window, _ = self.make_window()
        tab = window.new_model_conversation_tab()
        self.tabs.append(tab)
        pump_until(lambda: all(p.input.get_selected_model() and not p.input.capabilities_loading for p in tab.targets))
        return window, tab

    def test_setup_roundtrip_run_history_and_reopen_without_requests(self):
        from src.widgets.model_conversation_view import ModelConversationTab
        window, tab = self.make_conversation()
        tab.targets[0].system_entry.restore_draft('A system\nline two')
        tab.targets[1].system_entry.restore_draft('B system')
        tab.chat_input.entry.restore_draft('Discuss architecture')
        tab.rounds.set_value(1)
        tab.draft.flush()
        pump_until(lambda: self.storage.writer.idle)
        draft = self.storage.get_draft(tab.draft.id)
        self.assertEqual(draft['targets'][0]['panel']['fields']['system_prompt_entry'], 'A system\nline two')
        with patch.object(ollama, 'chat', side_effect=lambda **kw: iter([dict(message={'content': 'answer'}, done=True)])) as api:
            tab.start()
            tab.start()
            pump_until(lambda: tab.request is None and tab.controller.run is not None)
            self.assertEqual(api.call_count, 2)
            self.assertEqual(len(tab.bubbles), 2)
            self.assertEqual([b.full_text for b in tab.bubbles.values()], ['answer', 'answer'])
            pump_until(lambda: tab.controller.id in window.chat_rows)
            self.assertEqual(window.chat_rows[tab.controller.id].get_section(), window.recent_model_conversations_section)
            snapshot = self.storage.conversation_page(tab.controller.id)
            reopened = ModelConversationTab(self.storage, saved=snapshot)
            self.tabs.append(reopened)
            self.assertEqual(len(reopened.targets), 0)
            self.assertEqual(len(reopened.bubbles), 2)
            self.assertEqual(api.call_count, 2)
            self.assertTrue(reopened.summary.get_visible())
        tab.run_again()
        clone = window.tab_view.get_selected_page().get_child()
        self.tabs.append(clone)
        self.assertEqual(clone.targets[1].system_entry.read_draft(), 'B system')
        self.assertEqual(clone.rounds.get_value_as_int(), 1)

    def test_close_saves_draft_and_reopens_participant_configuration(self):
        from src.widgets.model_conversation_view import ModelConversationTab
        window, tab = self.make_conversation()
        tab.chat_input.entry.restore_draft('seed')
        tab.targets[1].system_entry.restore_draft('second role')
        tab.targets[0].panel.temperature_entry.set_text('0.2')
        done = []
        tab.close_session(lambda: done.append(True))
        pump_until(lambda: bool(done))
        draft = self.storage.get_draft(tab.draft.id)
        restored = ModelConversationTab(self.storage, draft=draft)
        self.tabs.append(restored)
        self.assertEqual(restored.targets[1].system_entry.read_draft(), 'second role')
        self.assertEqual(restored.targets[0].panel.temperature_entry.get_text(), '0.2')
        self.assertTrue(restored.targets[0].system_entry.get_visible())

    def test_completion_preserves_text_when_early_ui_delivery_is_missed(self):
        window, tab = self.make_conversation()
        tab.chat_input.entry.restore_draft('seed')
        tab.rounds.set_value(1)
        tab.controller.chunk = lambda *args: None
        with patch.object(ollama, 'chat', side_effect=lambda **kw: iter([
                dict(message={'content': 'complete answer', 'thinking': 'private thought'}, done=True)])):
            tab.start()
            pump_until(lambda: tab.request is None and len(tab.bubbles) == 2)
        self.assertEqual([b.full_text for b in tab.bubbles.values()], ['complete answer'] * 2)
        self.assertEqual([b.thinking_text for b in tab.bubbles.values()], ['private thought'] * 2)

    def test_long_transcript_is_paged_and_search_can_jump_to_earlier_turn(self):
        from src.widgets.model_conversation_view import ModelConversationTab
        window, tab = self.make_conversation()
        tab.chat_input.entry.restore_draft('seed')
        tab.rounds.set_value(30)
        with patch.object(ollama, 'chat', side_effect=lambda **kw: iter([dict(message={'content': 'answer'}, done=True)])) as api:
            tab.start()
            pump_until(lambda: tab.request is None and tab.controller.run is not None, timeout=15)
            self.assertEqual(api.call_count, 60)
        page = self.storage.conversation_page(tab.controller.id)
        self.assertEqual(len(page['messages']), 50)
        self.assertEqual(page['message_offset'], 11)
        saved = ModelConversationTab(self.storage, saved=page)
        self.tabs.append(saved)
        self.assertEqual(len(saved.bubbles), 50)
        first = page['attempts'][0]['id']
        saved.show_message(first)
        pump_until(lambda: first in saved.bubbles)
        self.assertEqual(saved._offset, 0)
        self.assertLessEqual(len(saved.bubbles), 50)

    def test_layout_resizes_and_long_settings_stay_scrollable(self):
        from test_response_layout import ResponseLayoutTests
        window, tab = self.make_conversation()
        resize = lambda width: ResponseLayoutTests.resize(self, window, width)
        resize(1050)
        pump_until(lambda: tab._wide and tab.targets[0].get_width() > 300)
        self.assertAlmostEqual(tab.targets[0].get_width(), tab.targets[1].get_width(), delta=2)
        resize(360)
        pump_until(lambda: not tab._wide and tab.targets[1].system_entry.get_width() > 200)
        tab.targets[0].system_entry.restore_draft('Long instructions ' * 300)
        tab.chat_input.entry.restore_draft('seed')
        tab.rounds.set_value(1)
        with patch.object(ollama, 'chat', side_effect=lambda **kw: iter([dict(message={'content': '```python\nresult = compute(value)\n```'}, done=True)])):
            tab.start()
            pump_until(lambda: tab.request is None and len(tab.bubbles) == 2)
        bubble = next(iter(tab.bubbles.values()))
        tab.summary.set_expanded(True)
        bubble.api_expander.set_expanded(True)
        pump_until(lambda: tab.summary.get_child().get_mapped() and bubble.bubble_box.get_width() > 200)
        self.assertLessEqual(tab.summary.get_child().get_height(), 310)
        self.assertLessEqual(window.get_width(), 360)
        narrow = bubble.bubble_box.get_width()
        resize(1050)
        pump_until(lambda: bubble.bubble_box.get_width() > narrow + 400)
        horizontal = tab.message_list.scrolled.get_hadjustment()
        self.assertLessEqual(horizontal.get_upper(), horizontal.get_page_size() + 1)
