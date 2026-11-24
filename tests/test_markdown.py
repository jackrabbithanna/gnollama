"""Markdown text blocks rendered as Pango markup."""
import unittest

from unittest import mock

from gi.repository import Adw, Gdk, Gtk
from src import math_render
from src.markdown_view import SEGMENT_SIZE, MarkdownView, MathBlock, TableBlock, segment_ends, table_data
from test_ui import pump_until


class Label:
    def set_markup(self, markup):
        # Inline equations as the text shown when they cannot be drawn.
        self.markup = math_render.to_text(markup)


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


class MathParsingTests(unittest.TestCase):
    def test_display_math_is_split_from_surrounding_text(self):
        self.assertEqual(blocks('Intro:\n\\[\n x = \\frac{1}{2}\n\\]\nAfter'),
                         [('text', 'Intro:\n'), ('math', 'x = \\frac{1}{2}'), ('text', '\nAfter')])
        self.assertEqual(blocks('a $$E=mc^2$$ b'), [('text', 'a '), ('math', 'E=mc^2'), ('text', ' b')])
        self.assertEqual(blocks('\\[a\\]\n\n\\[b\\]'), [('math', 'a'), ('math', 'b')])

    def test_display_math_in_code_or_still_open_stays_as_it_is(self):
        self.assertEqual(blocks('```\n\\[x\\]\n```'), [('code', '\\[x\\]')])
        for text in ('`\\[x\\]` code', 'open \\[ x = 1', 'open $$ x = 1', '$$ $$'):
            self.assertEqual(blocks(text), [('text', text)])

    def test_equations_without_delimiters_are_display_math(self):
        integral = 'f(x) = \\int_{0}^{x} f(t) \\, dt'
        self.assertEqual(blocks(integral), [('math', integral)])
        self.assertEqual(blocks('So:\n' + integral + '\nDone.'), [('text', 'So:'), ('math', integral), ('text', 'Done.')])
        aligned = '\\begin{aligned}\na &= b \\\\\nc &= \\frac{1}{2}\n\\end{aligned}'
        self.assertEqual(blocks('Then\n' + aligned), [('text', 'Then\n'), ('math', aligned)])
        for text in ('The integral \\int_0^1 x dx is simple', '1. \\frac{a}{b}', 'C:\\Users\\int\\file',
                     'a \\(\\frac{1}{2}\\) b', '| \\frac{a}{b} | c |'):
            self.assertEqual(blocks(text), [('text', text)])

    def test_inline_math_is_not_read_as_markdown(self):
        self.assertEqual(render('$a_1 * b_2$ and $c_3 * d_4$'),
                         '<i>a</i><sub>1</sub> * <i>b</i><sub>2</sub> and <i>c</i><sub>3</sub> * <i>d</i><sub>4</sub>')
        self.assertEqual(render('\\(x\\_y\\)'), '<i>x</i>_<i>y</i>')
        self.assertEqual(render('**so \\(E = mc^2\\)**'), '<b>so <i>E</i> = <i>mc</i><sup>2</sup></b>')

    def test_simple_inline_math_becomes_unicode(self):
        self.assertEqual(render('$\\frac{a+b}{2}$, $\\sqrt{x}$, $\\alpha \\leq \\beta$'),
                         '(<i>a</i>+<i>b</i>)/2, √<i>x</i>, α ≤ β')
        self.assertEqual(render('$\\sum_{i=1}^{n} x_i \\in \\mathbb{R}$'),
                         '∑<sub><i>i</i>=1</sub><sup><i>n</i></sup> <i>x</i><sub><i>i</i></sub> ∈ ℝ')
        self.assertEqual(render('$\\text{rate} = \\mathbf{v} \\cdot t - \\sin x$'),
                         'rate = <b>v</b> · <i>t</i> − sin <i>x</i>')

    def test_other_inline_math_is_shown_as_source(self):
        self.assertEqual(render('$\\begin{pmatrix}a & b\\end{pmatrix}$'),
                         '<tt>\\begin{pmatrix}a &amp; b\\end{pmatrix}</tt>')
        self.assertEqual(render('$\\frac{a}{b$'), '<tt>\\frac{a}{b</tt>')
        self.assertEqual(render('tail \\[ \\frac{a}{b'), 'tail <tt>\\[ \\frac{a}{b</tt>')

    def test_money_code_and_urls_are_not_math(self):
        self.assertEqual(render('It costs $5 and $10, or $5/$10.'), 'It costs $5 and $10, or $5/$10.')
        self.assertEqual(render('`$x_1$` code'), '<tt>$x_1$</tt> code')
        self.assertEqual(render('[l](http://a?b=$x$)'), "<a href='http://a?b=$x$'>l</a>")
        self.assertEqual(render('$5x^2$'), '5<i>x</i><sup>2</sup>')

    def test_inline_math_in_table_cells(self):
        rows = table_data('| a | b |\n|---|---|\n| $x_1$ | \\(y^2\\) |')['rows']
        self.assertEqual([[math_render.to_text(cell) for cell in row] for row in rows],
                         [['<i>x</i><sub>1</sub>', '<i>y</i><sup>2</sup>']])


@unittest.skipUnless(Gdk.Display.get_default(), 'GTK tests require a display')
class MathWidgetTests(unittest.TestCase):
    def children(self, view):
        found, child = [], view.get_first_child()
        while child:
            found.append(child)
            child = child.get_next_sibling()
        return found

    def test_streamed_equation_becomes_one_math_block(self):
        text = 'Intro:\n\\[\nx = \\frac{-b}{2a}\n\\]\nAfter'
        view = MarkdownView('')
        for end in range(1, len(text) + 1):
            view.update(text[:end])
            if end == 6:
                intro = self.children(view)[0]
        before, block, after = self.children(view)
        self.assertIs(before, intro)
        self.assertIsInstance(block, MathBlock)
        self.assertEqual(block.tex, 'x = \\frac{-b}{2a}')
        self.assertIsInstance(after, Gtk.Label)
        view.update(text + ' text')
        self.assertEqual(self.children(view)[:2], [before, block])

    def test_equation_is_drawn_at_its_own_size(self):
        try:
            import ziamath
        except ImportError:
            self.skipTest('ziamath is not installed')
        block = MathBlock('x = \\frac{-b \\pm \\sqrt{b^2 - 4ac}}{2a}')
        self.assertTrue(block.scrolled.get_visible())
        self.assertFalse(block.source.get_visible())
        window = Gtk.Window(child=block, default_width=600, default_height=200)
        self.addCleanup(window.destroy)
        window.present()
        pump_until(lambda: block.picture.get_width() > 0)
        width, height = block.picture.get_width(), block.picture.get_height()
        self.assertTrue(80 < width < 400 and 20 < height < 100, (width, height))
        block.set_tex('x')
        pump_until(lambda: 0 < block.picture.get_width() < width)

    def test_inline_equations_are_drawn_inside_the_text(self):
        try:
            import ziamath
        except ImportError:
            self.skipTest('ziamath is not installed')
        view = MarkdownView('Here \\(\\frac{dx}{dt} = kx\\) and $x_0$.\n\n'
                            '| a |\n|---|\n| $\\begin{cases} a \\\\[4pt] b \\end{cases}$ |')
        label, table = self.children(view)
        self.assertEqual(label.get_text(), 'Here ￼ and ￼.')
        self.assertEqual([index for index, image in label._equations], [5, 13])
        plain = MarkdownView('Here \\(x\\).').get_first_child()
        window = Gtk.Window(child=view, default_width=500, default_height=300)
        self.addCleanup(window.destroy)
        window.present()
        pump_until(lambda: label.get_height() > 0 and table.view.get_last_child().get_first_child())
        # The fraction makes its line taller than a line of plain text.
        self.assertGreater(label.get_height(), plain.measure(Gtk.Orientation.VERTICAL, -1)[1])
        view.update('Here plain.\n\n| a |\n|---|\n| b |')
        self.assertEqual((label.get_text(), label._equations), ('Here plain.', []))

    def test_inline_equations_fall_back_to_text(self):
        with mock.patch.object(math_render, 'render', return_value=None):
            label = MarkdownView('Here \\(x^2\\).').get_first_child()
        self.assertEqual(label.get_label(), 'Here <i>x</i><sup>2</sup>.')

    def test_equation_that_cannot_be_rendered_shows_its_source(self):
        with mock.patch.object(math_render, 'render', return_value=None):
            block = MathBlock('\\frac{a}{b}')
        self.assertFalse(block.scrolled.get_visible())
        self.assertTrue(block.source.get_visible())
        self.assertEqual(block.source.get_text(), '\\frac{a}{b}')


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


PROSE = ('A paragraph with **bold** text, `code` and inline math \\(a^2\\) that is long enough '
         'to add up over a response of several thousand characters.\n\n')
MIXED = ''.join(
    f'## Section {n}\n\n' + PROSE * 3
    + '- point one\n- point two\n\n    continued text of point two\n\n- point three\n\n'
    + f'```python\nvalue = {n}\n\n\nprint(value)  # $$ and \\[ are literal here\n```\n\n'
    + TABLE + f'\n| c | {n} |\n\n'
    + 'So:\n\n\\[\nx = \\frac{-b}{2a}\n\n+ 1\n\\]\n\n'
    + '1. step one\n2. step two\n\n> a quote\n\n> its second paragraph\n\n' + PROSE
    for n in range(12))


class SegmentTests(unittest.TestCase):
    def test_short_text_is_one_segment(self):
        self.assertEqual(segment_ends(PROSE * 3), [])

    def test_long_text_is_cut_at_paragraph_breaks(self):
        text = PROSE * 60
        ends = segment_ends(text)
        self.assertGreater(len(ends), 2)
        for start, end in zip([0] + ends, ends):
            self.assertGreaterEqual(end - start, SEGMENT_SIZE)
            self.assertLess(end - start, SEGMENT_SIZE + 2 * len(PROSE))
            self.assertTrue(text[:end].endswith('\n\n') and text[end:].startswith('A paragraph'))

    def test_cuts_are_the_same_however_much_text_follows(self):
        ends = segment_ends(MIXED)
        self.assertGreater(len(ends), 2)
        for length in range(0, len(MIXED), 97):
            for complete in (True, False):
                found = segment_ends(MIXED[:length], complete)
                self.assertEqual(found, ends[:len(found)])

    def test_no_cut_inside_code_lists_quotes_or_open_equations(self):
        long = PROSE * 15
        # These lines may continue the list, quote or indented text above the blank line.
        for line in ('- another item', '* another item', '2. another item', '    continued', '> more', ': definition'):
            self.assertNotIn(len(long), segment_ends(long + line + '\n\nend'), line)
        self.assertIn(len(long), segment_ends(long + 'A new paragraph\n\nend'))
        self.assertEqual(segment_ends('Money $$ and\n\n' + long + 'end'), [])
        code = '```\n' + long + '```\n\n'
        self.assertEqual(segment_ends(code + 'text'), [len(code)])
        self.assertEqual(segment_ends('\\[ x\n\n' + long + 'end'), [])
        # A finished equation or code block does not hold the text after it together.
        self.assertTrue(segment_ends('\\[ x \\]\n\n' + long + 'end'))
        self.assertTrue(segment_ends('```\n$$\n```\n\n' + long + 'end'))

    def test_unfinished_last_line_is_not_cut_off_while_streaming(self):
        text = PROSE * 15
        self.assertEqual(len(text), segment_ends(text + 'A')[-1])
        self.assertNotIn(len(text), segment_ends(text + 'A', complete=False))
        self.assertIn(len(text), segment_ends(text + 'A\n', complete=False))
        self.assertNotIn(len(text), segment_ends(text + '-', complete=False))
        self.assertNotIn(len(text), segment_ends(text + '- item\n', complete=False))


class StreamingTests(unittest.TestCase):
    def shown(self, view):
        found, child = [], view.get_first_child()
        while child:
            if isinstance(child, TableBlock):
                rows = [child.store.get_item(i).cells for i in range(child.store.get_n_items())]
                found.append(('table', child.header, rows, child.raw))
            elif isinstance(child, MathBlock):
                found.append(('math', child.tex))
            elif isinstance(child, Gtk.Label):
                found.append(('text', child.get_label()))
            else:
                found.append(('code', child._lang, child._raw_code))
            child = child.get_next_sibling()
        return found

    def test_streaming_in_any_chunks_matches_rendering_at_once(self):
        import random
        whole = self.shown(MarkdownView(MIXED))
        self.assertGreater(len(segment_ends(MIXED)), 2)
        for seed in range(3):
            chooser = random.Random(seed)
            view, end, step = MarkdownView(''), 0, 0
            while end < len(MIXED):
                end = min(len(MIXED), end + chooser.choice((1, 2, 5, 17, 60, 300)))
                view.update(MIXED[:end], streaming=True)
                step += 1
                if step % 25 == 0:
                    fresh = MarkdownView('')
                    fresh.update(MIXED[:end], streaming=True)
                    self.assertEqual(self.shown(view), self.shown(fresh), (seed, end))
            view.update(MIXED)
            self.assertEqual(self.shown(view), whole)

    def test_text_is_the_same_one_character_at_a_time(self):
        text = PROSE * 16 + 'Before a list:\n\n- item\n\n- item two\n\n' + PROSE + '\\[\ny\n\n= x\n\\]\n\n' + PROSE * 16
        view = MarkdownView('')
        for end in range(len(text) - 2700, len(text) + 1):
            view.update(text[:end], streaming=True)
        middle = self.shown(view)
        fresh = MarkdownView('')
        fresh.update(text, streaming=True)
        self.assertEqual(middle, self.shown(fresh))
        view.update(text)
        self.assertEqual(self.shown(view), self.shown(MarkdownView(text)))

    def test_finished_segments_are_left_alone(self):
        text = PROSE * 90
        view = MarkdownView('')
        view.update(text[:len(text) // 2], streaming=True)
        first = view.get_first_child()
        with mock.patch.object(MarkdownView, '_parse_blocks', autospec=True,
                               side_effect=MarkdownView._parse_blocks) as parse:
            for end in range(len(text) // 2, len(text), 40):
                view.update(text[:end], streaming=True)
        self.assertIs(view.get_first_child(), first)
        self.assertLess(max(len(call.args[1]) for call in parse.call_args_list), SEGMENT_SIZE + 2 * len(PROSE))
        self.assertGreater(len(self.shown(view)), 4)

    def test_text_that_is_not_a_continuation_is_rendered_again(self):
        view = MarkdownView('')
        view.update(PROSE * 40, streaming=True)
        view.update('Something else', streaming=True)
        self.assertEqual(self.shown(view), [('text', 'Something else')])

    def test_bubble_shows_each_chunk_at_once(self):
        from src.bubbles import AiBubble
        bubble = AiBubble(model_name='test')
        bubble.append_text('Hello ')
        bubble.append_text('**there**')
        self.assertEqual(self.shown(bubble.markdown_view), [('text', 'Hello <b>there</b>')])
        bubble.show_response_metadata({})
        self.assertEqual(self.shown(bubble.markdown_view), [('text', 'Hello <b>there</b>')])
