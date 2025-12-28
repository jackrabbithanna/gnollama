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
                    if clean_lang.lower() in ['markdown', 'md', 'python', 'py', 'bash', 'sh', 'javascript', 'js', 'html', 'css', 'json', 'xml', 'sql', 'java', 'c', 'cpp', 'go', 'rs', 'rust']:
                        lang = clean_lang
                        content_start_idx += 1 # Skip the lang line
                
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

