# markdown_view.py
#
# Copyright 2025 Jackrabbithanna
#
# SPDX-License-Identifier: GPL-3.0-or-later

import html
import re
from gi.repository import Gtk, Gdk, Pango, GObject

try:
    import markdown
    from markdown.extensions import Extension
    from markdown.treeprocessors import Treeprocessor
except ImportError:
    markdown = None

try:
    from gi.repository import GtkSource
except ImportError:
    GtkSource = None

from html.parser import HTMLParser

class PangoMarkupParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.output = []
        self.tags = []
        
        # Table state
        self.in_table = False
        self.table_rows = []
        self.current_row = []
        self.in_cell = False
        self.cell_content = "" # Buffer for cell content
        
    def handle_starttag(self, tag, attrs):
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
            if self.in_table: 
                 href = dict(attrs).get('href', '')
                 # Simplified link for ASCII table, just show text? Or keep link?
                 # Pango allows links in labels.
                 self.cell_content += f"<a href='{html.escape(href)}'>"
            else:
                href = dict(attrs).get('href', '')
                self.output.append(f"<a href='{html.escape(href)}'>")
        elif tag == 'br':
            if self.in_table: self.cell_content += " "
            else: self.output.append("\n")
        elif tag == 'hr':
            self.output.append("\n" + "─" * 20 + "\n")
        elif tag == 'pre':
            # Add indentation/marker for code blocks rendered via text fallback
            self.output.append("\n  ") 
        elif tag == 'blockquote':
            self.output.append("\n  <i>") # Indent and italicize quote
            
    def handle_endtag(self, tag):
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

    def handle_data(self, data):
        if self.in_table and self.in_cell:
            self.cell_content += html.escape(data) 
        elif not self.in_table:
            self.output.append(html.escape(data))
    
    def render_ascii_table(self):
        if not self.table_rows:
            return
            
        # Helper to get visible length (ignoring pango tags)
        def visible_len(s):
            # Strip tags like <b>, </b>, <span ...>
            # Simple regex for <...>
            return len(re.sub(r'<[^>]+>', '', s))

        # Calc max widths
        col_widths = {}
        for row in self.table_rows:
            for i, cell in enumerate(row):
                col_widths[i] = max(col_widths.get(i, 0), visible_len(cell))
        
        # Generate lines
        lines = []
        for row in self.table_rows:
            line_parts = []
            for i, cell in enumerate(row):
                width = col_widths.get(i, 0)
                v_len = visible_len(cell)
                # Padding needed
                padding = width - v_len
                # Left align: cell + spaces
                line_parts.append(cell + " " * padding)
            lines.append(" | ".join(line_parts))
            
        table_str = "\n".join(lines)
        # Use <tt> for monospace alignment
        self.output.append(f"<tt>{table_str}</tt>")

    def get_markup(self):
        return "".join(self.output).strip()


class MarkdownView(Gtk.Box):
    __gtype_name__ = 'MarkdownView'

    def __init__(self, text="", **kwargs):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, **kwargs)
        self.add_css_class("markdown-view")
        self._text = text
        self.set_spacing(12) 
        
        self.render()

    def update(self, text):
        self._text = text
        self.render()

    def render(self):
        # Clear existing children
        child = self.get_first_child()
        while child:
            next_child = child.get_next_sibling()
            self.remove(child)
            child = next_child

        if not markdown:
            # Fallback for missing library
            self.render_text_block(self._text)
            return

        self._process_content(self._text)

    def _process_content(self, text):
        lines = text.split('\n')
        i = 0
        n = len(lines)
        
        while i < n:
            line = lines[i]
            stripped = line.strip()
            
            # Check for fence start
            # Matches ``` or ~~~ (at least 3)
            # Must be checked carefully.
            # We want to match: spaces? + (`{3,} | ~{3,}) + lang?
            match = re.match(r'^(\s*)(`{3,}|~{3,})(.*)$', line)
            
            if match:
                # Start of code block
                indent, fence, raw_lang = match.groups()
                lang = raw_lang.strip()
                
                # Check for "Orphaned Lang" (lang on next line)
                # If lang is empty, peek next line
                content_start_idx = i + 1
                if not lang and content_start_idx < n:
                    # Sniff next line
                    next_line = lines[content_start_idx].strip()
                    # Clean potential backticks from lang guess
                    clean_lang = next_line.strip('`')
                    lower_clean = clean_lang.lower()
                    
                    if lower_clean in ['markdown', 'md', 'python', 'py', 'bash', 'sh', 'javascript', 'js', 'html', 'css', 'json', 'xml', 'sql', 'java', 'c', 'cpp', 'go', 'rs', 'rust']:
                        lang = clean_lang
                        content_start_idx += 1 # Skip the lang line
                    # Heuristic: If it starts with a horizontal rule or header, assume it's markdown
                    elif re.match(r'^[-*_]{3,}\s*$', next_line) or re.match(r'^#{1,6}\s', next_line):
                        lang = 'markdown'
                
                # Consume lines until closing fence or EOF
                code_lines = []
                i = content_start_idx
                closed = False
                while i < n:
                    curr_line = lines[i]
                    # Check for closing fence
                    # Must match opening fence style (backticks/tildes) and length (at least as long)
                    # And match indent (roughly? markdown is loose here, strict indent matching usually <= 3 spaces)
                    close_match = re.match(r'^(\s*)(`{3,}|~{3,})\s*$', curr_line)
                    if close_match:
                        c_indent, c_fence = close_match.groups()
                        if c_fence[0] == fence[0] and len(c_fence) >= len(fence):
                            closed = True
                            i += 1 # Consume closing fence
                            break
                    
                    code_lines.append(curr_line)
                    i += 1
                
                # Render the block
                # If EOF reached without close, we still render (standard markdown behavior usually auto-closes)
                self.render_code_block(lang, "\n".join(code_lines))
                continue
            
            # Not a fence, accumulate text block
            # We need to find the NEXT fence to know where this text block ends
            text_buffer = []
            while i < n:
                curr_line = lines[i]
                if re.match(r'^\s*(`{3,}|~{3,})', curr_line):
                    # Found start of next block
                    break
                text_buffer.append(curr_line)
                i += 1
            
            if text_buffer:
                self.render_text_block("\n".join(text_buffer))


    def render_code_block(self, lang, code):
        # Check if it's markdown - if so, render recursively
        if lang.lower() in ['markdown', 'md']:
             self._process_content(code)
             return

        # Create widget
        wrapper = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        wrapper.add_css_class("code-block")
        wrapper.set_margin_top(6)
        wrapper.set_margin_bottom(6)
        
        # Header (optional, maybe language name)
        if lang:
            header = Gtk.Label(label=lang)
            header.add_css_class("code-header")
            header.set_halign(Gtk.Align.END)
            # wrapper.append(header) 

        # Code View
        if GtkSource:
            buffer = GtkSource.Buffer()
            lm = GtkSource.LanguageManager.get_default()
            language = lm.get_language(lang)
            if language:
                buffer.set_language(language)
            
            # Style scheme
            sm = GtkSource.StyleSchemeManager.get_default()
            scheme = sm.get_scheme("oblivion") 
            if scheme:
                buffer.set_style_scheme(scheme)
                
            buffer.set_text(code)
            view = GtkSource.View.new_with_buffer(buffer)
            view.set_show_line_numbers(False) 
        else:
            view = Gtk.TextView()
            view.get_buffer().set_text(code)
            view.set_monospace(True)

        view.set_editable(False)
        view.set_wrap_mode(Gtk.WrapMode.NONE)
        view.set_top_margin(12)
        view.set_bottom_margin(12)
        view.set_left_margin(12)
        view.set_right_margin(12)
        view.add_css_class("code-view")
        
        # Scrolled Window for code
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_child(view)
        scrolled.set_propagate_natural_height(True)
        scrolled.set_max_content_height(400) 
        
        wrapper.append(scrolled)
        self.append(wrapper)


    def render_text_block(self, text):
        if not text.strip():
            return
            
        try:
            # Pre-process for strikethrough (~~text~~ -> <s>text</s>)
            # Python-markdown doesn't support ~~ by default without extensions not in stdlib
            # Hacky but effective for standard GFM style strikethrough
            text = re.sub(r'~~(.*?)~~', r'<s>\1</s>', text)
            
            # Enable extensions for better parsing
            # 'extra' includes tables, footnotes, etc.
            html_text = markdown.markdown(text, extensions=['extra', 'fenced_code'])
            parser = PangoMarkupParser()
            parser.feed(html_text)
            markup = parser.get_markup()
        except Exception as e:
            # Fallback for parser errors
            markup = html.escape(text)
            print(f"Error parsing Markdown: {e}")
        
        label = Gtk.Label()
        label.set_markup(markup)
        label.set_wrap(True)
        label.set_xalign(0)
        label.set_selectable(True)
        
        self.append(label)

