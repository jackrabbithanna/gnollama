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
        
    def handle_starttag(self, tag, attrs):
        if tag in ['h1', 'h2']:
            self.output.append("\n<span size='xx-large' weight='bold'>")
        elif tag == 'h3':
            self.output.append("\n<span size='x-large' weight='bold'>")
        elif tag in ['h4', 'h5', 'h6']:
            self.output.append("\n<span size='large' weight='bold'>")
        elif tag in ['b', 'strong']:
            self.output.append("<b>")
        elif tag in ['i', 'em']:
            self.output.append("<i>")
        elif tag in ['code', 'tt']:
            self.output.append("<tt>")
        elif tag == 'p':
            if self.output and not self.output[-1].endswith("\n\n"):
                self.output.append("\n")
        elif tag == 'ul':
            self.output.append("\n")
        elif tag == 'li':
            self.output.append("• ")
        elif tag == 'a':
            href = dict(attrs).get('href', '')
            self.output.append(f"<a href='{html.escape(href)}'>")
        elif tag == 'br':
            self.output.append("\n")
        elif tag == 'hr':
            self.output.append("\n" + "─" * 20 + "\n")
            
    def handle_endtag(self, tag):
        if tag in ['h1', 'h2', 'h3', 'h4', 'h5', 'h6']:
            self.output.append("</span>\n")
        elif tag in ['b', 'strong']:
            self.output.append("</b>")
        elif tag in ['i', 'em']:
            self.output.append("</i>")
        elif tag in ['code', 'tt']:
            self.output.append("</tt>")
        elif tag == 'p':
            self.output.append("\n")
        elif tag == 'a':
            self.output.append("</a>")
        elif tag == 'li':
            self.output.append("\n")

    def handle_data(self, data):
        self.output.append(html.escape(data))
        
    def get_markup(self):
        return "".join(self.output).strip()


class MarkdownView(Gtk.Box):
    __gtype_name__ = 'MarkdownView'

    def __init__(self, text="", **kwargs):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, **kwargs)
        self.add_css_class("markdown-view")
        self._text = text
        self.set_spacing(12) # Spacing between blocks (paragraphs, code blocks)
        
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
            label = Gtk.Label(label=self._text)
            label.set_wrap(True)
            label.set_xalign(0)
            self.append(label)
            return

        # We will split the markdown into blocks: Code blocks vs Text
        # A simple approach is to use regex to find code blocks ```...``` 
        # and render them separately, and pass the rest to standard markdown
        # But `markdown` library is better at parsing. 
        # Ideally, we'd write a custom renderer for `markdown` that outputs Gtk Widgets?
        # That's complex. 
        # Simpler: Split by code fences manually, then render chunks.
        
        # Regex for code blocks: ```lang\ncode\n```
        # flags=re.DOTALL to match newlines
        parts = re.split(r'(```(?:\w+)?\n.*?\n```)', self._text, flags=re.DOTALL)
        
        for part in parts:
            if not part: 
                continue
            
            if part.startswith("```") and part.endswith("```"):
                self.render_code_block(part)
            else:
                self.render_text_block(part)

    def render_code_block(self, block_text):
        # Extract lang and code
        # Format: ```lang\ncode...```
        lines = block_text.split('\n')
        first_line = lines[0].strip()
        lang = ""
        if len(first_line) > 3:
            lang = first_line[3:].strip()
        
        # Remove first and last line (fences)
        code = "\n".join(lines[1:-1])
        
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
            # wrapper.append(header) # Optional

        # Code View
        if GtkSource:
            buffer = GtkSource.Buffer()
            lm = GtkSource.LanguageManager.get_default()
            language = lm.get_language(lang)
            if language:
                buffer.set_language(language)
            
            # Style scheme
            sm = GtkSource.StyleSchemeManager.get_default()
            scheme = sm.get_scheme("oblivion") # Hardcoded for now, should detect theme
            if scheme:
                buffer.set_style_scheme(scheme)
                
            buffer.set_text(code)
            view = GtkSource.View.new_with_buffer(buffer)
            view.set_show_line_numbers(False) # Clean look
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
        scrolled.set_max_content_height(400) # Limit max height
        
        wrapper.append(scrolled)
        self.append(wrapper)


    def render_text_block(self, text):
        if not text.strip():
            return
            
        try:
            html_text = markdown.markdown(text)
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

