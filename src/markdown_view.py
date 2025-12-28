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
        elif tag == 'pre':
            # Add indentation/marker for code blocks rendered via text fallback
            self.output.append("\n  ") 
        elif tag == 'blockquote':
            self.output.append("\n  <i>") # Indent and italicize quote
        elif tag == 'table':
            self.output.append("\n")
        elif tag == 'tr':
            self.output.append("\n")
        elif tag in ['td', 'th']:
            self.output.append(" | ")
            
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
        elif tag == 'blockquote':
            self.output.append("</i>\n")
        elif tag == 'pre':
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
        # Use finditer to support matching fence lengths (3 or more backticks)
        # Regex matches: (`{3,}) -> fence
        #                ([^\n]*) -> optional lang (anything but newline)
        #                \n -> newline
        #                .*? -> content
        #                \s* -> whitespace/newlines before closing fence
        #                \1 -> matching closing fence
        pattern = r'(`{3,})([^\n]*)\n(.*?)\s*\1'
        last_pos = 0
        for match in re.finditer(pattern, text, flags=re.DOTALL):
            start, end = match.span()
            
            # Render text before match
            if start > last_pos:
                self.render_text_block(text[last_pos:start])
            
            # Render code block
            # Group 2 is lang, Group 3 is content.
            fence = match.group(1)
            lang = match.group(2).strip()
            code = match.group(3)
            
            # Fix for "Orphaned Lang": If lang is empty, check if first line of content looks like a language
            # This handles cases where the model puts the language on the next line
            if not lang and code:
                lines = code.split('\n', 1)
                first_line = lines[0].strip()
                if len(lines) > 1 and first_line.lower() in ['markdown', 'python', 'bash', 'sh', 'javascript', 'js', 'html', 'css', 'json']:
                     lang = first_line
                     code = lines[1]
            
            self.render_code_block(lang, code)
            last_pos = end
            
        # Render remaining text
        if last_pos < len(text):
            self.render_text_block(text[last_pos:])

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
            # Enable extensions for better parsing
            html_text = markdown.markdown(text, extensions=['fenced_code', 'tables'])
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

