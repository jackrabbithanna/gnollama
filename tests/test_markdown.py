"""Markdown text blocks rendered as Pango markup."""
import unittest

from src.markdown_view import MarkdownView


class Label:
    def set_markup(self, markup):
        self.markup = markup


def render(text):
    label = Label()
    MarkdownView._update_text_block(None, label, text)
    return label.markup


class ListTests(unittest.TestCase):
    def test_ordered_list_items_are_numbered(self):
        self.assertEqual(render('1. one\n2. two\n3. three'), '1. one\n2. two\n3. three')

    def test_ordered_list_keeps_its_starting_number(self):
        # A list resumed after a code block arrives as its own text block.
        self.assertEqual(render('3. three\n4. four'), '3. three\n4. four')

    def test_bullets_and_numbers_do_not_merge(self):
        self.assertEqual(render('- a\n- b\n\n5. five\n6. six'), '• a\n• b\n\n\n5. five\n6. six')

    def test_nested_lists_are_indented_and_numbered_separately(self):
        text = '1. one\n    - sub a\n    - sub b\n2. two\n    1. inner\n    2. inner2\n3. three'
        self.assertEqual(render(text), '1. one\n    • sub a\n    • sub b\n2. two\n'
                                       '    1. inner\n    2. inner2\n3. three')

    def test_loose_items_keep_marker_and_text_on_one_line(self):
        self.assertEqual(render('1. one\n\n    more\n\n2. two'), '1. one\n    more\n2. two')

    def test_inline_formatting_inside_items(self):
        self.assertEqual(render('1. **bold** *it* `c`\n2. x'), '1. <b>bold</b> <i>it</i> <tt>c</tt>\n2. x')

    def test_text_around_a_list_stays_separated(self):
        self.assertEqual(render('Intro\n\n1. one\n\nAfter'), 'Intro\n\n\n1. one\n\n\nAfter')
