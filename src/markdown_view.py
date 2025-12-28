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
    import gi
    gi.require_version('GtkSource', '5')
    from gi.repository import GtkSource
except (ImportError, ValueError):
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
        # Parse content into blocks
        blocks = self._parse_blocks(self._text)
        self._sync_view(blocks)

    def _parse_blocks(self, text):
        """
        Parses text into a list of block dicts:
        [{'type': 'code', 'lang': '...', 'content': '...'}, {'type': 'text', 'content': '...'}]
        """
        blocks = []
        lines = text.split('\n')
        i = 0
        n = len(lines)
        
        while i < n:
            line = lines[i]
            
            # Check for fence start
            match = re.match(r'^(\s*)(`{3,}|~{3,})(.*)$', line)
            
            if match:
                # Start of code block
                indent, fence, raw_lang = match.groups()
                lang = raw_lang.strip()
                
                # Check for "Orphaned Lang"
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
                
                # Consume lines until closing fence
                code_lines = []
                i = content_start_idx
                while i < n:
                    curr_line = lines[i]
                    close_match = re.match(r'^(\s*)(`{3,}|~{3,})\s*$', curr_line)
                    if close_match:
                        c_indent, c_fence = close_match.groups()
                        if c_fence[0] == fence[0] and len(c_fence) >= len(fence):
                            i += 1 # Consume closing fence
                            break
                    
                    code_lines.append(curr_line)
                    i += 1
                
                # Recursion for nested markdown
                if lang.lower() in ['markdown', 'md']:
                    # Recursively parse the inner markdown content
                    inner_blocks = self._parse_blocks("\n".join(code_lines))
                    blocks.extend(inner_blocks)
                else:
                    blocks.append({
                        'type': 'code',
                        'lang': lang,
                        'content': "\n".join(code_lines)
                    })
                continue
            
            # Text block
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

    def _sync_view(self, blocks):
        """
        Syncs the Gtk widget list with the parsed blocks.
        """
        child = self.get_first_child()
        
        for block in blocks:
            # Try to reuse existing child
            if child:
                # Check if compatible
                is_code = child.has_css_class("code-block")
                is_text = isinstance(child, Gtk.Label)
                
                if block['type'] == 'code' and is_code:
                    # Update code block
                    self._update_code_block(child, block['lang'], block['content'])
                    child = child.get_next_sibling()
                    continue
                elif block['type'] == 'text' and is_text:
                    # Update text block
                    self._update_text_block(child, block['content'])
                    child = child.get_next_sibling()
                    continue
                else:
                    # Mismatch, need to insert new
                    # For now, simplest robust strategy is:
                    # If mismatch, remove current 'child' and insert new one
                    # But pure append is safer for now if we assume purely additive structure?
                    # No, structure can change.
                    
                    # Replacement strategy:
                    new_widget = self._create_widget_for_block(block)
                    self.insert_child_after(new_widget, child.get_prev_sibling())
                    
                    # The 'child' is now effectively "next" after the one we inserted?
                    # No, logic is tricky. 
                    # Simpler: Remove current mismatching child, loop will create new.
                    next_child = child.get_next_sibling()
                    self.remove(child)
                    child = new_widget # This is the one we just added
                    
                    # Advance
                    child = next_child # Process the *next* old child against *next* block? 
                    # Actually if we inserted `new_widget` before `child`...
                    # Gtk4 doesn't have `insert_before`. `insert_child_after`.
                    
                    # Retry:
                    # Since Gtk list manipulation mid-loop is hard, 
                    # let's use the standard "Diff" approach mapping:
                    # But simpler: Rebuild if mismatch.
                    pass 
            
            # If we fall through here (logic above was tricky), use brute force
            # Create new widget
            if child:
                 # Mismatch case handling was hard above. 
                 # Let's resort to: if mismatch, destroy from here on?
                 # Or just remove mismatching child.
                 
                 # Let's simplify:
                 # If we are here, we either had no child, or we chose not to reuse it.
                 pass

        # RE-WRITE OF SYNC LOOP FOR STABILITY
        # Let's step back.
        child = self.get_first_child()
        
        for block in blocks:
            matched = False
            if child:
                is_code = child.has_css_class("code-block")
                is_text = isinstance(child, Gtk.Label) # Text blocks are bare Labels
                
                if block['type'] == 'code' and is_code:
                    # Check if lang matches? Usually keep same widget
                    self._update_code_block(child, block['lang'], block['content'])
                    matched = True
                elif block['type'] == 'text' and is_text:
                    self._update_text_block(child, block['content'])
                    matched = True
            
            if matched:
                child = child.get_next_sibling()
            else:
                # No match or no child.
                # If there was a child but it didn't match, we should remove it?
                # Case: text -> text, code. 
                # Old: text. New: text, code.
                # 1. match text. child = None. 
                # 2. block code. child=None. Insert.
                
                # Case: text -> code.
                # Old: text. New: code.
                # 1. block code. child=text. Mismatch.
                # Remove child?
                if child:
                    next_s = child.get_next_sibling()
                    self.remove(child)
                    child = next_s
                    # Now try current block again against next child?
                    # Recursion? No, just Loop.
                    # But we are inside a for loop iterating blocks.
                    # If we remove child, we haven't processed this block yet.
                    
                    # Correction: If mismatch, insert NEW widget before current child (if possible)
                    # or just append if we are at end.
                    # Gtk4: `insert_child_after(child, sibling)`. Sibling=None -> prepend.
                    
                    # Easiest: If mismatch, remove old child. create new widget.
                    # This causes flashing if we delete-then-add.
                    # Better: Insert new widget `before` old child?
                    # Gtk4 Insert After Prev Sibling.
                    
                    new_widget = self._create_widget_for_block(block)
                    # Insert after partial's previous
                    # We need to track `prev_widget`.
                    pass
        
        # FINAL ATTEMPT AT CLEAN LOOP
        curr_child = self.get_first_child()
        prev_child = None
        
        for block in blocks:
            # Check if curr_child matches block type
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
            
            if match:
                # Move forward
                prev_child = curr_child
                curr_child = curr_child.get_next_sibling()
            else:
                # Mismatch or End of List
                # If mismatch, we assume the structure changed significantly or we are inserting.
                # If we have a curr_child, it's the wrong type.
                # Strategy: Destroy curr_child and replace.
                # This might cause flash if type flipped. But usually streaming ONLY Appends.
                # So usually curr_child is None.
                
                if curr_child:
                    # Structure changed (e.g. text -> code transition).
                    # Remove the wrong child.
                    next_s = curr_child.get_next_sibling()
                    self.remove(curr_child)
                    curr_child = next_s
                
                # Create and insert
                new_widget = self._create_widget_for_block(block)
                self.insert_child_after(new_widget, prev_child)
                prev_child = new_widget
                # curr_child stays as is (next one)
                
        # Remove any remaining children (truncation)
        while curr_child:
            next_s = curr_child.get_next_sibling()
            self.remove(curr_child)
            curr_child = next_s


    def _create_widget_for_block(self, block):
        if block['type'] == 'text':
             label = Gtk.Label()
             label.set_wrap(True)
             label.set_xalign(0)
             label.set_selectable(True)
             self._update_text_block(label, block['content'])
             return label
        else: # code
             return self._create_code_widget(block['lang'], block['content'])

    def _create_code_widget(self, lang, code):
        wrapper = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        wrapper.add_css_class("code-block")
        wrapper.set_margin_top(6)
        wrapper.set_margin_bottom(6)
        
        # Code View
        if GtkSource:
            buffer = GtkSource.Buffer()
            lm = GtkSource.LanguageManager.get_default()
            language = lm.get_language(lang)
            if language:
                buffer.set_language(language)
            
            sm = GtkSource.StyleSchemeManager.get_default()
            scheme = sm.get_scheme("oblivion") 
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
        
        # Initial content
        view.get_buffer().set_text(code)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_child(view)
        scrolled.set_propagate_natural_height(True)
        scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        
        wrapper.append(scrolled)
        return wrapper

    def _update_text_block(self, label, text):
        if not text.strip():
            label.set_markup("")
            return
        
        try:
             # Existing render logic
            text = re.sub(r'~~(.*?)~~', r'<s>\1</s>', text)
            html_text = markdown.markdown(text, extensions=['extra', 'fenced_code'])
            parser = PangoMarkupParser()
            parser.feed(html_text)
            markup = parser.get_markup()
        except Exception:
            markup = html.escape(text)
            
        label.set_markup(markup)

    def _update_code_block(self, wrapper, lang, code):
        # Wrapper -> ScrolledWindow -> View
        scrolled = wrapper.get_first_child()
        view = scrolled.get_child()
        buffer = view.get_buffer()
        
        # Only update if text changed to avoid cursor jumps?
        # Actually set_text is fine on non-editable.
        start, end = buffer.get_bounds()
        current = buffer.get_text(start, end, True)
        if current != code:
             buffer.set_text(code)

