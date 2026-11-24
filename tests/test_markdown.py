"""Markdown text blocks rendered as Pango markup."""
import unittest

from gi.repository import Adw, Gdk, Gtk
from src.markdown_view import MarkdownView, TableBlock, table_data
from test_ui import pump_until


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


def blocks(text):
    class Parser:
        _parse_blocks = MarkdownView._parse_blocks
    return [(block['type'], block['content']) for block in Parser()._parse_blocks(text)]


TABLE = '| Name | Qty |\n|:--|--:|\n| **a** | 1 |\n| b | 2 |'


class TableParsingTests(unittest.TestCase):
    def test_table_is_split_from_surrounding_text(self):
        self.assertEqual(blocks('Intro\n\n' + TABLE + '\n\nAfter'),
                         [('text', 'Intro\n'), ('table', TABLE), ('text', '\nAfter')])

    def test_table_directly_after_a_paragraph_line_and_without_outer_pipes(self):
        self.assertEqual(blocks('Intro:\n' + TABLE), [('text', 'Intro:'), ('table', TABLE)])
        self.assertEqual(blocks('a | b\n--|--\n1 | 2'), [('table', 'a | b\n--|--\n1 | 2')])

    def test_pipes_outside_tables_stay_text(self):
        for text in ('a | b', 'a | b\n---', 'Heading\n---\nbody | text', '> | a |\n> |---|', '    | a |\n    |---|'):
            self.assertEqual(blocks(text), [('text', text)])

    def test_table_inside_a_code_fence_stays_code(self):
        self.assertEqual(blocks('```\n' + TABLE + '\n```'), [('code', TABLE)])

    def test_table_ends_at_a_code_fence(self):
        self.assertEqual(blocks(TABLE + '\n```\nx | y\n```'), [('table', TABLE), ('code', 'x | y')])

    def test_table_data_keeps_alignment_and_inline_markup(self):
        self.assertEqual(table_data(TABLE), {'header': ['Name', 'Qty'], 'align': [0.0, 1.0],
                                             'rows': [['<b>a</b>', '1'], ['b', '2']]})

    def test_table_data_handles_short_rows_escaped_pipes_and_no_rows(self):
        self.assertEqual(table_data('| a | b |\n|---|:-:|\n| x \\| y |')['rows'], [['x | y', '']])
        self.assertEqual(table_data('| a | b |\n|---|---|'), {'header': ['a', 'b'], 'align': [0.0, 0.0], 'rows': []})

    def test_table_data_rejects_text_that_is_not_one_table(self):
        self.assertIsNone(table_data('just text'))


@unittest.skipUnless(Gdk.Display.get_default(), 'GTK tests require a display')
class TableWidgetTests(unittest.TestCase):
    def children(self, view):
        found, child = [], view.get_first_child()
        while child:
            found.append(child)
            child = child.get_next_sibling()
        return found

    def test_table_is_a_column_view_between_text_labels(self):
        view = MarkdownView('Intro\n\n' + TABLE + '\n\nAfter')
        before, table, after = self.children(view)
        self.assertIsInstance(before, Gtk.Label)
        self.assertIsInstance(after, Gtk.Label)
        self.assertIsInstance(table, TableBlock)
        self.assertEqual([column.get_title() for column in table.view.get_columns()], ['Name', 'Qty'])
        self.assertEqual([table.store.get_item(i).cells for i in range(2)], [['<b>a</b>', '1'], ['b', '2']])
        self.assertEqual(table.raw, TABLE)

    def test_column_titles_are_plain_text(self):
        table = self.children(MarkdownView('| **Name** | A &amp; B |\n|---|---|\n| 1 | 2 |'))[0]
        self.assertEqual([column.get_title() for column in table.view.get_columns()], ['Name', 'A & B'])

    def test_streamed_rows_extend_the_same_table(self):
        view = MarkdownView('| Name | Qty |\n|:--|--:|')
        table = self.children(view)[0]
        self.assertEqual(table.store.get_n_items(), 0)
        view.update(TABLE)
        first = table.store.get_item(0)
        view.update(TABLE + '\n| c | 3 |')
        self.assertEqual(self.children(view), [table])
        self.assertIs(table.store.get_item(0), first)
        self.assertEqual(table.store.get_item(2).cells, ['c', '3'])
        self.assertEqual(table.raw, TABLE + '\n| c | 3 |')

    def test_changed_header_rebuilds_the_table(self):
        view = MarkdownView(TABLE)
        table = self.children(view)[0]
        view.update(TABLE.replace('Qty', 'Count'))
        rebuilt = self.children(view)[0]
        self.assertIsNot(rebuilt, table)
        self.assertEqual(rebuilt.view.get_columns().get_item(1).get_title(), 'Count')

    def test_header_line_is_text_until_the_delimiter_arrives(self):
        view = MarkdownView('| Name | Qty |')
        self.assertIsInstance(self.children(view)[0], Gtk.Label)
        view.update('| Name | Qty |\n|:--|--:|')
        self.assertIsInstance(self.children(view)[0], TableBlock)

    def test_table_shows_every_row_at_wide_and_narrow_widths(self):
        long = 'A long description that has to wrap over several lines in a narrow table. ' * 2
        rows = ''.join(f'| item {i} | {long if i % 3 == 0 else "short"} | {i} |\n' for i in range(12))
        view = MarkdownView('| Name | Description | Value |\n|---|---|--:|\n' + rows)
        table = self.children(view)[0]
        clamp = Adw.Clamp(child=view, maximum_size=900, tightening_threshold=900)
        window = Gtk.Window(child=Gtk.ScrolledWindow(child=clamp), default_width=1000, default_height=600)
        self.addCleanup(window.destroy)
        window.present()
        adjustment = table.scrolled.get_vadjustment()

        def settled():
            list_view = table.view.get_last_child()
            row, count, bottom = list_view.get_first_child(), 0, 0
            while row:
                ok, bounds = row.compute_bounds(table.scrolled)
                count, bottom = count + 1, max(bottom, bounds.get_y() + bounds.get_height())
                row = row.get_next_sibling()
            # No hidden rows and no empty space under the last one.
            return (count == 12 and adjustment.get_upper() <= adjustment.get_page_size() + 1
                    and abs(table.scrolled.get_height() - bottom) <= 1)

        pump_until(settled)
        wide = table.scrolled.get_height()
        for width in (320, 900):
            clamp.set_maximum_size(width)
            clamp.set_tightening_threshold(width)
            pump_until(lambda: abs(table.scrolled.get_width() - width) <= 4 and settled())
        self.assertEqual(table.scrolled.get_height(), wide)
