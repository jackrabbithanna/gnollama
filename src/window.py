# window.py
#
# Copyright 2025 Jackrabbithanna
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# SPDX-License-Identifier: GPL-3.0-or-later

from gi.repository import Adw, Gtk, Gio, GLib, Gdk
import threading
import json
import urllib.request
import re
import html
from .tab import GenerationTab

@Gtk.Template(resource_path='/com/github/jackrabbithanna/Gnollama/window.ui')
class GnollamaWindow(Adw.ApplicationWindow):
    __gtype_name__ = 'GnollamaWindow'

    notebook = Gtk.Template.Child()

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.settings = Gio.Settings.new('com.github.jackrabbithanna.Gnollama')
        
        # Setup actions
        action = Gio.SimpleAction.new("new_tab", None)
        action.connect("activate", self.on_new_tab)
        self.add_action(action)

        action_chat = Gio.SimpleAction.new("new_chat_tab", None)
        action_chat.connect("activate", self.on_new_chat_tab)
        self.add_action(action_chat)
        
        # Load CSS
        self.load_css()
        
        # Create initial tab
        self.new_tab()
        
    def load_css(self):
        css_provider = Gtk.CssProvider()
        css = """
        .user-bubble {
            background-color: @accent_bg_color;
            color: @accent_fg_color;
            border-radius: 15px;
            padding: 10px;
            margin-bottom: 5px;
        }
        .bot-bubble {
            background-color: alpha(@window_fg_color, 0.05);
            border-radius: 15px;
            padding: 10px;
            margin-bottom: 5px;
        }
        .thinking-text {
            color: alpha(@window_fg_color, 0.6);
            font-style: italic;
        }
        .dim-label {
            opacity: 0.7;
            font-size: smaller;
            margin-bottom: 2px;
        }
        tab {
            border: 1px solid alpha(@window_fg_color, 0.1);
            border-bottom: none;
            border-radius: 6px 6px 0 0;
            margin: 0 2px;
            padding: 4px 8px;
        }
        """
        css_provider.load_from_data(css.encode('utf-8'))
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            css_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        
    def on_new_tab(self, action, param):
        self.new_tab()

    def on_new_chat_tab(self, action, param):
        self.new_chat_tab()
        
    def new_tab(self):
        # Create tab label widget
        tab_label_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        tab_title = Gtk.Label(label="New Response")
        tab_label_box.append(tab_title)
        
        close_button = Gtk.Button.new_from_icon_name("window-close-symbolic")
        close_button.add_css_class("flat")
        close_button.set_valign(Gtk.Align.CENTER)
        tab_label_box.append(close_button)
        
        tab = GenerationTab(tab_title, mode='generate')
        
        # Add to notebook
        page_num = self.notebook.append_page(tab, tab_label_box)
        self.notebook.set_current_page(page_num)
        self.notebook.set_tab_reorderable(tab, True)
        self.notebook.set_tab_detachable(tab, True)
        
        # Connect close button
        close_button.connect("clicked", lambda btn: self.close_tab(tab))
        
        # Show the tab
        tab.set_visible(True)

    def new_chat_tab(self):
        # Create tab label widget
        tab_label_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        tab_title = Gtk.Label(label="New Chat")
        tab_label_box.append(tab_title)
        
        close_button = Gtk.Button.new_from_icon_name("window-close-symbolic")
        close_button.add_css_class("flat")
        close_button.set_valign(Gtk.Align.CENTER)
        tab_label_box.append(close_button)
        
        tab = GenerationTab(tab_title, mode='chat')
        
        # Add to notebook
        page_num = self.notebook.append_page(tab, tab_label_box)
        self.notebook.set_current_page(page_num)
        self.notebook.set_tab_reorderable(tab, True)
        self.notebook.set_tab_detachable(tab, True)
        
        # Connect close button
        close_button.connect("clicked", lambda btn: self.close_tab(tab))
        
        # Show the tab
        tab.set_visible(True)

    def close_tab(self, page):
        page_num = self.notebook.page_num(page)
        if page_num != -1:
            self.notebook.remove_page(page_num)
