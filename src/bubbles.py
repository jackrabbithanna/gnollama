# bubbles.py
#
# Copyright 2025 Jackrabbithanna
#
# SPDX-License-Identifier: GPL-3.0-or-later

from gi.repository import Gtk, GObject, Pango, GLib
from .markdown_view import MarkdownView

class ChatBubble(Gtk.ListBoxRow):
    __gtype_name__ = 'ChatBubble'

    def __init__(self, is_user=False, **kwargs):
        super().__init__(**kwargs)
        self.set_activatable(False)
        self.set_selectable(False)
        
        self.container = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        self.container.set_margin_top(6)
        self.container.set_margin_bottom(6)
        self.container.set_margin_start(12)
        self.container.set_margin_end(12)
        
        self.bubble_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        # self.bubble_box.set_spacing(4)
        
        if is_user:
            self.container.set_halign(Gtk.Align.END)
            self.bubble_box.add_css_class("user-bubble")
            self.bubble_box.set_halign(Gtk.Align.END)
        else:
            self.container.set_halign(Gtk.Align.START)
            self.bubble_box.add_css_class("bot-bubble")
            self.bubble_box.set_halign(Gtk.Align.START)
            self.bubble_box.set_hexpand(True) # Robot messages usually need more width
            
        self.container.append(self.bubble_box)
        self.set_child(self.container)

class UserBubble(ChatBubble):
    __gtype_name__ = 'UserBubble'

    def __init__(self, text, **kwargs):
        super().__init__(is_user=True, **kwargs)
        
        label = Gtk.Label(label=text)
        label.set_wrap(True)
        label.set_max_width_chars(50)
        label.set_xalign(0)
        label.set_selectable(True)
        
        self.bubble_box.append(label)

class AiBubble(ChatBubble):
    __gtype_name__ = 'AiBubble'

    def __init__(self, model_name=None, **kwargs):
        super().__init__(is_user=False, **kwargs)
        
        # Header
        if model_name:
            header = Gtk.Label(label=f"Ollama ({model_name})")
            header.add_css_class("caption-heading") # Custom class or standard dim-label
            header.set_halign(Gtk.Align.START)
            header.set_margin_bottom(4)
            self.bubble_box.append(header)
        
        # Thinking Expander
        self.thinking_expander = Gtk.Expander(label="Thinking...")
        self.thinking_expander.set_visible(False)
        self.thinking_label = Gtk.Label()
        self.thinking_label.set_wrap(True)
        self.thinking_label.set_xalign(0)
        self.thinking_label.add_css_class("thinking-text")
        self.thinking_expander.set_child(self.thinking_label)
        self.bubble_box.append(self.thinking_expander)
        
        # Markdown Content
        self.markdown_view = MarkdownView()
        self.bubble_box.append(self.markdown_view)
        
        self.full_text = ""
        self.thinking_text = ""
        self._update_scheduled = False

    def append_text(self, text):
        self.full_text += text
        
        # Throttle updates to avoid freezing the UI with excessive re-renders
        if not self._update_scheduled:
            self._update_scheduled = True
            GLib.timeout_add(50, self._flush_update)
            
    def _flush_update(self):
        # Check if widget is still valid
        if not self.get_root():
             self._update_scheduled = False
             return False

        self.markdown_view.update(self.full_text)
        self._update_scheduled = False
        return False
        
    def append_thinking(self, text):
        if not self.thinking_expander.get_visible():
            self.thinking_expander.set_visible(True)
        
        self.thinking_text += text
        self.thinking_label.set_label(self.thinking_text)
