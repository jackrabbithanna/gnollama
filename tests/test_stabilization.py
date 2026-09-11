"""Behavioral regressions discovered during the stabilization review."""
import tempfile
import unittest
from unittest.mock import patch

from gi.repository import Gdk, Gtk, GLib
from src.database import DatabaseManager
from src.markdown_view import MarkdownView
from src.widgets.message_list import MessageList
from src.bubbles import AiBubble
import test_ui
from test_ui import pump_until


class CleanupTests(unittest.TestCase):
    def test_collection_only_chat_survives_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            db = DatabaseManager(directory + '/test.db')
            db.create_chat('configured', 'New Chat', 0, 0, '')
            db.save_tool_state('configured', options={'knowledge': {'selection': {}, 'collection_ids': ['manuals']}})
            db.create_chat('unused', 'New Chat', 0, 0, '')
            db.cleanup_empty_chats()
            self.assertIsNotNone(db.get_chat('configured'))
            self.assertIsNone(db.get_chat('unused'))

    def test_scoped_cleanup_leaves_other_open_empty_chats(self):
        with tempfile.TemporaryDirectory() as directory:
            db = DatabaseManager(directory + '/test.db')
            for chat_id in ('closing', 'open'):
                db.create_chat(chat_id, 'New Chat', 0, 0, '')
            db.cleanup_empty_chats('closing')
            self.assertIsNone(db.get_chat('closing'))
            self.assertIsNotNone(db.get_chat('open'))

    def test_fences_preserve_literal_content(self):
        class Parser:
            _parse_blocks = MarkdownView._parse_blocks
        parser = Parser()
        for content in ('python\nprint(42)', 'bash\necho hello', '# A literal heading'):
            blocks = parser._parse_blocks('```\n' + content + '\n```')
            self.assertEqual(blocks, [{'type': 'code', 'lang': '', 'content': content}])
        self.assertEqual(parser._parse_blocks('```markdown\n# Heading\n```'),
                         [{'type': 'code', 'lang': 'markdown', 'content': '# Heading'}])


@unittest.skipUnless(Gdk.Display.get_default(), 'GTK requires a display')
class LifecycleTests(unittest.TestCase):
    setUp = test_ui.UITests.setUp
    tearDown = test_ui.UITests.tearDown
    make_tab = test_ui.UITests.make_tab
    make_window = test_ui.UITests.make_window

    def test_collection_only_tab_survives_close(self):
        window, tab = self.make_window()
        tab.knowledge_control.load({'collection_ids': ['manuals'], 'selection': {}})
        tab._persist_tool_options()
        window.close_tab(tab)
        pump_until(lambda: window.tab_view.get_n_pages() == 0 and self.storage.writer.idle)
        self.assertIsNotNone(self.storage.get_chat(tab.strategy.chat_id))

    def test_delete_requires_confirmation(self):
        window, tab = self.make_window()
        pump_until(lambda: self.storage.writer.idle)
        with patch('src.window.Adw.AlertDialog.present') as presented:
            dialog = window.delete_chat(tab.strategy.chat_id)
        self.assertTrue(presented.called)
        self.assertIsNotNone(self.storage.get_chat(tab.strategy.chat_id))
        dialog.emit('response', 'cancel')
        self.assertIsNotNone(self.storage.get_chat(tab.strategy.chat_id))
        with patch('src.window.Adw.AlertDialog.present'):
            dialog = window.delete_chat(tab.strategy.chat_id)
        dialog.emit('response', 'delete')
        pump_until(lambda: self.storage.writer.idle)
        self.assertIsNone(self.storage.get_chat(tab.strategy.chat_id))

    def test_close_preserves_saved_metadata_and_ignores_localized_default_title(self):
        for state, keep in (('pinned', True), ('renamed', True), ('localized', False)):
            with self.subTest(state=state):
                window, tab = self.make_window()
                chat_id = tab.strategy.chat_id
                if state == 'pinned':
                    self.storage.update_chat_pinned(chat_id, True)
                elif state == 'renamed':
                    self.storage.update_title(chat_id, 'Keep this workspace')
                else:
                    tab.title = 'Nouvelle conversation'
                window.close_tab(tab)
                pump_until(lambda: window.tab_view.get_n_pages() == 0 and self.storage.writer.idle)
                self.assertEqual(self.storage.get_chat(chat_id) is not None, keep)

    def test_stream_follows_bottom_and_respects_reader(self):
        window = Gtk.Window(default_width=500, default_height=300)
        self.windows.append(window)
        messages = MessageList()
        window.set_child(messages)
        window.present()
        bubble = AiBubble(model_name='test')
        messages.add_ai_bubble(bubble)
        bubble.append_text('\n\n'.join('Paragraph ' + str(i) for i in range(30)))
        adj = messages.get_vadjustment()
        pump_until(lambda: adj.get_upper() > 600)
        pump_until(lambda: abs(adj.get_value() - (adj.get_upper() - adj.get_page_size())) < 2)
        old_upper = adj.get_upper()
        bubble.append_text('\n\n' + '\n\n'.join('More ' + str(i) for i in range(30)))
        pump_until(lambda: adj.get_upper() > old_upper + 300)
        pump_until(lambda: abs(adj.get_value() - (adj.get_upper() - adj.get_page_size())) < 2)
        adj.set_value(100)
        old_upper = adj.get_upper()
        bubble.append_text('\n\n' + '\n\n'.join('Last ' + str(i) for i in range(30)))
        pump_until(lambda: adj.get_upper() > old_upper + 300)
        self.assertAlmostEqual(adj.get_value(), 100, delta=2)
        self.assertTrue(messages.jump_button.get_visible())
        messages.jump_button.emit('clicked')
        pump_until(lambda: abs(adj.get_value() - (adj.get_upper() - adj.get_page_size())) < 2)
