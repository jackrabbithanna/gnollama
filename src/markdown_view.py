import html
import re
from typing import List, Dict, Any, Optional, Tuple, Union
from html.parser import HTMLParser
from gi.repository import Gtk, Gdk, Pango, GObject

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
    def __init__(self) -> None:
        super().__init__()
        self.output: List[str] = []
        self.tags: List[str] = []
        
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
            self.output.append("\n") # Spacing before table
        elif tag == 'tr':
            if self.in_table:
                self.current_row = []
        elif tag in ['td', 'th']:
            if self.in_table:
                self.in_cell = True
                self.cell_content = ""
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
            if self.in_table: self.cell_content += tag_str
            else: self.output.append(tag_str)
        elif tag in ['i', 'em']:
            tag_str = "<i>"
            if self.in_table: self.cell_content += tag_str
            else: self.output.append(tag_str)
        elif tag in ['s', 'del', 'strike']:
            tag_str = "<s>"
            if self.in_table: self.cell_content += tag_str
            else: self.output.append(tag_str)
        elif tag in ['code', 'tt']:
            tag_str = "<tt>"
            if self.in_table: self.cell_content += tag_str
            else: self.output.append(tag_str)
        elif tag == 'p':
            if not self.in_table and self.output and not self.output[-1].endswith("\n\n"):
                self.output.append("\n")
        elif tag == 'ul':
            self.output.append("\n")
        elif tag == 'li':
            self.output.append("• ")
        elif tag == 'a':
            href = dict(attrs).get('href', '')
            tag_str = f"<a href='{html.escape(href)}'>"
            if self.in_table: 
                 self.cell_content += tag_str
            else:
                self.output.append(tag_str)
        elif tag == 'br':
            if self.in_table: self.cell_content += " "
            else: self.output.append("\n")
        elif tag == 'hr':
            self.output.append("\n" + "─" * 20 + "\n")
        elif tag == 'pre':
            self.output.append("\n  ") 
        elif tag == 'blockquote':
            self.output.append("\n  <i>") # Indent and italicize quote
            
    def handle_endtag(self, tag: str) -> None:
        if tag == 'table':
            self.in_table = False
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
            if self.in_table: self.cell_content += tag_str
            else: self.output.append(tag_str)
        elif tag in ['i', 'em']:
            tag_str = "</i>"
            if self.in_table: self.cell_content += tag_str
            else: self.output.append(tag_str)
        elif tag in ['s', 'del', 'strike']:
            tag_str = "</s>"
            if self.in_table: self.cell_content += tag_str
            else: self.output.append(tag_str)
        elif tag in ['code', 'tt']:
            tag_str = "</tt>"
            if self.in_table: self.cell_content += tag_str
            else: self.output.append(tag_str)
        elif tag == 'p':
            if not self.in_table: self.output.append("\n")
        elif tag == 'a':
            tag_str = "</a>"
            if self.in_table: self.cell_content += tag_str
            else: self.output.append(tag_str)
        elif tag == 'li':
            self.output.append("\n")
        elif tag == 'blockquote':
            self.output.append("</i>\n")
        elif tag == 'pre':
            self.output.append("\n")

    def handle_data(self, data: str) -> None:
        if self.in_table and self.in_cell:
            self.cell_content += html.escape(data) 
        elif not self.in_table:
            self.output.append(html.escape(data))
    
    def render_ascii_table(self) -> None:
        """Renders the collected table rows as an ASCII table using Pango tags for styling."""
        if not self.table_rows:
            return
            
        def visible_len(s: str) -> int:
            """Helper to get visible length (ignoring pango tags)."""
            return len(re.sub(r'<[^>]+>', '', s))

        # Handle multi-line cells: replace <br> with \n and split
        processed_rows = []
        for row in self.table_rows:
            processed_row = []
            for cell in row:
                cell_text = re.sub(r'<br\s*/?>', '\n', cell, flags=re.IGNORECASE)
                lines = cell_text.split('\n')
                processed_row.append(lines)
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


class MarkdownView(Gtk.Box):
    """
    A GTK widget that renders Markdown by breaking it into blocks of text and code.
    Supports incremental updates and block-level syncing to minimize UI churn.
    """
    __gtype_name__ = 'MarkdownView'

    def __init__(self, text: str = "", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.set_orientation(Gtk.Orientation.VERTICAL)
        self.set_spacing(12)
        self.add_css_class("markdown-view")
        self._text: str = text
        
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
                scrolled = curr_child.get_first_child()
                if isinstance(scrolled, Gtk.ScrolledWindow):
                    view = scrolled.get_child()
                    if isinstance(view, GtkSource.View):
                        buffer = view.get_buffer()
                        buffer.set_style_scheme(scheme)
            curr_child = curr_child.get_next_sibling()

    def update(self, text: str) -> None:
        """Updates the view with new markdown text."""
        self._text = text
        self.render()

    def render(self) -> None:
        """Parses and renders the current markdown text."""
        blocks = self._parse_blocks(self._text)
        self._sync_view(blocks)

    def _parse_blocks(self, text: str) -> List[Dict[str, Any]]:
        """Parses markdown text into a list of block dictionaries."""
        blocks: List[Dict[str, Any]] = []
        lines = text.split('\n')
        i = 0
        n = len(lines)
        
        while i < n:
            line = lines[i]
            match = re.match(r'^(\s*)(`{3,}|~{3,})(.*)$', line)
            
            if match:
                indent, fence, raw_lang = match.groups()
                lang = raw_lang.strip()
                
                content_start_idx = i + 1
                if not lang and content_start_idx < n:
                    next_line = lines[content_start_idx].strip()
                    clean_lang = next_line.strip('`')
                    lower_clean = clean_lang.lower()
                    
                    if lower_clean in ['markdown', 'md', 'python', 'py', 'bash', 'sh', 'javascript', 'js', 'html', 'css', 'json', 'xml', 'sql', 'java', 'c', 'cpp', 'go', 'rs', 'rust']:
                        lang = clean_lang
                        content_start_idx += 1
                    elif re.match(r'^[-*_]{3,}\s*$', next_line) or re.match(r'^#{1,6}\s', next_line):
                        lang = 'markdown'
                
                code_lines = []
                i = content_start_idx
                while i < n:
                    curr_line = lines[i]
                    close_match = re.match(r'^(\s*)(`{3,}|~{3,})\s*$', curr_line)
                    if close_match:
                        c_indent, c_fence = close_match.groups()
                        if c_fence[0] == fence[0] and len(c_fence) >= len(fence):
                            i += 1
                            break
                    
                    code_lines.append(curr_line)
                    i += 1
                
                if lang.lower() in ['markdown', 'md']:
                    inner_blocks = self._parse_blocks("\n".join(code_lines))
                    blocks.extend(inner_blocks)
                else:
                    blocks.append({
                        'type': 'code',
                        'lang': lang,
                        'content': "\n".join(code_lines)
                    })
                continue
            
            text_buffer = []
            while i < n:
                curr_line = lines[i]
                if re.match(r'^\s*(`{3,}|~{3,})', curr_line):
                    break
                text_buffer.append(curr_line)
                i += 1
            
            if text_buffer:
                blocks.append({
                    'type': 'text',
                    'content': "\n".join(text_buffer)
                })
                
        return blocks

    def _sync_view(self, blocks: List[Dict[str, Any]]) -> None:
        """Syncs the Gtk widget list with the parsed blocks, minimizing churn."""
        curr_child = self.get_first_child()
        prev_child: Optional[Gtk.Widget] = None
        
        for block in blocks:
            match = False
            if curr_child:
                is_code = curr_child.has_css_class("code-block")
                is_text = isinstance(curr_child, Gtk.Label)
                
                if block['type'] == 'code' and is_code:
                    self._update_code_block(curr_child, block['lang'], block['content'])
                    match = True
                elif block['type'] == 'text' and is_text:
                    self._update_text_block(curr_child, block['content'])
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
                
        while curr_child:
            next_s = curr_child.get_next_sibling()
            self.remove(curr_child)
            curr_child = next_s

    def _create_widget_for_block(self, block: Dict[str, Any]) -> Gtk.Widget:
        """Creates an appropriate widget for a given block type."""
        if block['type'] == 'text':
             label = Gtk.Label()
             label.set_wrap(True)
             label.set_xalign(0)
             label.set_selectable(True)
             self._update_text_block(label, block['content'])
             return label
        else: # code
             return self._create_code_widget(block['lang'], block['content'])

    def _create_code_widget(self, lang: str, code: str) -> Gtk.Box:
        """Creates a styled code view widget, using GtkSource if available."""
        wrapper = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
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
        view.set_wrap_mode(Gtk.WrapMode.NONE)
        view.set_top_margin(12)
        view.set_bottom_margin(12)
        view.set_left_margin(12)
        view.set_right_margin(12)
        view.add_css_class("code-view")
        
        view.get_buffer().set_text(code)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_child(view)
        scrolled.set_propagate_natural_height(True)
        scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        
        wrapper.append(scrolled)
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
            text = re.sub(r'~~(.*?)~~', r'<s>\1</s>', text)
            if markdown:
                html_text = markdown.markdown(text, extensions=['extra', 'fenced_code'])
            else:
                html_text = html.escape(text)
            parser = PangoMarkupParser()
            parser.feed(html_text)
            markup = parser.get_markup()
        except Exception:
            markup = html.escape(text)
            
        label.set_markup(markup)

    def _update_code_block(self, wrapper: Gtk.Box, lang: str, code: str) -> None:
        """Updates an existing code block widget with new content."""
        scrolled = wrapper.get_first_child()
        if isinstance(scrolled, Gtk.ScrolledWindow):
            view = scrolled.get_child()
            if isinstance(view, Gtk.TextView):
                buffer = view.get_buffer()
                start, end = buffer.get_bounds()
                current = buffer.get_text(start, end, True)
                if current != code:
                    buffer.set_text(code)
