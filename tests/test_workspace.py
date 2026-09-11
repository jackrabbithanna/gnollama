"""Workspace persistence, scheduling, and user workflow regressions."""
import base64
import copy
import json
import tempfile
import threading
import unittest
from unittest.mock import patch
from gi.repository import Gtk, Gdk, Gio
from src.storage import ChatStorage
from src.services import Services, ModelCatalog
from src import ollama
from src.context import estimate_context
from src.export import export_json, export_markdown
from src.comparison import ComparisonRun, prepare_run, ComparisonController
from src.widgets.composer import Composer
import test_ui
from test_ui import pump_until


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.storage = ChatStorage(self.temp.name)
        self.chat = self.storage.create_chat()
        self.storage.writer.flush().result(5)

    def tearDown(self):
        self.storage.writer.flush().result(5)
        self.storage.writer.shutdown()
        self.storage.services.shutdown()
        self.storage.knowledge.shutdown()
        self.temp.cleanup()

    def test_incremental_retry_preserves_row_and_attachment_identity(self):
        db, id = self.storage.db, self.chat['id']
        message = dict(uid='stable', role='user', content='first', images=[base64.b64encode(b'image').decode()])
        db.append_messages(id, [message], 0)
        with db._get_conn() as conn:
            row = tuple(conn.execute('SELECT * FROM message_images').fetchone())
            conn.execute("CREATE TRIGGER untouched BEFORE UPDATE ON messages WHEN old.uid='stable' BEGIN SELECT RAISE(ABORT, 'old message rewritten'); END")
            conn.commit()
        db.append_messages(id, [dict(uid='answer', role='assistant', content='second')], 1)
        db.append_messages(id, [dict(uid='answer', role='assistant', content='second')], 1)
        with db._get_conn() as conn:
            self.assertEqual(tuple(conn.execute('SELECT * FROM message_images').fetchone()), row)
        self.assertEqual(len(db.get_messages(id)), 2)

    def test_draft_bytes_revision_and_atomic_consumption(self):
        db, id = self.storage.db, self.chat['id']
        draft = dict(id=id, mode='chat', chat_id=id, revision=2, text='  code\n', settings={}, images=[base64.b64encode(b'bytes').decode()])
        db.save_draft(draft)
        db.save_draft(dict(draft, revision=1, text='stale'))
        self.assertEqual(db.get_draft(id)['text'], '  code\n')
        db.save_chat(id, [dict(uid='user', role='user', content='submitted')], start=0, draft_id=id, draft_revision=1)
        self.assertEqual(db.get_draft(id)['images'], draft['images'])
        db.save_chat(id, [], start=1, draft_id=id, draft_revision=2)
        self.assertIsNone(db.get_draft(id))
        db.delete_chat(id)
        db.save_draft(draft)
        self.assertIsNone(db.get_draft(id))

    def test_search_is_literal_prefix_and_returns_message_anchor(self):
        db, id = self.storage.db, self.chat['id']
        db.append_messages(id, [dict(uid='match', role='assistant', content='Unique aardvark language')], 0)
        result = db.list_history('"aardv" OR --')
        self.assertEqual(result, [])
        result = db.list_history('aardv')
        self.assertEqual(result[0]['id'], id)
        self.assertEqual(result[0]['match_uid'], 'match')
        self.assertIn('aardvark', result[0]['snippet'])
        db.update_message(id, 'match', dict(uid='match', role='assistant', content='updated'))
        self.assertEqual(db.list_history('aardv'), [])
        db.delete_chat(id)
        self.assertEqual(db.list_history('updated'), [])

    def test_snapshot_export_preserves_unknown_metadata_and_excludes_secrets(self):
        db, id = self.storage.db, self.chat['id']
        text = '```markdown\n# literal\n```'
        db.append_messages(id, [dict(uid='m', role='assistant', content=text,
            future_metadata={'x': 1}, response_metadata={'status': 'complete', 'credential_id': 'secret'})], 0)
        snapshot = self.storage.export_snapshot(id)
        exported = json.loads(export_json(snapshot))
        self.assertEqual(exported['version'], 1)
        self.assertEqual(exported['conversation']['messages'][0]['future_metadata'], {'x': 1})
        self.assertNotIn('secret', export_json(snapshot))
        self.assertIn(text, export_markdown(snapshot))

    def test_message_pages_are_bounded_and_locate_search_match(self):
        db, id = self.storage.db, self.chat['id']
        db.append_messages(id, [dict(uid=str(i), role='user', content=str(i)) for i in range(130)], 0)
        page = self.storage.conversation_page(id)
        self.assertEqual(len(page['messages']), 50)
        self.assertEqual(page['message_offset'], 80)
        page = self.storage.conversation_page(id, '15')
        self.assertEqual(page['messages'][10]['uid'], '15')

    def test_inference_saturation_does_not_block_control_or_transfers(self):
        services = self.storage.services
        gate = threading.Event()
        entered = [threading.Event() for _ in range(4)]
        def busy(event):
            event.set()
            gate.wait(5)
        try:
            for event in entered:
                services.inference.submit(busy, event)
            self.assertTrue(all(event.wait(2) for event in entered))
            self.assertEqual(services.control.submit(self.storage.list_history).result(1)[0]['id'], self.chat['id'])
            self.assertEqual(services.transfer.submit(lambda: 42).result(1), 42)
        finally:
            gate.set()

    def test_context_estimate_is_advisory_and_counts_unicode_and_output(self):
        settings = dict(options=dict(num_ctx=10, num_predict=4), system='')
        before = copy.deepcopy(settings)
        estimate = estimate_context([dict(role='user', content='😀' * 4)], settings)
        self.assertEqual(estimate['text_tokens'], 4)
        self.assertTrue(estimate['warning'])
        self.assertEqual(settings, before)

    def test_catalog_shares_fetch_and_cancels_subscribers_independently(self):
        catalog = self.storage.services.catalog
        started, release = threading.Event(), threading.Event()
        first, second = Gio.Cancellable(), Gio.Cancellable()
        def fetch(*args, **kwargs):
            started.set()
            release.wait(3)
            self.assertFalse(kwargs['cancellable'].is_cancelled())
            return [{'name': 'model', 'digest': 'one'}]
        with patch.object(ollama, 'fetch_models', side_effect=fetch) as tags, patch.object(ollama, 'show_model', return_value={'capabilities': ['completion']}) as show:
            a = self.storage.services.control.submit(catalog.models, 'http://localhost:11434', first)
            self.assertTrue(started.wait(1))
            b = self.storage.services.control.submit(catalog.models, 'http://localhost:11434', second)
            import time
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                with catalog._condition:
                    if any(len(job['members']) == 2 for job in catalog._pending.values()):
                        break
                time.sleep(.005)
            try:
                first.cancel()
            finally:
                release.set()
            with self.assertRaises(ollama.RequestCancelled):
                a.result(2)
            self.assertEqual(b.result(2)[0][0], 'model')
            catalog.models('http://localhost:11434')
            self.assertEqual(tags.call_count, 1)
            self.assertEqual(show.call_count, 0)
            catalog.invalidate()
            catalog.models('http://localhost:11434')
            self.assertEqual(tags.call_count, 2)

    def test_catalog_abandoned_lookup_cancels_transport(self):
        started = threading.Event()
        cancel = Gio.Cancellable()
        def fetch(*args, **kwargs):
            started.set()
            import time
            deadline = time.monotonic() + 2
            while not kwargs['cancellable'].is_cancelled() and time.monotonic() < deadline:
                time.sleep(.005)
            self.assertTrue(kwargs['cancellable'].is_cancelled())
            raise ollama.RequestCancelled()
        with patch.object(ollama, 'fetch_models', side_effect=fetch):
            future = self.storage.services.control.submit(self.storage.services.catalog.models, 'http://localhost:11434', cancel)
            self.assertTrue(started.wait(1))
            cancel.cancel()
            with self.assertRaises(ollama.RequestCancelled):
                future.result(2)

    def test_comparison_resolves_sources_once_and_keeps_shared_options(self):
        from src.knowledge import DEFAULT_RAG
        host = self.storage.get_all_hosts()[0]
        settings = dict(options={'num_ctx': 2048}, system='Shared instructions', thinking=True,
            format={'type': 'object'}, keep_alive=30, logprobs=True, top_logprobs=2, show_stats=True,
            knowledge=dict(DEFAULT_RAG, enabled=True, config_id='config', host=host['hostname'],
                           model='embed', collection_ids=['source']))
        snapshot = dict(query='question', hits=[dict(title='Reference', text='A shared passage.', id='passage')])
        targets = [dict(host_id=host['id'], model=name) for name in ('one', 'two')]
        with patch.object(self.storage.services.catalog, 'models', return_value=[(name, {'capabilities': ['completion', 'vision', 'thinking']}) for name in ('one', 'two')]), patch.object(self.storage.knowledge, 'retrieve', return_value=snapshot) as retrieve:
            active = ComparisonRun()
            run = prepare_run(self.storage, active, 'question', ['aW1hZ2U='], settings, targets, None, 0)
            self.assertEqual(retrieve.call_count, 1)
            a, b = run['targets']
            self.assertEqual(a['request']['messages'], b['request']['messages'])
            self.assertEqual(a['request']['format'], {'type': 'object'})
            self.assertEqual(a['request']['options'], {'num_ctx': 2048})
            self.assertEqual(a['request']['messages'][-1]['images'], ['aW1hZ2U='])
            snapshot['hits'][0]['text'] = 'changed later'
            self.assertNotIn('changed later', json.dumps(a['request']))

    def test_comparison_stop_all_saves_stopped_outcomes(self):
        host = self.storage.get_all_hosts()[0]
        settings = dict(options={}, system=None, thinking=None, format=None, keep_alive=None,
                        logprobs=False, top_logprobs=None, show_stats=True, knowledge={})
        targets = [dict(host_id=host['id'], model=name) for name in ('one', 'two')]
        with patch.object(self.storage.services.catalog, 'models', return_value=[(name, None) for name in ('one', 'two')]):
            active = ComparisonRun()
            run = prepare_run(self.storage, active, 'question', [], settings, targets, None, 0)
        self.storage.db.create_comparison(run)
        active.cancellable.cancel()
        with patch.object(ollama, 'chat') as api:
            ComparisonController(self.storage, active).dispatch(lambda *args: None, lambda *args: None)
            pump_until(lambda: not active.pending and self.storage.writer.idle)
            self.assertFalse(api.called)
        saved = self.storage.export_snapshot(active.id)
        self.assertEqual([target['status'] for target in saved['targets']], ['stopped', 'stopped'])


@unittest.skipUnless(Gdk.Display.get_default(), 'GTK tests require a display')
class WorkflowTests(unittest.TestCase):
    setUp = test_ui.UITests.setUp
    tearDown = test_ui.UITests.tearDown
    make_tab = test_ui.UITests.make_tab
    make_window = test_ui.UITests.make_window

    def test_composer_preserves_whitespace_and_ime_preedit(self):
        composer = Composer()
        sent = []
        composer.connect('activate', lambda *args: sent.append(composer.read_draft()))
        composer.restore_draft('  code\n  next\n')
        self.assertFalse(composer._key_pressed(None, Gdk.KEY_Return, 0, Gdk.ModifierType.SHIFT_MASK))
        composer._preedit = True
        self.assertFalse(composer._key_pressed(None, Gdk.KEY_Return, 0, 0))
        self.assertEqual(sent, [])
        composer._preedit = False
        self.assertTrue(composer._key_pressed(None, Gdk.KEY_Return, 0, 0))
        self.assertEqual(sent, ['  code\n  next\n'])

    def test_composer_text_uses_available_width(self):
        window, tab = self.make_window()
        window.set_default_size(360, 800)
        window.present()
        tab.chat_input.entry.restore_draft('A visible multiline draft\nwith another line')
        pump_until(lambda: tab.chat_input.entry.view.get_width() > 180)
        self.assertLessEqual(tab.chat_input.entry.scrolled.get_height(), 184)
        self.assertFalse(tab.advanced.get_expanded())

    def test_draft_restores_multiline_text_and_raw_settings(self):
        tab = self.make_tab()
        tab.chat_input.entry.restore_draft('  draft\n  more')
        tab.options_panel.num_ctx_entry.set_text('unfinished')
        tab.draft.flush()
        pump_until(lambda: self.storage.writer.idle)
        saved = self.storage.get_draft(tab.draft.id)
        tab.chat_input.entry.clear_draft()
        tab.restore_draft(saved)
        self.assertEqual(tab.chat_input.entry.read_draft(), '  draft\n  more')
        self.assertEqual(tab.options_panel.num_ctx_entry.get_text(), 'unfinished')

    def draft_menu(self, window, draft_id):
        window.load_history_sidebar()
        pump_until(lambda: draft_id in window.draft_rows)
        window.history_sidebar.emit('setup-menu', window.draft_rows[draft_id])
        menu = window.history_sidebar.get_menu_model()
        entries = {}
        for index in range(menu.get_n_items()):
            label = menu.get_item_attribute_value(index, 'label', None).get_string()
            self.assertTrue(label.strip())
            action = menu.get_item_attribute_value(index, 'action', None).get_string()
            target = menu.get_item_attribute_value(index, 'target', None)
            self.assertEqual(target.get_string(), draft_id)
            entries[action] = target
        self.assertEqual(set(entries), {'win.draft_open', 'win.draft_discard'})
        return entries

    def test_draft_sidebar_icon_loads_from_bundled_resources(self):
        window, tab = self.make_window()
        tab._configured = True
        tab.draft.changed()
        tab.draft.flush()
        pump_until(lambda: self.storage.writer.idle)
        self.draft_menu(window, tab.draft.id)
        item = window.draft_rows[tab.draft.id]
        self.assertEqual(item.get_title(), 'Draft')
        theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
        for direction in (Gtk.TextDirection.LTR, Gtk.TextDirection.RTL):
            icon = theme.lookup_icon(item.get_icon_name(), None, 16, 1, direction, Gtk.IconLookupFlags.FORCE_SYMBOLIC)
            self.assertEqual(icon.get_file().get_uri(),
                'resource:///io/github/jackrabbithanna/Gnollama/icons/scalable/actions/gnollama-draft-symbolic.svg')

    def test_draft_menu_preserves_live_edits_and_discards_only_selected_draft(self):
        window, original = self.make_window()
        chat_id = original.strategy.chat_id
        self.storage.save_chat(chat_id, [dict(role='user', content='Keep this conversation')])
        pump_until(lambda: self.storage.writer.idle)
        for create in (window.new_chat_tab, window.new_tab, window.new_comparison_tab):
            with self.subTest(mode=create.__name__):
                tab = create()
                self.tabs.append(tab)
                pump_until(lambda: self.storage.services.idle)
                tab.chat_input.entry.restore_draft('Saved draft')
                tab.draft.flush()
                pump_until(lambda: self.storage.writer.idle)
                window.history_sidebar.emit('setup-menu', window.chat_rows[chat_id])
                entries = self.draft_menu(window, tab.draft.id)
                tab.chat_input.entry.restore_draft('Latest unsaved edits')
                window.lookup_action('draft_open').activate(entries['win.draft_open'])
                self.assertIs(window.tab_view.get_selected_page().get_child(), tab)
                self.assertEqual(tab.chat_input.entry.read_draft(), 'Latest unsaved edits')
                window.lookup_action('draft_discard').activate(entries['win.draft_discard'])
                pump_until(lambda: self.storage.writer.idle and tab.draft.id not in window.draft_rows)
                self.assertIsNone(self.storage.get_draft(tab.draft.id))
                self.assertEqual(tab.chat_input.entry.read_draft(), '')
                self.assertFalse(tab.draft.dirty)
                self.assertEqual(self.storage.get_chat(chat_id)['messages'][0]['content'], 'Keep this conversation')
        window.history_sidebar.emit('setup-menu', None)
        self.assertEqual(window.history_sidebar.get_menu_model().get_n_items(), 0)

    def test_draft_menu_reopens_and_discards_closed_drafts(self):
        window, original = self.make_window()
        for create in (window.new_chat_tab, window.new_tab, window.new_comparison_tab):
            with self.subTest(mode=create.__name__):
                tab = create()
                self.tabs.append(tab)
                pump_until(lambda: self.storage.services.idle)
                tab.chat_input.entry.restore_draft('Recover after closing')
                draft_id = tab.draft.id
                window.close_tab(tab)
                pump_until(lambda: tab not in window.tabs())
                entries = self.draft_menu(window, draft_id)
                window.lookup_action('draft_open').activate(entries['win.draft_open'])
                pump_until(lambda: any(t.draft.id == draft_id for t in window.tabs()))
                reopened = next(t for t in window.tabs() if t.draft.id == draft_id)
                self.tabs.append(reopened)
                self.assertEqual(reopened.chat_input.entry.read_draft(), 'Recover after closing')
                window.close_tab(reopened)
                pump_until(lambda: reopened not in window.tabs())
                entries = self.draft_menu(window, draft_id)
                window.lookup_action('draft_discard').activate(entries['win.draft_discard'])
                pump_until(lambda: self.storage.writer.idle and draft_id not in window.draft_rows)
                self.assertIsNone(self.storage.get_draft(draft_id))

    def test_close_waits_for_attachment_bytes_in_every_draft_mode(self):
        from pathlib import Path
        from gi.repository import GLib
        from src.tab import GenerationTab
        from src.widgets.comparison_view import ComparisonTab
        import builtins
        texture = Gdk.MemoryTexture.new(1, 1, Gdk.MemoryFormat.R8G8B8A8,
            GLib.Bytes.new(bytes([255, 0, 0, 255])), 4)
        raw = texture.save_to_png_bytes().get_data()
        path = Path(self.temp.name) / 'pending.png'
        path.write_bytes(raw)
        for mode in ('chat', 'generate', 'comparison'):
            with self.subTest(mode=mode):
                tab = self.make_tab() if mode == 'chat' else ComparisonTab(self.storage) if mode == 'comparison' else GenerationTab(storage=self.storage)
                if tab not in self.tabs:
                    self.tabs.append(tab)
                entered, release = threading.Event(), threading.Event()
                original_open = builtins.open
                def delayed(file, *args, **kwargs):
                    if str(file) == str(path):
                        entered.set()
                        release.wait(2)
                    return original_open(file, *args, **kwargs)
                closed = []
                with patch('builtins.open', side_effect=delayed):
                    tab.chat_input.import_images([str(path)])
                    self.assertTrue(entered.wait(1))
                    tab.chat_input.entry.restore_draft('Wait for this attachment')
                    self.assertFalse(tab.chat_input.send_button.get_sensitive())
                    (tab.send_or_stop if mode == 'comparison' else tab.on_send_clicked)()
                    self.assertIsNone(tab.request)
                    self.assertEqual(tab.chat_input.entry.read_draft(), 'Wait for this attachment')
                    tab.close_session(lambda: closed.append(True))
                    pump_until(lambda: self.storage.writer.idle)
                    self.assertEqual(closed, [])
                    release.set()
                    pump_until(lambda: bool(closed))
                saved = self.storage.get_draft(tab.draft.id)
                self.assertIsNotNone(saved)
                self.assertEqual(saved['images'], [base64.b64encode(raw).decode('ascii')])

    def test_discard_does_not_restore_a_pending_attachment(self):
        from pathlib import Path
        from gi.repository import GLib
        import builtins
        tab = self.make_tab()
        texture = Gdk.MemoryTexture.new(1, 1, Gdk.MemoryFormat.R8G8B8A8,
            GLib.Bytes.new(bytes([255, 0, 0, 255])), 4)
        path = Path(self.temp.name) / 'discard.png'
        path.write_bytes(texture.save_to_png_bytes().get_data())
        entered, release = threading.Event(), threading.Event()
        original_open = builtins.open
        def delayed(file, *args, **kwargs):
            if str(file) == str(path):
                entered.set()
                release.wait(2)
            return original_open(file, *args, **kwargs)
        with patch('builtins.open', side_effect=delayed):
            tab.chat_input.import_images([str(path)])
            self.assertTrue(entered.wait(1))
            tab.discard_draft()
            release.set()
            pump_until(lambda: tab.chat_input.pending_imports == 0 and self.storage.writer.idle)
        self.assertEqual(tab.chat_input.get_images(), [])
        self.assertIsNone(self.storage.get_draft(tab.draft.id))

    def test_comparison_mixed_results_reopen_without_requests(self):
        from src.widgets.comparison_view import ComparisonTab
        self.stack.enter_context(patch.object(ollama, 'fetch_models', return_value=['test', 'two']))
        self.storage.services.catalog.invalidate()
        window, first = self.make_window()
        tab = window.new_comparison_tab()
        self.tabs.append(tab)
        pump_until(lambda: all(p.input.get_selected_model() for p in tab.targets))
        tab.targets[1].input.select_model('two')
        pump_until(lambda: all(not p.input.capabilities_loading for p in tab.targets))
        tab.chat_input.entry.restore_draft('compare this')
        calls = []
        def chat(**kwargs):
            calls.append(kwargs)
            if kwargs['model'] == 'two':
                raise ollama.OllamaError('target failed')
            yield dict(message={'content': 'answer'}, done=True, eval_count=3)
        with patch.object(ollama, 'chat', side_effect=chat) as api:
            tab.send_or_stop()
            pump_until(lambda: tab.request is None and self.storage.writer.idle)
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0]['messages'], calls[1]['messages'])
            snapshot = self.storage.export_snapshot(tab.strategy.chat_id)
            self.assertEqual({t['status'] for t in snapshot['targets']}, {'complete', 'failed'})
            reopened = ComparisonTab(self.storage, saved=snapshot)
            self.tabs.append(reopened)
            self.assertEqual(len(reopened.results), 2)
            self.assertEqual(api.call_count, 2)

    def test_comparison_duplicate_targets_preserves_draft(self):
        from src.widgets.comparison_view import ComparisonTab
        tab = ComparisonTab(self.storage)
        self.tabs.append(tab)
        pump_until(lambda: all(p.input.get_selected_model() and not p.input.capabilities_loading for p in tab.targets))
        tab.chat_input.entry.restore_draft('keep me')
        with patch.object(ollama, 'chat') as api:
            tab.send_or_stop()
            pump_until(lambda: tab.request is None)
            self.assertEqual(tab.chat_input.entry.read_draft(), 'keep me')
            self.assertFalse(api.called)
            self.assertTrue(tab.notice.get_visible())
