"""Responsive response allocation and separate recent history groups."""
import json
import unittest
from gi.repository import Gdk, Gtk
from src.bubbles import AiBubble
from src.widgets.comparison_view import ComparisonTab
import test_ui
from test_ui import pump_until


@unittest.skipUnless(Gdk.Display.get_default(), 'GTK tests require a display')
class ResponseLayoutTests(unittest.TestCase):
    setUp = test_ui.UITests.setUp
    tearDown = test_ui.UITests.tearDown
    make_window = test_ui.UITests.make_window

    def resize(self, window, width):
        window.set_default_size(width, 900)
        window.split_view.set_show_sidebar(False)
        window.present()
        pump_until(lambda: width - 20 <= window.get_width() <= width)
        # Crossing the sidebar breakpoint may reveal it during allocation.
        window.split_view.set_show_sidebar(False)

    def test_chat_code_and_api_details_use_available_width_and_resize(self):
        window, tab = self.make_window()
        bubble = AiBubble(model_name='test')
        code = 'result = combine(' + ', '.join('argument_' + str(i) for i in range(6)) + ')'
        bubble.append_text('```python\n' + code + '\n```')
        tab.message_list.add_ai_bubble(bubble)
        self.resize(window, 520)
        pump_until(lambda: bubble.markdown_view.get_first_child() is not None and bubble.bubble_box.get_width() > 300)
        block = bubble.markdown_view.get_first_child()
        scrolled = block._code_scrolled
        narrow = bubble.bubble_box.get_width()
        pump_until(lambda: scrolled.get_hadjustment().get_upper() > scrolled.get_hadjustment().get_page_size())
        self.resize(window, 1100)
        pump_until(lambda: bubble.bubble_box.get_width() > narrow + 400)
        pump_until(lambda: scrolled.get_hadjustment().get_upper() <= scrolled.get_hadjustment().get_page_size() + 1)
        self.assertGreater(bubble.bubble_box.get_width(), bubble.get_width() - 60)
        width = bubble.bubble_box.get_width()
        bubble.append_thinking('Thinking about the response. ' * 100)
        bubble.thinking_expander.set_expanded(True)
        pump_until(lambda: bubble.thinking_label.get_mapped())
        self.assertAlmostEqual(bubble.bubble_box.get_width(), width, delta=2)
        details = dict(model='test', prompt='A long request ' * 80, host='https://' + 'host-' * 100 + '.test')
        bubble.set_api_details(details)
        bubble.api_expander.set_expanded(True)
        api = bubble.api_markdown_view.get_first_child()
        self.resize(window, 360)
        pump_until(lambda: api._code_scrolled.get_width() > 0 and bubble.bubble_box.get_width() < 360)
        adjustment = api._code_scrolled.get_hadjustment()
        pump_until(lambda: adjustment.get_upper() <= adjustment.get_page_size() + 1)
        self.assertEqual(json.loads(api._raw_code), details)
        self.assertEqual(block._raw_code, code)
        outer = tab.message_list.scrolled.get_hadjustment()
        self.assertLessEqual(outer.get_upper(), outer.get_page_size() + 1)

    def test_comparison_responses_fill_equal_columns_and_resize(self):
        window, first = self.make_window()
        settings = dict(model='test', host='http://localhost:11434')
        code = 'result = combine(first_argument, second_argument)'
        saved = dict(id='layout', title='Compare layouts', targets=[
            dict(id=id, settings=settings, status='complete') for id in ('one', 'two')], messages=[
            dict(uid='one', content='```python\n' + code + '\n```'),
            dict(uid='two', content='A short answer.')])
        tab = window._add_tab(ComparisonTab(self.storage, saved=saved))
        self.tabs.append(tab)
        self.resize(window, 900)
        bubbles = [value[1] for value in tab.results.values()]
        pump_until(lambda: tab._wide and all(b.bubble_box.get_width() > 300 for b in bubbles))
        widths = [b.bubble_box.get_width() for b in bubbles]
        self.assertAlmostEqual(*widths, delta=2)
        for box, bubble, stop in tab.results.values():
            self.assertGreater(bubble.bubble_box.get_width(), box.get_width() - 60)
        self.resize(window, 1250)
        pump_until(lambda: all(b.bubble_box.get_width() > previous + 140 for b, previous in zip(bubbles, widths)))
        scrolled = bubbles[0].markdown_view.get_first_child()._code_scrolled
        pump_until(lambda: scrolled.get_hadjustment().get_upper() <= scrolled.get_hadjustment().get_page_size() + 1)
        self.assertAlmostEqual(bubbles[0].bubble_box.get_width(), bubbles[1].bubble_box.get_width(), delta=2)
        self.resize(window, 360)
        pump_until(lambda: not tab._wide and tab.result_selector.get_visible())
        self.assertLess(bubbles[0].bubble_box.get_width(), 360)

    def test_long_user_messages_expand_when_window_grows(self):
        window, tab = self.make_window()
        tab.message_list.add_user_message('Preserve the original text while resizing. ' * 40)
        bubble = tab.message_list.list_box.get_last_child()
        self.resize(window, 520)
        pump_until(lambda: bubble.label.get_width() > 200)
        before = bubble.label.get_width()
        self.resize(window, 1000)
        pump_until(lambda: bubble.label.get_width() > before + 300)

    def test_recent_chats_and_comparisons_stay_separate_when_pinned_and_refreshed(self):
        window, first = self.make_window()
        pump_until(lambda: self.storage.writer.idle)
        db = self.storage.db
        db.update_chat_title(first.strategy.chat_id, 'Older chat', 30)
        db.create_chat('new-chat', 'Newer chat', 50, 50, '')
        for id, stamp in [('old-comparison', 20), ('new-comparison', 40)]:
            db.create_comparison(dict(id=id, prompt=id, message_uid=id + '-prompt', settings={}, targets=[]))
            db.update_chat_title(id, id, stamp)
        window.load_history_sidebar()
        pump_until(lambda: all(id in window.chat_rows for id in ('new-chat', 'old-comparison', 'new-comparison')))
        def ids(section):
            rows = [item for item in window.chat_rows.values() if item.get_section() == section]
            return [item.chat_id for item in sorted(rows, key=lambda item: item.get_section_index())]
        self.assertEqual(ids(window.recent_chats_section), ['new-chat', first.strategy.chat_id])
        self.assertEqual(ids(window.recent_comparisons_section), ['new-comparison', 'old-comparison'])
        window.pin_chat('old-comparison')
        pump_until(lambda: window.chat_rows['old-comparison'].get_section() == window.pinned_section)
        window.pin_chat('old-comparison')
        pump_until(lambda: window.chat_rows['old-comparison'].get_section() == window.recent_comparisons_section)
        self.assertEqual(ids(window.recent_comparisons_section), ['new-comparison', 'old-comparison'])
        self.assertEqual(ids(window.recent_chats_section), ['new-chat', first.strategy.chat_id])
