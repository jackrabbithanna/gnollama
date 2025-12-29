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
from .storage import ChatStorage

@Gtk.Template(resource_path='/io/github/jackrabbithanna/Gnollama/window.ui')
class GnollamaWindow(Adw.ApplicationWindow):
    __gtype_name__ = 'GnollamaWindow'

    notebook = Gtk.Template.Child()
    history_list = Gtk.Template.Child()

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.settings = Gio.Settings.new('io.github.jackrabbithanna.Gnollama')
        self.storage = ChatStorage()
        
        self.history_list.connect("row-activated", self.on_history_row_activated)
        
        # Setup actions
        action = Gio.SimpleAction.new("new_tab", None)
        action.connect("activate", self.on_new_tab)
        self.add_action(action)

        action_chat = Gio.SimpleAction.new("new_chat_tab", None)
        action_chat.connect("activate", self.on_new_chat_tab)
        self.add_action(action_chat)
        
        # Load CSS
        self.load_css()
        
        # Load history
        self.load_history_sidebar()
        
        # Connect tab switching
        self.notebook.connect("switch-page", self.on_tab_switched)
        
        # Create initial tab
        self.new_chat_tab()
        
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
        
        icon = Gtk.Image.new_from_icon_name("edit-find-symbolic")
        tab_label_box.append(icon)
        
        tab_title = Gtk.Label(label=_("New Response"))
        tab_label_box.append(tab_title)
        
        close_button = Gtk.Button.new_from_icon_name("window-close-symbolic")
        close_button.add_css_class("flat")
        close_button.set_valign(Gtk.Align.CENTER)
        tab_label_box.append(close_button)
        
        tab = GenerationTab(tab_title, mode='generate', storage=self.storage)
        
        # Add to notebook
        page_num = self.notebook.append_page(tab, tab_label_box)
        self.notebook.set_current_page(page_num)
        self.notebook.set_tab_reorderable(tab, True)
        self.notebook.set_tab_detachable(tab, True)
        
        # Connect close button
        close_button.connect("clicked", lambda btn: self.close_tab(tab))
        
        # Show the tab
        tab.set_visible(True)

        # Show the tab
        tab.set_visible(True)

    def load_history_sidebar(self):
        # Clear existing rows
        while True:
            row = self.history_list.get_first_child()
            if not row:
                break
            self.history_list.remove(row)
            
        chats = self.storage.get_all_chats()
        for chat in chats:
            self.add_history_row(chat)

    def add_history_row(self, chat):
        row = Gtk.ListBoxRow()
        row.chat_id = chat['id']
        
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_margin_top(8)
        box.set_margin_bottom(8)
        
        label = Gtk.Label(label=chat.get('title', _('New Chat')))
        label.set_halign(Gtk.Align.START)
        label.set_ellipsize(3) # PANGO_ELLIPSIZE_END
        label.set_hexpand(True)
        box.append(label)
        
        # Edit button
        edit_btn = Gtk.Button.new_from_icon_name("edit-paste-symbolic")
        edit_btn.add_css_class("flat")
        edit_btn.set_valign(Gtk.Align.CENTER)
        edit_btn.set_tooltip_text(_("Rename Chat"))
        edit_btn.connect("clicked", self.on_edit_chat_clicked, chat['id'], row, label)
        box.append(edit_btn)
        
        # Delete button
        del_btn = Gtk.Button.new_from_icon_name("user-trash-symbolic")
        del_btn.add_css_class("flat")
        del_btn.set_valign(Gtk.Align.CENTER)
        del_btn.set_tooltip_text(_("Delete Chat"))
        del_btn.connect("clicked", self.on_delete_chat_clicked, chat['id'], row)
        box.append(del_btn)
        
        row.set_child(box)
        self.history_list.append(row)

    def on_delete_chat_clicked(self, btn, chat_id, row):
        # Confirm dialog could be here, but for now direct delete
        self.storage.delete_chat(chat_id)
        self.history_list.remove(row)
        
        # Close matching tab if open
        n_pages = self.notebook.get_n_pages()
        for i in range(n_pages):
            page = self.notebook.get_nth_page(i)
            if isinstance(page, GenerationTab) and hasattr(page.strategy, 'chat_id') and page.strategy.chat_id == chat_id:
                self.close_tab(page)
                break

    def on_edit_chat_clicked(self, btn, chat_id, row, label):
        # Create a simple dialog for renaming
        dialog = Adw.MessageDialog(
            transient_for=self,
            heading=_("Rename Chat"),
            body=_("Enter a new title for this chat.")
        )
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("save", _("Save"))
        dialog.set_default_response("save")
        dialog.set_close_response("cancel")
        
        # Add entry
        entry = Gtk.Entry()
        entry.set_text(label.get_text())
        entry.set_activates_default(True)
        dialog.set_extra_child(entry)
        
        def on_response(dialog, response):
            if response == "save":
                new_title = entry.get_text().strip()
                if new_title:
                    self.storage.update_title(chat_id, new_title)
                    label.set_text(new_title)
                    # Update active tab if open
                    self.update_tab_title(chat_id, new_title)
            dialog.close()
            
        dialog.connect("response", on_response)
        dialog.present()

    def update_tab_title(self, chat_id, new_title):
        # Iterate pages to find matching chat
        n_pages = self.notebook.get_n_pages()
        for i in range(n_pages):
            page = self.notebook.get_nth_page(i)
            if isinstance(page, GenerationTab) and hasattr(page.strategy, 'chat_id') and page.strategy.chat_id == chat_id:
                if page.tab_label:
                    page.tab_label.set_label(new_title)
                break

    def on_history_row_activated(self, listbox, row):
        chat_id = row.chat_id
        chat_data = self.storage.get_chat(chat_id)
        if chat_data:
            self.open_chat_tab(chat_data)

    def open_chat_tab(self, chat_data):
        # Check if already open
        chat_id = chat_data['id']
        n_pages = self.notebook.get_n_pages()
        for i in range(n_pages):
            page = self.notebook.get_nth_page(i)
            if isinstance(page, GenerationTab) and hasattr(page.strategy, 'chat_id') and page.strategy.chat_id == chat_id:
                self.notebook.set_current_page(i)
                return
        
        tab_label_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        icon = Gtk.Image.new_from_icon_name("network-server-symbolic")
        tab_label_box.append(icon)
        
        title = chat_data.get('title', _('Chat'))
        tab_title = Gtk.Label(label=title)
        tab_label_box.append(tab_title)
        
        close_button = Gtk.Button.new_from_icon_name("window-close-symbolic")
        close_button.add_css_class("flat")
        close_button.set_valign(Gtk.Align.CENTER)
        tab_label_box.append(close_button)
        
        # Create tab with existing data
        tab = GenerationTab(
            tab_title, 
            mode='chat', 
            chat_id=chat_data['id'],
            initial_history=chat_data.get('messages', []),
            storage=self.storage
        )
        
        # Add to notebook
        page_num = self.notebook.append_page(tab, tab_label_box)
        self.notebook.set_current_page(page_num)
        self.notebook.set_tab_reorderable(tab, True)
        self.notebook.set_tab_detachable(tab, True)
        
        # Connect signals
        tab.connect("chat-updated", self.on_chat_updated)
        close_button.connect("clicked", lambda btn: self.close_tab(tab))
        tab.set_visible(True)

    def on_chat_updated(self, tab, chat_id, new_title):
        # Find row and update label
        row = self.history_list.get_first_child()
        while row:
            if hasattr(row, 'chat_id') and row.chat_id == chat_id:
                # The label is the first child of the box, which is the child of the row
                box = row.get_child()
                if box:
                    label = box.get_first_child()
                    if isinstance(label, Gtk.Label):
                        label.set_text(new_title)
                break
            row = row.get_next_sibling()

    def new_chat_tab(self):
        # Create new chat in storage
        chat_data = self.storage.create_chat()
        
        # Add to sidebar
        self.add_history_row(chat_data)
        
        # Create tab label widget
        tab_label_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        
        icon = Gtk.Image.new_from_icon_name("network-server-symbolic")
        tab_label_box.append(icon)
        
        tab_title = Gtk.Label(label=_("New Chat"))
        tab_label_box.append(tab_title)
        
        close_button = Gtk.Button.new_from_icon_name("window-close-symbolic")
        close_button.add_css_class("flat")
        close_button.set_valign(Gtk.Align.CENTER)
        tab_label_box.append(close_button)
        
        tab = GenerationTab(tab_title, mode='chat', chat_id=chat_data['id'], storage=self.storage)
        
        # Add to notebook
        page_num = self.notebook.append_page(tab, tab_label_box)
        self.notebook.set_current_page(page_num)
        self.notebook.set_tab_reorderable(tab, True)
        self.notebook.set_tab_detachable(tab, True)
        
        # Connect signals
        tab.connect("chat-updated", self.on_chat_updated)
        close_button.connect("clicked", lambda btn: self.close_tab(tab))
        
        # Show the tab
        tab.set_visible(True)

    def on_tab_switched(self, notebook, page, page_num):
        # Deselect first
        self.history_list.select_row(None)
        
        if isinstance(page, GenerationTab) and hasattr(page.strategy, 'chat_id') and page.strategy.chat_id:
            chat_id = page.strategy.chat_id
            
            # Find matching row
            row = self.history_list.get_first_child()
            while row:
                if hasattr(row, 'chat_id') and row.chat_id == chat_id:
                     self.history_list.select_row(row)
                     break
                row = row.get_next_sibling()

    def close_tab(self, page):
        page_num = self.notebook.page_num(page)
        if page_num != -1:
            self.notebook.remove_page(page_num)
