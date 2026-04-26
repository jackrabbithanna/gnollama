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

from typing import Any, List, Dict, Optional, Union
from gi.repository import Adw, Gtk, Gio, GLib, Gdk, GObject
import threading
import json
import urllib.request
import re
import html
from .tab import GenerationTab
from .storage import ChatStorage
from .host_manager import HostManagerDialog
from .model_manager import ModelManagerDialog

@Gtk.Template(resource_path='/io/github/jackrabbithanna/Gnollama/history_row.ui')
class HistoryRow(Gtk.ListBoxRow):
    """A row in the chat history list."""
    __gtype_name__ = 'HistoryRow'

    chat_id = GObject.Property(type=str, default="")

    label: Gtk.Label = Gtk.Template.Child()
    edit_btn: Gtk.Button = Gtk.Template.Child()
    del_btn: Gtk.Button = Gtk.Template.Child()

    def __init__(self, chat_id: str, title: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.init_template()
        self.chat_id: str = chat_id
        self.label.set_text(title)

@Gtk.Template(resource_path='/io/github/jackrabbithanna/Gnollama/window.ui')
class GnollamaWindow(Adw.ApplicationWindow):
    """The main application window for Gnollama."""
    __gtype_name__ = 'GnollamaWindow'

    notebook: Gtk.Notebook = Gtk.Template.Child()
    history_list: Gtk.ListBox = Gtk.Template.Child()

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.init_template()
        self.settings: Gio.Settings = Gio.Settings.new('io.github.jackrabbithanna.Gnollama')
        self.storage: ChatStorage = ChatStorage()
        self.chat_rows: Dict[str, HistoryRow] = {}
        
        self.history_list.connect("row-activated", self.on_history_row_activated)
        
        # Setup actions
        self._setup_actions()
        
        # Load CSS
        self.load_css()
        
        # Load history
        self.load_history_sidebar()
        
        # Connect tab switching
        self.notebook.connect("switch-page", self.on_tab_switched)
        
        # Create initial tab
        self.new_chat_tab()

    def _setup_actions(self) -> None:
        """Initializes application actions and their shortcuts."""
        actions = [
            ("new_tab", self.on_new_tab),
            ("new_chat_tab", self.on_new_chat_tab),
            ("clear_history", self.on_clear_history),
            ("manage_hosts", self.on_manage_hosts),
            ("manage_models", self.on_manage_models)
        ]
        for name, callback in actions:
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            self.add_action(action)
        
    def load_css(self) -> None:
        """Loads application-wide CSS from resources."""
        css_provider = Gtk.CssProvider()
        css_provider.load_from_resource('/io/github/jackrabbithanna/Gnollama/style.css')
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            css_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        
    def on_clear_history(self, action: Gio.SimpleAction, param: Optional[GLib.Variant]) -> None:
        """Displays a confirmation dialog to clear all chat history."""
        dialog = Adw.MessageDialog(
            transient_for=self,
            heading=_("Clear chat history"),
            body=_("Are you sure you want to delete all chat history?")
        )
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("delete", _("Delete history"))
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        
        def on_response(dialog: Adw.MessageDialog, response: str) -> None:
            if response == "delete":
                pages_to_close = []
                for i in range(self.notebook.get_n_pages()):
                    page = self.notebook.get_nth_page(i)
                    if isinstance(page, GenerationTab) and hasattr(page.strategy, 'chat_id'):
                        pages_to_close.append(page)

                self.storage.clear_all_chats()
                self.load_history_sidebar()
                self.new_chat_tab()

                for page in pages_to_close:
                    self.close_tab(page)
            dialog.close()
            
        dialog.connect("response", on_response)
        dialog.present()

    def on_manage_hosts(self, action: Gio.SimpleAction, param: Optional[GLib.Variant]) -> None:
        """Opens the Host Manager dialog."""
        dialog = HostManagerDialog(storage=self.storage, on_hosts_changed_cb=self.on_hosts_changed)
        dialog.set_transient_for(self)
        dialog.present()

    def on_manage_models(self, action: Gio.SimpleAction, param: Optional[GLib.Variant]) -> None:
        """Opens the Model Manager dialog."""
        dialog = ModelManagerDialog(storage=self.storage)
        dialog.set_transient_for(self)
        dialog.present()

    def on_hosts_changed(self) -> None:
        """Callback when hosts configuration is updated."""
        n_pages = self.notebook.get_n_pages()
        for i in range(n_pages):
            page = self.notebook.get_nth_page(i)
            if hasattr(page, 'update_hosts'):
                page.update_hosts()

    def on_new_tab(self, action: Gio.SimpleAction, param: Optional[GLib.Variant]) -> None:
        """Action callback for creating a new generation tab."""
        self.new_tab()

    def on_new_chat_tab(self, action: Gio.SimpleAction, param: Optional[GLib.Variant]) -> None:
        """Action callback for creating a new chat tab."""
        self.new_chat_tab()
        
    def new_tab(self) -> None:
        """Creates and adds a new generation tab to the notebook."""
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
        
        page_num = self.notebook.append_page(tab, tab_label_box)
        self.notebook.set_menu_label_text(tab, tab_title.get_label())
        tab_title.connect("notify::label", lambda lbl, pspec, t=tab: self.notebook.page_num(t) != -1 and self.notebook.set_menu_label_text(t, lbl.get_label()))
        self.notebook.set_current_page(page_num)
        self.notebook.set_tab_reorderable(tab, True)
        self.notebook.set_tab_detachable(tab, True)
        
        close_button.connect("clicked", lambda btn: self.close_tab(tab))
        tab.set_visible(True)

    def load_history_sidebar(self) -> None:
        """Reloads the chat history list in the sidebar."""
        while True:
            row = self.history_list.get_first_child()
            if not row:
                break
            self.history_list.remove(row)
        self.chat_rows.clear()
            
        chats = self.storage.get_all_chats()
        for chat in chats:
            self.add_history_row(chat)

    def add_history_row(self, chat: Dict[str, Any]) -> None:
        """Adds a single row to the chat history list using the HistoryRow template."""
        chat_id = chat['id']
        row = HistoryRow(chat_id, chat.get('title', _('New Chat')))
        row.edit_btn.connect("clicked", self.on_edit_chat_clicked, chat_id, row, row.label)
        row.del_btn.connect("clicked", self.on_delete_chat_clicked, chat_id, row)
        self.history_list.append(row)
        self.chat_rows[chat_id] = row

    def on_delete_chat_clicked(self, btn: Gtk.Button, chat_id: str, row: Gtk.ListBoxRow) -> None:
        """Deletes a chat from storage and UI."""
        self.storage.delete_chat(chat_id)
        if chat_id in self.chat_rows:
            self.history_list.remove(self.chat_rows[chat_id])
            del self.chat_rows[chat_id]
        
        # Close matching tab if open
        n_pages = self.notebook.get_n_pages()
        for i in range(n_pages):
            page = self.notebook.get_nth_page(i)
            if isinstance(page, GenerationTab) and hasattr(page.strategy, 'chat_id') and page.strategy.chat_id == chat_id:
                self.close_tab(page)
                break

    def on_edit_chat_clicked(self, btn: Gtk.Button, chat_id: str, row: Gtk.ListBoxRow, label: Gtk.Label) -> None:
        """Opens a dialog to rename a chat."""
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
        
        def on_response(dialog: Adw.MessageDialog, response: str) -> None:
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

    def update_tab_title(self, chat_id: str, new_title: str) -> None:
        """Updates the title of an open tab matching a chat ID."""
        # Iterate pages to find matching chat
        n_pages = self.notebook.get_n_pages()
        for i in range(n_pages):
            page = self.notebook.get_nth_page(i)
            if isinstance(page, GenerationTab) and hasattr(page.strategy, 'chat_id') and page.strategy.chat_id == chat_id:
                if page.tab_label:
                    page.tab_label.set_label(new_title)
                break

    def on_history_row_activated(self, listbox: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        """Callback when a chat row is activated in the sidebar."""
        chat_id = getattr(row, 'chat_id', None)
        if chat_id:
            chat_data = self.storage.get_chat(chat_id)
            if chat_data:
                self.open_chat_tab(chat_data)

    def open_chat_tab(self, chat_data: Dict[str, Any]) -> None:
        """Opens an existing chat in a new or existing tab."""
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
        self.notebook.set_menu_label_text(tab, tab_title.get_label())
        tab_title.connect("notify::label", lambda lbl, pspec, t=tab: self.notebook.page_num(t) != -1 and self.notebook.set_menu_label_text(t, lbl.get_label()))
        self.notebook.set_current_page(page_num)
        self.notebook.set_tab_reorderable(tab, True)
        self.notebook.set_tab_detachable(tab, True)
        
        # Connect signals
        tab.connect("chat-updated", self.on_chat_updated)
        close_button.connect("clicked", lambda btn: self.close_tab(tab))
        tab.set_visible(True)

    def on_chat_updated(self, tab: GenerationTab, chat_id: str, new_title: str) -> None:
        """Updates the sidebar row when a chat's title changes."""
        if chat_id in self.chat_rows:
            self.chat_rows[chat_id].label.set_text(new_title)

    def new_chat_tab(self) -> None:
        """Creates a new empty chat session and adds its tab."""
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
        self.notebook.set_menu_label_text(tab, tab_title.get_label())
        tab_title.connect("notify::label", lambda lbl, pspec, t=tab: self.notebook.page_num(t) != -1 and self.notebook.set_menu_label_text(t, lbl.get_label()))
        self.notebook.set_current_page(page_num)
        self.notebook.set_tab_reorderable(tab, True)
        self.notebook.set_tab_detachable(tab, True)
        
        # Connect signals
        tab.connect("chat-updated", self.on_chat_updated)
        close_button.connect("clicked", lambda btn: self.close_tab(tab))
        
        # Show the tab
        tab.set_visible(True)

    def on_tab_switched(self, notebook: Gtk.Notebook, page: Gtk.Widget, page_num: int) -> None:
        """Syncs the sidebar selection with the active tab."""
        # Deselect first
        self.history_list.select_row(None)
        
        if isinstance(page, GenerationTab) and hasattr(page.strategy, 'chat_id') and page.strategy.chat_id:
            chat_id = page.strategy.chat_id
            
            # Find matching row using registry
            if chat_id in self.chat_rows:
                 self.history_list.select_row(self.chat_rows[chat_id])

    def close_tab(self, page: Gtk.Widget) -> None:
        """Closes a notebook tab and performs cleanups."""
        # Cleanup empty chats if they weren't used
        if isinstance(page, GenerationTab) and page.mode == 'chat' and hasattr(page.strategy, 'chat_id'):
            chat_id = page.strategy.chat_id
            if chat_id and hasattr(page.strategy, 'history') and not page.strategy.history:
                self.storage.delete_chat(chat_id)
                if chat_id in self.chat_rows:
                    self.history_list.remove(self.chat_rows[chat_id])
                    del self.chat_rows[chat_id]

        page_num = self.notebook.page_num(page)
        if page_num != -1:
            self.notebook.remove_page(page_num)
            # Workaround for GTK4 bug: force rebuild of popup menu to prevent layout assertions
            self.notebook.set_property("enable-popup", False)
            self.notebook.set_property("enable-popup", True)
