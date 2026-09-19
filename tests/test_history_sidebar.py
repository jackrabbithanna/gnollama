"""Every history category remains reachable, searchable, and independently sized."""
import tempfile
import threading
import unittest
from unittest.mock import patch
from gi.repository import Gtk, Gdk
from src.storage import ChatStorage
from src.history import CATEGORIES
from src.widgets.history_sidebar import ICONS
import test_ui
from test_ui import pump_until


def seed(storage, count=9):
    with storage.db._get_conn() as conn:
        for key in CATEGORIES[1:]:
            for index in range(count):
                id = f'{key}-{index}'
                title = ('Solar ' if index < 7 else 'Lunar ') + id
                conn.execute('''INSERT INTO chats(id,title,created_at,updated_at,kind,options,is_pinned)
                    VALUES (?,?,?,?,?,'{}',?)''', (id, title, index, index + (10000 if key == 'chat' else 0),
                    'chat' if key == 'pinned' else key, key == 'pinned'))
        conn.commit()
    for index in range(count):
        storage.db.save_draft(dict(id=f'draft-{index}', mode='chat', text=('Solar ' if index < 7 else 'Lunar ') + str(index),
                                  settings={}, targets=[], revision=1))


class SidebarStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.storage = ChatStorage(self.temp.name)
        seed(self.storage)

    def tearDown(self):
        self.storage.writer.flush().result(4)
        self.storage.writer.shutdown()
        self.storage.services.shutdown()
        self.storage.knowledge.shutdown()
        self.temp.cleanup()

    def test_each_category_gets_its_own_limit_and_full_expansion(self):
        collapsed = self.storage.sidebar_snapshot()['groups']
        self.assertEqual({key: len(rows) for key, rows in collapsed.items()}, dict.fromkeys(CATEGORIES, 6))
        expanded = self.storage.sidebar_snapshot(expanded={'comparison'})['groups']
        self.assertEqual(len(expanded['comparison']), 9)
        self.assertEqual(len(expanded['chat']), 6)
        self.assertEqual([r['id'] for r in expanded['comparison']], [f'comparison-{i}' for i in range(8, -1, -1)])
        self.assertNotIn('options', expanded['comparison'][0])

    def test_filtering_precedes_limits_and_includes_every_category(self):
        result = self.storage.sidebar_snapshot('solar')['groups']
        self.assertEqual(set(result), set(CATEGORIES))
        self.assertTrue(all(len(rows) == 6 for rows in result.values()))
        self.assertTrue(all('Solar' in r['title'] for rows in result.values() for r in rows))
        result = self.storage.sidebar_snapshot('lunar')['groups']
        self.assertTrue(all(len(rows) == 2 for rows in result.values()))

    def test_searches_message_bodies_and_full_draft_text_with_literal_prefixes(self):
        self.storage.db.append_messages('comparison-0', [dict(uid='body-match', role='assistant', content='Distinctive aardvark result')], 0)
        self.storage.db.save_draft(dict(id='long', mode='chat', text='Opening text ' * 40 + 'AARDVARK', settings={}, targets=[], revision=1))
        result = self.storage.sidebar_snapshot('aardv')['groups']
        self.assertEqual(result['comparison'][0]['match_uid'], 'body-match')
        self.assertIn('aardvark', result['comparison'][0]['snippet'])
        self.assertEqual([r['id'] for r in result['drafts']], ['long'])
        self.assertFalse(any(self.storage.sidebar_snapshot('"aardv" OR --')['groups'].values()))
        self.assertFalse(any(self.storage.sidebar_snapshot('%')['groups'].values()))

    def test_completions_cover_hidden_titles_without_loading_full_messages(self):
        self.storage.db.update_chat_title('comparison-0', 'Unique autocomplete title', 0)
        self.storage.db.update_chat_title('chat-0', 'Unique autocomplete title', 0)
        result = self.storage.sidebar_snapshot('uniq')
        self.assertEqual([r['title'] for r in result['suggestions']], ['Unique autocomplete title'])
        self.assertLessEqual(len(self.storage.sidebar_snapshot('solar')['suggestions']), 8)
        self.assertEqual(self.storage.sidebar_snapshot('')['suggestions'], [])

    def test_draft_search_is_case_and_accent_insensitive(self):
        self.storage.db.save_draft(dict(id='accent', mode='chat', text='Café Überprüfung', settings={}, targets=[], revision=1))
        self.assertEqual([r['id'] for r in self.storage.sidebar_snapshot('CAFE uber')['groups']['drafts']], ['accent'])


@unittest.skipUnless(Gdk.Display.get_default(), 'GTK tests require a display')
class SidebarUITests(unittest.TestCase):
    setUp = test_ui.UITests.setUp
    tearDown = test_ui.UITests.tearDown
    make_window = test_ui.UITests.make_window

    def sidebar(self, count=9):
        window, first = self.make_window()
        pump_until(lambda: self.storage.writer.idle and not window.history.pending)
        seed(self.storage, count)
        window.load_history_sidebar()
        pump_until(lambda: not window.history.pending)
        return window, first

    def rows(self, window, key):
        source = window.draft_rows if key == 'drafts' else window.chat_rows
        return sorted((r for r in source.values() if r.get_section() == window.history.sections[key]),
                      key=lambda r: r.get_section_index())

    def query(self, window, text):
        window.history_search.set_text(text)
        pump_until(lambda: not window.history.pending and window.history._suggestion_query == text)

    def test_five_rows_and_footer_in_each_section(self):
        window, _ = self.sidebar()
        for key in CATEGORIES:
            self.assertEqual(len(self.rows(window, key)), 5)
            footer = window.history.footers[key]
            self.assertEqual(footer.get_title(), 'Show more')
            self.assertEqual(footer.get_section_index(), 5)
            window.history_sidebar.emit('setup-menu', footer)
            self.assertEqual(window.history_sidebar.get_menu_model().get_n_items(), 0)

    def test_show_more_reveals_full_list_beyond_old_global_page_and_show_less_returns_five(self):
        window, _ = self.sidebar(count=121)
        footer = window.history.footers['comparison']
        window.history_sidebar.emit('activated', footer.get_index())
        pump_until(lambda: not window.history.pending)
        self.assertEqual(len(self.rows(window, 'comparison')), 121)
        self.assertEqual(footer.get_title(), 'Show less')
        self.assertEqual(footer.get_section_index(), 121)
        self.assertEqual(len(self.rows(window, 'chat')), 5)
        window.history_sidebar.emit('activated', footer.get_index())
        pump_until(lambda: not window.history.pending)
        self.assertEqual(len(self.rows(window, 'comparison')), 5)
        self.assertEqual(footer.get_section_index(), 5)

    def test_live_filter_preserves_categories_and_restores_browse_expansion(self):
        window, _ = self.sidebar()
        window.history.toggle('comparison')
        pump_until(lambda: not window.history.pending)
        self.query(window, 'solar')
        for key in CATEGORIES:
            self.assertEqual(len(self.rows(window, key)), 5)
            self.assertTrue(all('Solar' in r.get_title() for r in self.rows(window, key)))
        window.history.toggle('drafts')
        pump_until(lambda: not window.history.pending)
        self.assertEqual(len(self.rows(window, 'drafts')), 7)
        self.query(window, 'lunar')
        self.assertTrue(all(len(self.rows(window, key)) == 2 for key in CATEGORIES))
        self.assertTrue(all(footer.get_section() is None for footer in window.history.footers.values()))
        self.query(window, '')
        self.assertEqual(len(self.rows(window, 'comparison')), 9)
        self.assertEqual(len(self.rows(window, 'drafts')), 5)

    def test_empty_results_and_stale_search_delivery(self):
        window, _ = self.sidebar()
        original = self.storage.sidebar_snapshot
        entered, release = threading.Event(), threading.Event()
        def delayed(query='', expanded=()):
            result = original(query, expanded)
            if query == 'solar':
                entered.set()
                release.wait(3)
            return result
        with patch.object(self.storage, 'sidebar_snapshot', side_effect=delayed):
            window.history_search.set_text('solar')
            pump_until(entered.is_set)
            self.query(window, 'lunar')
            release.set()
            pump_until(lambda: self.storage.services.idle)
            self.assertTrue(all('Lunar' in r.get_title() for r in window.chat_rows.values()))
        self.query(window, 'nonexistent keyword')
        self.assertEqual(window.chat_rows, {})
        self.assertEqual(window.draft_rows, {})
        self.assertEqual(window.history.placeholder.get_title(), 'No matching conversations')

    def test_keyboard_completion_fills_filter_without_opening_conversations(self):
        window, _ = self.sidebar()
        window.present()
        window.split_view.set_show_sidebar(True)
        pump_until(lambda: window.history_search.get_mapped())
        window.history_search.grab_focus()
        self.query(window, 'solar')
        pump_until(lambda: window.history.popover.get_visible())
        count = window.tab_view.get_n_pages()
        history = window.history
        self.assertTrue(history._key_pressed(None, Gdk.KEY_Down, 0, Gdk.ModifierType(0)))
        title = history.suggestion_list.get_selected_row().title
        self.assertTrue(history._key_pressed(None, Gdk.KEY_Return, 0, Gdk.ModifierType(0)))
        pump_until(lambda: not history.pending)
        self.assertEqual(window.history_search.get_text(), title)
        self.assertEqual(window.tab_view.get_n_pages(), count)
        self.assertFalse(history.popover.get_visible())

    def test_rapid_changes_coalesce_reads_and_only_apply_the_latest_filter(self):
        window, _ = self.sidebar()
        jobs = []
        with patch.object(self.storage.services.control, 'submit', side_effect=jobs.append):
            for query in ('solar', 'lunar', 'pinned', 'chat', 'comparison'):
                window.history_search.set_text(query)
                window.history.reload()
            self.assertEqual(len(jobs), 2)
            jobs[0]()
            pump_until(lambda: len(jobs) == 3)
            jobs[1]()
            jobs[2]()
            pump_until(lambda: not window.history.pending)
        self.assertEqual(len(jobs), 3)
        self.assertEqual(window.history._inflight, 0)
        self.assertEqual(len(window.chat_rows), 5)
        self.assertTrue(all(row.get_section() == window.recent_comparisons_section for row in window.chat_rows.values()))

    def test_pin_moves_rows_and_preserves_category_limits_and_menus(self):
        window, _ = self.sidebar()
        item = self.rows(window, 'comparison')[0]
        window.pin_chat(item.chat_id)
        pump_until(lambda: self.storage.writer.idle and item.get_section() == window.pinned_section and not window.history.pending)
        self.assertEqual(item.get_icon_name(), ICONS['pinned'])
        self.assertEqual(len(self.rows(window, 'comparison')), 5)
        self.assertEqual(len(self.rows(window, 'pinned')), 5)
        window.history_sidebar.emit('setup-menu', item)
        self.assertEqual(window.history_sidebar.get_menu_model().get_item_attribute_value(0, 'label', None).get_string(), 'Unpin Chat')
        window.pin_chat(item.chat_id)
        pump_until(lambda: self.storage.writer.idle and item.get_section() == window.recent_comparisons_section and not window.history.pending)
        self.assertEqual(item.get_icon_name(), ICONS['comparison'])

    def test_each_category_has_a_unique_bundled_icon(self):
        window, _ = self.sidebar()
        self.assertEqual(len(set(ICONS.values())), 5)
        theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
        for key, name in ICONS.items():
            self.assertEqual(self.rows(window, key)[0].get_icon_name(), name)
            for direction in (Gtk.TextDirection.LTR, Gtk.TextDirection.RTL):
                icon = theme.lookup_icon(name, None, 16, 1, direction, Gtk.IconLookupFlags.FORCE_SYMBOLIC)
                self.assertIn('/io/github/jackrabbithanna/Gnollama/icons/', icon.get_file().get_uri())
