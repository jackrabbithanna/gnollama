from gettext import gettext as _
import html
import math
import re
from typing import List, Dict, Any, Optional, Tuple
from html.parser import HTMLParser
from gi.repository import Gtk, Gdk, Gio, GLib, Graphene, Gsk, Pango, GObject, Adw

from . import math_render

try:
    import markdown
except ImportError:
    markdown = None

try:
    import gi
    gi.require_version('GtkSource', '5')
    from gi.repository import GtkSource
except (ImportError, ValueError):
    GtkSource = None

try:
    import gi
    gi.require_version('Adw', '1')
    from gi.repository import Adw
except (ImportError, ValueError):
    Adw = None

class PangoMarkupParser(HTMLParser):
    """
    A simple HTML parser that converts a subset of HTML into Pango markup.
    Also handles ASCII table rendering for basic HTML tables.
    """
    LIST_INDENT = "    "

    def __init__(self, collect_tables: bool = False) -> None:
        super().__init__()
        # Keep tables as data in self.tables instead of drawing them as text.
        self.collect_tables = collect_tables
        self.tables: List[Dict[str, Any]] = []
        self.output: List[str] = []
        self.tags: List[str] = []

        # List state: one entry per open list, None for bullets or the next
        # number for ordered lists.
        self.lists: List[Optional[int]] = []
        self.item_start: bool = False  # Marker written, no item content yet

        # Table state
        self.in_table: bool = False
        self.table_rows: List[List[str]] = []
        self.current_row: List[str] = []
        self.in_cell: bool = False
        self.cell_content: str = "" # Buffer for cell content
        
    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if tag == 'table':
            self.in_table = True
            self.table_rows = []
            self.table_align: List[float] = []
            self.table_has_header = False
            self.output.append("\n") # Spacing before table
        elif tag == 'tr':
            if self.in_table:
                self.current_row = []
        elif tag in ['td', 'th']:
            if self.in_table:
                self.in_cell = True
                self.cell_content = ""
                if not self.table_rows:
                    self.table_has_header = self.table_has_header or tag == 'th'
                    style = dict(attrs).get('style') or ''
                    self.table_align.append(1.0 if 'right' in style else 0.5 if 'center' in style else 0.0)
            else:
                self.output.append(" | ")
        elif tag in ['h1', 'h2']:
            self.output.append("\n<span size='xx-large' weight='bold'>")
        elif tag == 'h3':
            self.output.append("\n<span size='x-large' weight='bold'>")
        elif tag in ['h4', 'h5', 'h6']:
            self.output.append("\n<span size='large' weight='bold'>")
        elif tag in ['b', 'strong']:
            tag_str = "<b>"
            if self.in_table:
                self.cell_content += tag_str
            else:
                self.output.append(tag_str)
        elif tag in ['i', 'em']:
            tag_str = "<i>"
            if self.in_table:
                self.cell_content += tag_str
            else:
                self.output.append(tag_str)
        elif tag in ['s', 'del', 'strike']:
            tag_str = "<s>"
            if self.in_table:
                self.cell_content += tag_str
            else:
                self.output.append(tag_str)
        elif tag in ['code', 'tt']:
            tag_str = "<tt>"
            if self.in_table:
                self.cell_content += tag_str
            else:
                self.output.append(tag_str)
        elif tag == 'p':
            if self.lists:
                if not self.item_start:
                    # Later paragraphs of an item line up under its text.
                    self._ensure_newline()
                    self.output.append(self.LIST_INDENT * len(self.lists))
            elif not self.in_table and self.output and not self.output[-1].endswith("\n\n"):
                self.output.append("\n")
        elif tag in ['ul', 'ol']:
            if self.lists:
                self._ensure_newline()
            else:
                self.output.append("\n")
            number: Optional[int] = None
            if tag == 'ol':
                try:
                    number = int(dict(attrs).get('start') or 1)
                except ValueError:
                    number = 1
            self.lists.append(number)
            self.item_start = False
        elif tag == 'li':
            self._ensure_newline()
            indent = self.LIST_INDENT * max(len(self.lists) - 1, 0)
            number = self.lists[-1] if self.lists else None
            if number is None:
                self.output.append(f"{indent}• ")
            else:
                self.output.append(f"{indent}{number}. ")
                self.lists[-1] = number + 1
            self.item_start = True
        elif tag == 'a':
            href = dict(attrs).get('href') or ''
            tag_str = f"<a href='{html.escape(href)}'>"
            if self.in_table: 
                 self.cell_content += tag_str
            else:
                self.output.append(tag_str)
        elif tag == 'br':
            if self.in_table:
                self.cell_content += " "
            else:
                self.output.append("\n")
        elif tag == 'hr':
            self.output.append("\n" + "─" * 20 + "\n")
        elif tag == 'pre':
            self.output.append("\n  ") 
        elif tag == 'blockquote':
            self.output.append("\n  <i>") # Indent and italicize quote
            
    def handle_endtag(self, tag: str) -> None:
        if tag == 'table':
            self.in_table = False
            if self.collect_tables:
                header = self.table_rows[:1] if self.table_has_header else []
                self.tables.append({'header': header[0] if header else [], 'align': self.table_align,
                                    'rows': self.table_rows[len(header):]})
            else:
                self.render_ascii_table()
            self.output.append("\n")
        elif tag == 'tr':
            if self.in_table:
                self.table_rows.append(self.current_row)
            else:
                self.output.append("\n")
        elif tag in ['td', 'th']:
            if self.in_table:
                self.in_cell = False
                self.current_row.append(self.cell_content.strip())
        elif tag in ['h1', 'h2', 'h3', 'h4', 'h5', 'h6']:
            self.output.append("</span>\n")
        elif tag in ['b', 'strong']:
            tag_str = "</b>"
            if self.in_table:
                self.cell_content += tag_str
            else:
                self.output.append(tag_str)
        elif tag in ['i', 'em']:
            tag_str = "</i>"
            if self.in_table:
                self.cell_content += tag_str
            else:
                self.output.append(tag_str)
        elif tag in ['s', 'del', 'strike']:
            tag_str = "</s>"
            if self.in_table:
                self.cell_content += tag_str
            else:
                self.output.append(tag_str)
        elif tag in ['code', 'tt']:
            tag_str = "</tt>"
            if self.in_table:
                self.cell_content += tag_str
            else:
                self.output.append(tag_str)
        elif tag == 'p':
            if self.lists:
                self._ensure_newline()
            elif not self.in_table:
                self.output.append("\n")
        elif tag == 'a':
            tag_str = "</a>"
            if self.in_table:
                self.cell_content += tag_str
            else:
                self.output.append(tag_str)
        elif tag == 'li':
            self._ensure_newline()
            self.item_start = False
        elif tag in ['ul', 'ol']:
            if self.lists:
                self.lists.pop()
            self.item_start = False
        elif tag == 'blockquote':
            self.output.append("</i>\n")
        elif tag == 'pre':
            self.output.append("\n")

    def handle_data(self, data: str) -> None:
        if self.in_table and self.in_cell:
            self.cell_content += html.escape(data) 
        elif not self.in_table:
            if self.lists:
                # Line breaks between list tags are HTML formatting, not content.
                if not data.strip() and (self.item_start or not self.output or self.output[-1].endswith("\n")):
                    return
                if self.item_start:
                    data = data.lstrip()
                self.item_start = False
            self.output.append(html.escape(data))

    def _ensure_newline(self) -> None:
        if self.output and not self.output[-1].endswith("\n"):
            self.output.append("\n")

    def render_ascii_table(self) -> None:
        """Renders the collected table rows as an ASCII table using Pango tags for styling."""
        if not self.table_rows:
            return
            
        def visible_len(s: str) -> int:
            """Helper to get visible length (ignoring pango tags)."""
            return len(re.sub(r'<[^>]+>', '', s))

        # Handle multi-line cells: replace <br> with \n and split
        processed_rows = []
        for cells in self.table_rows:
            processed_row = []
            for cell in cells:
                cell_text = re.sub(r'<br\s*/?>', '\n', cell, flags=re.IGNORECASE)
                processed_row.append(cell_text.split('\n'))
            processed_rows.append(processed_row)

        # Calc max widths
        col_widths: Dict[int, int] = {}
        for row in processed_rows:
            for i, cell_lines in enumerate(row):
                max_line_len = max((visible_len(line) for line in cell_lines), default=0)
                col_widths[i] = max(col_widths.get(i, 0), max_line_len)
        
        # Generate lines
        lines: List[str] = []
        for row in processed_rows:
            max_height = max((len(cell_lines) for cell_lines in row), default=1)
            for h in range(max_height):
                line_parts: List[str] = []
                for i, cell_lines in enumerate(row):
                    width = col_widths.get(i, 0)
                    if h < len(cell_lines):
                        cell_line = cell_lines[h]
                    else:
                        cell_line = ""
                    v_len = visible_len(cell_line)
                    padding = width - v_len
                    line_parts.append(cell_line + " " * padding)
                lines.append(" | ".join(line_parts))
            
        table_str = "\n".join(lines)
        self.output.append(f"<tt>{table_str}</tt>")

    def get_markup(self) -> str:
        """Returns the accumulated Pango markup."""
        return "".join(self.output).strip()


MARKDOWN_EXTENSIONS = ['extra', 'fenced_code', 'sane_lists']
TABLE_DELIMITER = re.compile(r'^ {0,3}\|?(\s*:?-+:?\s*\|)+(\s*:?-+:?\s*)?$')
FENCE = re.compile(r'^\s*(`{3,}|~{3,})')
FENCE_OPEN = re.compile(r'^(\s*)(`{3,}|~{3,})(.*)$')
FENCE_CLOSE = re.compile(r'^(\s*)(`{3,}|~{3,})\s*$')
# Long responses are rendered as separate segments of about this many characters,
# so that streamed text only re-renders the segment still being written.
SEGMENT_SIZE = 2000
DISPLAY_OPENER = re.compile(r'\\\[|\$\$|\\begin\{')
# A line that may belong to the list, quote or indented block before the blank line above it.
CONTINUATION = re.compile(r'[ \t>:]|[-*+][ \t]|\d+[.)][ \t]')


def segment_ends(text: str, complete: bool = True) -> List[int]:
    """Offsets where text can be cut into segments that render independently.

    A cut is made at the first paragraph break once a segment is long enough. Text
    added after a cut never changes the segments before it, so they can be left
    alone while a response streams in. The last line only counts when it is
    complete, as an unfinished line may still turn into a list item.
    """
    ends: List[int] = []
    lines = text.split('\n')
    start = pos = 0
    fence: Optional[str] = None
    blank = False
    prose: List[str] = []
    for index, line in enumerate(lines):
        if fence:
            close = FENCE_CLOSE.match(line)
            if close and close.group(2)[0] == fence[0] and len(close.group(2)) >= len(fence):
                fence = None
        elif not line.strip():
            blank = True
        else:
            if (blank and pos - start >= SEGMENT_SIZE and (complete or index < len(lines) - 1)
                    and not CONTINUATION.match(line)
                    # An equation that is still open could reach back across the cut.
                    and not DISPLAY_OPENER.search(math_render.DISPLAY_MATH.sub('', '\n'.join(prose)))):
                ends.append(pos)
                start = pos
                prose = []
            blank = False
            opening = FENCE_OPEN.match(line)
            if opening:
                fence = opening.group(2)
            else:
                prose.append(line)
        pos += len(line) + 1
    return ends


def _table_start(lines: List[str], index: int) -> bool:
    """True when a top-level pipe table starts at lines[index]."""
    if index + 1 >= len(lines):
        return False
    header = lines[index]
    return ('|' in header and bool(header.strip()) and not re.match(r'^(\t| {4})', header)
            and not FENCE.match(header) and bool(TABLE_DELIMITER.match(lines[index + 1].rstrip())))


def table_data(text: str) -> Optional[Dict[str, Any]]:
    """Parses one Markdown table into a header, column alignments and rows of Pango markup."""
    if not markdown:
        return None
    try:
        text, spans = math_render.protect(text)
        parser = PangoMarkupParser(collect_tables=True)
        parser.feed(markdown.markdown(text, extensions=MARKDOWN_EXTENSIONS))
    except Exception:
        return None
    if len(parser.tables) != 1 or not parser.tables[0]['header'] or parser.get_markup():
        return None
    table = parser.tables[0]
    # A header without rows comes back with one empty row.
    if len(table['rows']) == 1 and not any(table['rows'][0]):
        table['rows'] = []
    table['header'] = [math_render.restore(cell, spans) for cell in table['header']]
    table['rows'] = [[math_render.restore(cell, spans) for cell in row] for row in table['rows']]
    return table


# Before GTK 4.24 column view rows stay one line tall and cut off wrapped text,
# so cells stay on one line there and wide tables scroll sideways.
WRAP_TABLE_CELLS = Gtk.check_version(4, 24, 0) is None


def font_pixels(widget: Gtk.Widget) -> float:
    """The size of the widget's font in logical pixels."""
    font = widget.get_pango_context().get_font_description()
    size = font.get_size() / Pango.SCALE or 11
    if not font.get_size_is_absolute():
        dpi = widget.get_settings().get_property('gtk-xft-dpi') / 1024
        size *= (dpi if dpi > 0 else 96) / 72
    return size


def snapshot_equation(snapshot: Gtk.Snapshot, texture: Gdk.Texture, bounds: Graphene.Rect, color: Gdk.RGBA) -> None:
    """Draws an equation in the given colour. Its texture is only a mask."""
    snapshot.push_mask(Gsk.MaskMode.ALPHA)
    snapshot.append_texture(texture, bounds)
    snapshot.pop()
    snapshot.append_color(color, bounds)
    snapshot.pop()


class MathLabel(Gtk.Label):
    """A label that draws the inline equations in its markup as images on the text's baseline.
    Equations that cannot be rendered are shown as text."""
    __gtype_name__ = 'TureenMathLabel'
    OBJECT = '￼'  # Stands in the text where an equation is drawn.

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._markup = ''
        self._equations: List[Tuple[int, Tuple[Gdk.Texture, float, float, float]]] = []
        self.connect('notify::scale-factor', lambda *args: self.set_markup(self._markup))

    def set_markup(self, markup: str) -> None:
        self._markup = markup
        had_equations = bool(self._equations)
        self._equations = []
        if '' not in markup:
            Gtk.Label.set_markup(self, markup)
            if had_equations:
                self.set_attributes(None)
            return
        size, scale = round(font_pixels(self)), self.get_scale_factor()
        images = []

        def convert(tex: str) -> str:
            image = math_render.render(tex, size, scale, True)
            if not image:
                return math_render.inline_markup(tex)
            images.append(image)
            return self.OBJECT

        Gtk.Label.set_markup(self, math_render.expand(markup.replace(self.OBJECT, ''), convert))
        # Each stand-in character is given the size of its equation, so the text makes room for it.
        attributes = Pango.AttrList()
        index = 0
        for image, before in zip(images, self.get_text().split(self.OBJECT)):
            index += len(before.encode())
            texture, width, height, ascent = image
            shape = Pango.Rectangle()
            shape.x, shape.y = 0, -round(ascent * Pango.SCALE)
            shape.width, shape.height = round(width * Pango.SCALE), round(height * Pango.SCALE)
            attribute = Pango.attr_shape_new(shape, shape)
            attribute.start_index, attribute.end_index = index, index + len(self.OBJECT.encode())
            attributes.insert(attribute)
            self._equations.append((index, image))
            index += len(self.OBJECT.encode())
        self.set_attributes(attributes)

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        Gtk.Label.do_snapshot(self, snapshot)
        if not self._equations:
            return
        layout = self.get_layout()
        left, top = self.get_layout_offsets()
        baselines = []
        lines = layout.get_iter()
        while True:
            baselines.append(lines.get_baseline())
            if not lines.next_line():
                break
        color = self.get_color()
        for index, (texture, width, height, ascent) in self._equations:
            line = layout.index_to_line_x(index, False)[0]
            x = left + layout.index_to_pos(index).x / Pango.SCALE
            y = top + baselines[line] / Pango.SCALE - ascent
            snapshot_equation(snapshot, texture, Graphene.Rect().init(round(x), round(y), width, height), color)


class TableRow(GObject.Object):
    def __init__(self, cells: List[str]) -> None:
        super().__init__()
        self.cells = cells


class TableBlock(Gtk.Box):
    """A Markdown table shown in a column view that is always as tall as its rows."""
    __gtype_name__ = 'TureenTableBlock'

    def __init__(self, data: Dict[str, Any], raw: str) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6, halign=Gtk.Align.START)
        self.add_css_class("table-block")
        self.add_css_class("margin-v-6")
        self.header: List[str] = data['header']
        self.align: List[float] = data['align']
        self.raw = raw
        self._height_source = 0
        self.store = Gio.ListStore.new(TableRow)
        self.view = Gtk.ColumnView(model=Gtk.NoSelection(model=self.store), reorderable=False,
                                   show_row_separators=True, show_column_separators=True)
        for index, title in enumerate(self.header):
            factory = Gtk.SignalListItemFactory()
            factory.connect('setup', self._setup_cell, self.align[index] if index < len(self.align) else 0.0)
            factory.connect('bind', self._bind_cell, index)
            # Column titles are plain text.
            plain = html.unescape(re.sub(r'<[^>]+>', '', math_render.to_text(title)))
            self.view.append_column(Gtk.ColumnViewColumn(title=plain, factory=factory, expand=True))
        self.scrolled = Gtk.ScrolledWindow(child=self.view, propagate_natural_width=True, propagate_natural_height=True)
        self.scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        # Rows changing height show up on the vertical adjustment, a new width on the horizontal one.
        self.scrolled.get_vadjustment().connect('changed', self._queue_height)
        self.scrolled.get_hadjustment().connect('changed', self._queue_height)
        # The border is on a separate frame so the scrolled window's height is exactly its request.
        self.append(Gtk.Frame(child=self.scrolled))
        copy_button = Gtk.Button(label=_('Copy Table'), halign=Gtk.Align.END)
        from .widgets.feedback import copy_text
        copy_button.connect('clicked', lambda button: copy_text(button, self.raw))
        self.append(copy_button)
        self.set_rows(data['rows'])

    def _setup_cell(self, factory: Any, item: Any, xalign: float) -> None:
        justify = Gtk.Justification.RIGHT if xalign == 1.0 else Gtk.Justification.CENTER if xalign == 0.5 else Gtk.Justification.LEFT
        item.set_child(MathLabel(use_markup=True, selectable=True, wrap=WRAP_TABLE_CELLS, wrap_mode=Pango.WrapMode.WORD_CHAR,
                                 xalign=xalign, yalign=0, justify=justify))

    def _bind_cell(self, factory: Any, item: Any, index: int) -> None:
        cells = item.get_item().cells
        item.get_child().set_markup(cells[index] if index < len(cells) else "")

    def set_rows(self, rows: List[List[str]]) -> None:
        """Replaces the rows, leaving unchanged leading rows in place."""
        old = [self.store.get_item(i).cells for i in range(self.store.get_n_items())]
        start = 0
        while start < min(len(old), len(rows)) and old[start] == rows[start]:
            start += 1
        if start == len(old) == len(rows):
            return
        self.store.splice(start, len(old) - start, [TableRow(row) for row in rows[start:]])
        self._queue_height()

    def _queue_height(self, *args: Any) -> None:
        if not self._height_source:
            self._height_source = GLib.idle_add(self._sync_height)

    def _sync_height(self) -> bool:
        """Column views request a height that ignores how cells wrap at the
        allocated width, so request the height the rows really need."""
        self._height_source = 0
        scrolled = self.scrolled
        if not scrolled.get_mapped() or scrolled.get_height() <= 0:
            return False
        adjustment = scrolled.get_vadjustment()
        if adjustment.get_upper() > adjustment.get_page_size() + 1:
            # Rows are cut off: grow by the missing amount.
            want = scrolled.get_height() + adjustment.get_upper() - adjustment.get_page_size()
        else:
            # Everything fits: shrink to the bottom edge of the last row.
            rows = self.view.get_last_child()
            ok, bounds = rows.compute_bounds(scrolled)
            if not ok:
                return False
            bottom = bounds.get_y()
            below = scrolled.get_height() - (bounds.get_y() + bounds.get_height())
            row = rows.get_first_child()
            while row:
                ok, row_bounds = row.compute_bounds(scrolled)
                if ok:
                    bottom = max(bottom, row_bounds.get_y() + row_bounds.get_height())
                row = row.get_next_sibling()
            want = bottom + max(below, 0)
        want = math.ceil(want)
        if abs(want - scrolled.get_height()) > 1:
            scrolled.set_size_request(-1, want)
        return False


class MathPicture(Gtk.Widget):
    """A rendered equation, drawn in the text colour and sized to match the text."""
    __gtype_name__ = 'TureenMathPicture'

    def __init__(self) -> None:
        super().__init__(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        self._tex = ''
        self._image: Optional[Tuple[Gdk.Texture, float, float, float]] = None
        self.connect('notify::scale-factor', lambda *args: self.set_tex(self._tex))

    def set_tex(self, tex: str) -> bool:
        """Renders the equation. Returns False if it cannot be rendered."""
        self._tex = tex
        # Display equations read best a little larger than the text around them.
        self._image = math_render.render(tex, round(font_pixels(self) * 1.2), self.get_scale_factor())
        self.queue_resize()
        return self._image is not None

    def do_measure(self, orientation: Gtk.Orientation, for_size: int) -> Tuple[int, int, int, int]:
        size = 0
        if self._image:
            size = math.ceil(self._image[1 if orientation == Gtk.Orientation.HORIZONTAL else 2])
        return size, size, -1, -1

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        if not self._image:
            return
        texture, width, height, ascent = self._image
        snapshot_equation(snapshot, texture, Graphene.Rect().init(0, 0, width, height), self.get_color())


class MathBlock(Gtk.Box):
    """A display equation, with its TeX source shown instead when it cannot be rendered."""
    __gtype_name__ = 'TureenMathBlock'

    def __init__(self, tex: str) -> None:
        super().__init__(spacing=6)
        self.add_css_class("math-block")
        self.tex: Optional[str] = None
        self.picture = MathPicture()
        self.scrolled = Gtk.ScrolledWindow(child=self.picture, hexpand=True,
                                           propagate_natural_width=True, propagate_natural_height=True)
        self.scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        self.append(self.scrolled)
        self.source = Gtk.Label(selectable=True, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, xalign=0, hexpand=True)
        self.source.add_css_class("monospace")
        self.append(self.source)
        copy_button = Gtk.Button(icon_name='edit-copy-symbolic', tooltip_text=_('Copy TeX'), valign=Gtk.Align.CENTER)
        copy_button.add_css_class("flat")
        from .widgets.feedback import copy_text
        copy_button.connect('clicked', lambda button: copy_text(button, self.tex))
        self.append(copy_button)
        self.set_tex(tex)

    def set_tex(self, tex: str) -> None:
        if tex == self.tex:
            return
        self.tex = tex
        rendered = self.picture.set_tex(tex)
        self.scrolled.set_visible(rendered)
        self.source.set_visible(not rendered)
        self.source.set_text(tex)
        self.picture.update_property([Gtk.AccessibleProperty.LABEL], [tex])


class MarkdownView(Gtk.Box):
    """
    A GTK widget that renders Markdown by breaking it into blocks of text and code.
    Supports incremental updates and block-level syncing to minimize UI churn.
    """
    __gtype_name__ = 'MarkdownView'

    def __init__(self, text: str = "", wrap_code=False, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.set_orientation(Gtk.Orientation.VERTICAL)
        self.set_spacing(12)
        self.set_hexpand(True)
        self.wrap_code = wrap_code
        self.add_css_class("markdown-view")
        self._text: str = text
        self._tables: Dict[str, Optional[Dict[str, Any]]] = {}
        # While streaming, the leading segments that are finished and their last widget.
        self._settled_text: str = ''
        self._settled_widget: Optional[Gtk.Widget] = None

        self._theme_handler_id = None
        if Adw:
            sm = Adw.StyleManager.get_default()
            self._theme_handler_id = sm.connect("notify::dark", self._on_theme_changed)
            self.connect("destroy", self._on_destroy)
            
        self.render()

    def _on_destroy(self, widget: Gtk.Widget) -> None:
        if Adw and self._theme_handler_id:
            sm = Adw.StyleManager.get_default()
            sm.disconnect(self._theme_handler_id)

    def _on_theme_changed(self, style_manager: Any, pspec: Any) -> None:
        is_dark = style_manager.get_dark()
        scheme_name = "oblivion" if is_dark else "classic"
        
        if not GtkSource:
            return
            
        sm = GtkSource.StyleSchemeManager.get_default()
        scheme = sm.get_scheme(scheme_name)
        if not scheme:
            return
            
        curr_child = self.get_first_child()
        while curr_child:
            if curr_child.has_css_class("code-block"):
                scrolled = getattr(curr_child, '_code_scrolled', None)
                if isinstance(scrolled, Gtk.ScrolledWindow):
                    view = scrolled.get_child()
                    if isinstance(view, GtkSource.View):
                        buffer = view.get_buffer()
                        buffer.set_style_scheme(scheme)
            curr_child = curr_child.get_next_sibling()

    def update(self, text: str, streaming: bool = False) -> None:
        """Updates the view with new markdown text.

        With streaming, text that extends what is already shown only renders the
        segments that are new or unfinished. The result is the same either way,
        apart from a last line that streaming does not yet treat as complete.
        """
        self._text = text
        self.render(streaming)

    def render(self, streaming: bool = False) -> None:
        """Parses and renders the current markdown text."""
        text = self._text
        if not streaming or not text.startswith(self._settled_text):
            self._settled_text, self._settled_widget = '', None
        offset = len(self._settled_text)
        rest = text[offset:]
        start = 0
        for end in segment_ends(rest, complete=not streaming):
            blocks = self._blocks(rest[start:end].rstrip(), {})
            self._settled_widget = self._sync_view(blocks, self._settled_widget, trim=False)
            start = end
        if start:
            self._settled_text = text[:offset + start]
        tables: Dict[str, Optional[Dict[str, Any]]] = {}
        self._sync_view(self._blocks(rest[start:], tables), self._settled_widget)
        self._tables = tables

    def _blocks(self, text: str, tables: Dict[str, Optional[Dict[str, Any]]]) -> List[Dict[str, Any]]:
        """Parses one segment, recording the tables it parsed in `tables`."""
        blocks = self._parse_blocks(text)
        for block in blocks:
            if block['type'] == 'table':
                raw = block['content']
                data = self._tables[raw] if raw in self._tables else table_data(raw)
                tables[raw] = data
                if data is None:
                    block['type'] = 'text'  # Not a table after all
                else:
                    block['data'] = data
        return blocks

    def _parse_blocks(self, text: str) -> List[Dict[str, Any]]:
        """Parses markdown text into a list of block dictionaries."""
        blocks: List[Dict[str, Any]] = []
        lines = text.split('\n')
        i = 0
        n = len(lines)
        
        while i < n:
            line = lines[i]
            match = FENCE_OPEN.match(line)
            
            if match:
                indent, fence, raw_lang = match.groups()
                lang = raw_lang.strip()
                
                content_start_idx = i + 1
                code_lines = []
                i = content_start_idx
                while i < n:
                    curr_line = lines[i]
                    close_match = FENCE_CLOSE.match(curr_line)
                    if close_match:
                        c_indent, c_fence = close_match.groups()
                        if c_fence[0] == fence[0] and len(c_fence) >= len(fence):
                            i += 1
                            break
                    
                    code_lines.append(curr_line)
                    i += 1
                
                blocks.append({'type': 'code', 'lang': lang, 'content': "\n".join(code_lines)})
                continue
            
            text_buffer = []
            while i < n:
                curr_line = lines[i]
                if FENCE.match(curr_line) or _table_start(lines, i):
                    break
                text_buffer.append(curr_line)
                i += 1
            
            if text_buffer:
                for kind, content in math_render.split_display("\n".join(text_buffer)):
                    blocks.append({'type': kind, 'content': content})

            if i < n and _table_start(lines, i):
                end = i + 2
                while end < n and lines[end].strip() and '|' in lines[end] and not FENCE.match(lines[end]):
                    end += 1
                blocks.append({'type': 'table', 'content': "\n".join(lines[i:end])})
                i = end
                
        return blocks

    def _sync_view(self, blocks: List[Dict[str, Any]], after: Optional[Gtk.Widget] = None,
                   trim: bool = True) -> Optional[Gtk.Widget]:
        """Syncs the widgets that follow `after` with the parsed blocks, minimizing churn.
        Widgets left over are removed unless `trim` is False. Returns the last block's widget."""
        curr_child = after.get_next_sibling() if after else self.get_first_child()
        prev_child: Optional[Gtk.Widget] = after
        
        for block in blocks:
            match = False
            if curr_child:
                is_code = curr_child.has_css_class("code-block")
                is_text = isinstance(curr_child, Gtk.Label)
                
                if block['type'] == 'code' and is_code and curr_child._lang == block['lang']:
                    self._update_code_block(curr_child, block['lang'], block['content'])
                    match = True
                elif block['type'] == 'text' and is_text:
                    self._update_text_block(curr_child, block['content'])
                    match = True
                elif (block['type'] == 'table' and isinstance(curr_child, TableBlock)
                      and (curr_child.header, curr_child.align) == (block['data']['header'], block['data']['align'])):
                    curr_child.raw = block['content']
                    curr_child.set_rows(block['data']['rows'])
                    match = True
                elif block['type'] == 'math' and isinstance(curr_child, MathBlock):
                    curr_child.set_tex(block['content'])
                    match = True
            
            if match and curr_child:
                prev_child = curr_child
                curr_child = curr_child.get_next_sibling()
            else:
                if curr_child:
                    next_s = curr_child.get_next_sibling()
                    self.remove(curr_child)
                    curr_child = next_s
                
                new_widget = self._create_widget_for_block(block)
                self.insert_child_after(new_widget, prev_child)
                prev_child = new_widget
                
        while trim and curr_child:
            next_s = curr_child.get_next_sibling()
            self.remove(curr_child)
            curr_child = next_s
        return prev_child

    def _create_widget_for_block(self, block: Dict[str, Any]) -> Gtk.Widget:
        """Creates an appropriate widget for a given block type."""
        if block['type'] == 'text':
             label = MathLabel()
             label.set_wrap(True)
             label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
             label.set_xalign(0)
             label.set_selectable(True)
             self._update_text_block(label, block['content'])
             return label
        elif block['type'] == 'table':
             return TableBlock(block['data'], block['content'])
        elif block['type'] == 'math':
             return MathBlock(block['content'])
        else: # code
             return self._create_code_widget(block['lang'], block['content'])

    def _create_code_widget(self, lang: str, code: str) -> Gtk.Box:
        """Creates a styled code view widget, using GtkSource if available."""
        wrapper = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        wrapper.add_css_class("code-block")
        wrapper.add_css_class("margin-v-6")
        
        if GtkSource:
            buffer = GtkSource.Buffer()
            lm = GtkSource.LanguageManager.get_default()
            language = lm.get_language(lang)
            if language:
                buffer.set_language(language)
            
            sm = GtkSource.StyleSchemeManager.get_default()
            scheme_name = "oblivion"
            if Adw:
                style_manager = Adw.StyleManager.get_default()
                if not style_manager.get_dark():
                    scheme_name = "classic"
                    
            scheme = sm.get_scheme(scheme_name) 
            if scheme:
                buffer.set_style_scheme(scheme)
                
            view = GtkSource.View.new_with_buffer(buffer)
            view.set_show_line_numbers(False)
        else:
            view = Gtk.TextView()
            view.set_monospace(True)
            
        view.set_editable(False)
        view.set_direction(Gtk.TextDirection.LTR)
        view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR if self.wrap_code else Gtk.WrapMode.NONE)
        view.set_hexpand(True)
        view.set_top_margin(12)
        view.set_bottom_margin(12)
        view.set_left_margin(12)
        view.set_right_margin(12)
        view.add_css_class("code-view")
        
        view.get_buffer().set_text(code)

        scrolled = Gtk.ScrolledWindow(hexpand=True)
        scrolled.set_child(view)
        scrolled.set_propagate_natural_height(True)
        scrolled.set_policy(Gtk.PolicyType.NEVER if self.wrap_code else Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        
        wrapper.append(scrolled)
        wrapper._code_scrolled = scrolled
        wrapper._raw_code = code
        wrapper._lang = lang
        toolbar = Adw.WrapBox(child_spacing=6, line_spacing=6, halign=Gtk.Align.END)
        copy_button = Gtk.Button(label=_('Copy Code'))
        from .widgets.feedback import copy_text
        copy_button.connect('clicked', lambda button: copy_text(button, wrapper._raw_code))
        toolbar.append(copy_button)
        wrapper.append(toolbar)
        wrapper._preview = None
        if lang.lower() in ('markdown', 'md'):
            preview = MarkdownView(code, visible=False)
            wrapper._preview = preview
            toggle = Gtk.ToggleButton(label=_('Preview Markdown'))
            def toggled(button):
                preview.set_visible(button.get_active())
                scrolled.set_visible(not button.get_active())
            toggle.connect('toggled', toggled)
            toolbar.append(toggle)
            wrapper.append(preview)
        return wrapper

    def _update_text_block(self, label: Gtk.Label, text: str) -> None:
        """Updates a text block widget with rendered markdown content."""
        if getattr(label, '_raw_md', None) == text:
            return
            
        label._raw_md = text
        
        if not text.strip():
            label.set_markup("")
            return
        
        try:
            text, spans = math_render.protect(text)
            text = re.sub(r'~~(.*?)~~', r'<s>\1</s>', text)
            if markdown:
                html_text = markdown.markdown(text, extensions=MARKDOWN_EXTENSIONS)
            else:
                html_text = html.escape(text)
            parser = PangoMarkupParser()
            parser.feed(html_text)
            markup = math_render.restore(parser.get_markup(), spans)
        except Exception:
            markup = html.escape(label._raw_md)
            
        label.set_markup(markup)

    def _update_code_block(self, wrapper: Gtk.Box, lang: str, code: str) -> None:
        """Updates an existing code block widget with new content."""
        wrapper._raw_code = code
        if wrapper._preview is not None:
            wrapper._preview.update(code)
        scrolled = wrapper._code_scrolled
        if isinstance(scrolled, Gtk.ScrolledWindow):
            view = scrolled.get_child()
            if isinstance(view, Gtk.TextView):
                buffer = view.get_buffer()
                start, end = buffer.get_bounds()
                current = buffer.get_text(start, end, True)
                if current != code:
                    buffer.set_text(code)
