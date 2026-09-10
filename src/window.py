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

from gi.repository import Adw, Gtk, Gio, GLib, Gdk, GObject
from .tab import GenerationTab
from .session import ChatStrategy
from .storage import ChatStorage
from .host_manager import HostManagerDialog
from .model_manager import ModelManagerDialog


@Gtk.Template(resource_path='/io/github/jackrabbithanna/Gnollama/window.ui')
class GnollamaWindow(Adw.ApplicationWindow):
    __gtype_name__ = 'GnollamaWindow'

    tab_view = Gtk.Template.Child()
    history_sidebar = Gtk.Template.Child()
    split_view = Gtk.Template.Child()
    sidebar_toggle = Gtk.Template.Child()

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        icon_theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
        icon_theme.add_resource_path('/io/github/jackrabbithanna/Gnollama/icons')
        self.settings = Gio.Settings.new('io.github.jackrabbithanna.Gnollama')
        self.storage = ChatStorage()
        self.storage.on_error = self._on_save_error
        self._shutting_down = False
        self._allow_close = False
        self._cleanup_future = None
        self._save_error_dialog = None
        self.chat_rows = {}
        self.model_managers = []
        self._setup_actions()
        self._sidebar_menu = Gio.Menu()
        self.history_sidebar.set_menu_model(self._sidebar_menu)
        self.history_sidebar.connect('setup-menu', self._setup_sidebar_menu)
        self.history_sidebar.connect('activated', self.on_history_activated)
        self.tab_view.connect('notify::selected-page', self.on_tab_switched)
        self.tab_view.connect('close-page', self._on_close_page)
        self.connect('close-request', self.on_close_request)
        self.split_view.bind_property('show-sidebar', self.sidebar_toggle, 'active',
                                      GObject.BindingFlags.BIDIRECTIONAL | GObject.BindingFlags.SYNC_CREATE)
        self.load_css()
        self.load_history_sidebar()
        self.new_chat_tab()

    def tabs(self):
        return [self.tab_view.get_nth_page(i).get_child() for i in range(self.tab_view.get_n_pages())]

    def on_close_request(self, *args):
        if self._allow_close:
            return False
        self.request_shutdown()
        return True

    def request_shutdown(self):
        if self._shutting_down:
            return
        self._shutting_down = True
        self.tab_view.set_sensitive(False)
        self.history_sidebar.set_sensitive(False)
        from . import ollama
        ollama.cancel_all()
        for tab in self.tabs():
            tab.chat_input.cancel_fetches()
            if tab.options_panel._schema_dialog is not None:
                tab.options_panel._schema_dialog.close()
            if tab.request:
                tab.request.cancellable.cancel()
        for window in list(Gtk.Window.list_toplevels()):
            parent = window.get_transient_for()
            while parent is not None and parent is not self:
                parent = parent.get_transient_for()
            if parent is self:
                window.close()
        GLib.timeout_add(50, self._poll_shutdown)

    def _poll_shutdown(self):
        if not self._shutting_down:
            return False
        from .session import worker
        if not worker.idle or any(tab.request for tab in self.tabs()):
            return True
        if self.storage.writer.error is not None:
            self._on_save_error(self.storage.writer.error)
            return True
        if self._cleanup_future is None:
            self._cleanup_future = self.storage.cleanup_empty_chats()
        if not self.storage.writer.idle:
            return True
        self.storage.writer.shutdown()
        worker.shutdown(wait=False)
        self._allow_close = True
        self.close()
        return False

    def _on_save_error(self, error):
        if self._save_error_dialog is not None or self._allow_close:
            return
        dialog = Adw.AlertDialog(heading=_('History could not be saved'),
                                 body=_('Your unsaved changes are kept in memory. Retry saving before quitting.') + '\n\n' + str(error))
        self._save_error_dialog = dialog
        dialog.add_response('keep', _('Keep Open'))
        dialog.add_response('retry', _('Retry'))
        dialog.set_default_response('retry')
        dialog.set_close_response('keep')
        def response(dialog, choice):
            self._save_error_dialog = None
            if choice == 'retry':
                self.storage.writer.retry()
            elif self._shutting_down:
                from . import ollama
                self._shutting_down = False
                self._cleanup_future = None
                self.tab_view.set_sensitive(True)
                self.history_sidebar.set_sensitive(True)
                ollama.resume()
                for tab in self.tabs():
                    if not tab.closing:
                        tab.on_host_changed()
        dialog.connect('response', response)
        dialog.present(self)

    def _setup_actions(self):
        actions = [('new_tab', lambda *args: self.new_tab()),
                   ('new_chat_tab', lambda *args: self.new_chat_tab()),
                   ('clear_history', self.on_clear_history),
                   ('manage_hosts', self.on_manage_hosts),
                   ('manage_models', self.on_manage_models),
                   ('close_tab', self.close_selected_tab),
                   ('next_tab', lambda *args: self.tab_view.select_next_page()),
                   ('previous_tab', lambda *args: self.tab_view.select_previous_page()),
                   ('toggle_sidebar', lambda *args: self.split_view.set_show_sidebar(not self.split_view.get_show_sidebar()))]
        for name, callback in actions:
            action = Gio.SimpleAction.new(name, None)
            action.connect('activate', lambda action, param, cb=callback: cb(action, param) if not self._shutting_down else None)
            self.add_action(action)
        for name, callback in [('history_pin', self.pin_chat), ('history_rename', self.rename_chat), ('history_delete', self.delete_chat)]:
            action = Gio.SimpleAction.new(name, GLib.VariantType.new('s'))
            action.connect('activate', lambda a, p, cb=callback: cb(p.get_string()) if not self._shutting_down else None)
            self.add_action(action)

    def load_css(self):
        provider = Gtk.CssProvider()
        provider.load_from_resource('/io/github/jackrabbithanna/Gnollama/style.css')
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def on_clear_history(self, *args):
        dialog = Adw.AlertDialog(heading=_('Clear chat history'), body=_('Are you sure you want to delete all chat history?'))
        dialog.add_response('cancel', _('Cancel'))
        dialog.add_response('delete', _('Delete history'))
        dialog.set_response_appearance('delete', Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response('cancel')
        dialog.set_close_response('cancel')
        def response(dialog, choice):
            if choice == 'delete' and not self._shutting_down:
                for tab in self.tabs():
                    if isinstance(tab.strategy, ChatStrategy):
                        self.close_tab(tab, delete=True)
                self.storage.clear_all_chats()
                self.new_chat_tab()
                self.storage._submit(lambda: None, on_done=self.load_history_sidebar)
        dialog.connect('response', response)
        dialog.present(self)

    def on_manage_hosts(self, *args):
        dialog = HostManagerDialog(storage=self.storage, on_hosts_changed_cb=self.on_hosts_changed)
        dialog.set_transient_for(self)
        dialog.present()

    def on_manage_models(self, *args):
        dialog = ModelManagerDialog(storage=self.storage, is_model_busy=self.is_model_busy)
        self.model_managers.append(dialog)
        dialog.connect('close-request', lambda *args: self._forget_manager(dialog))
        dialog.set_transient_for(self)
        dialog.present()

    def _forget_manager(self, dialog):
        if dialog in self.model_managers:
            self.model_managers.remove(dialog)
        return False

    def is_model_busy(self, host, model):
        from .model_manager import model_key
        key = model_key(host, model)
        return any(tab.request and model_key(tab.request.settings['host'], tab.request.settings['model']) == key
                   for tab in self.tabs())

    def on_hosts_changed(self):
        for tab in self.tabs():
            tab.update_hosts()
        for manager in self.model_managers:
            manager.update_hosts()

    def _add_tab(self, tab):
        page = self.tab_view.append(tab)
        tab.bind_property('title', page, 'title', GObject.BindingFlags.SYNC_CREATE)
        page.set_icon(Gio.ThemedIcon.new('network-server-symbolic' if tab.mode == 'chat' else 'edit-find-symbolic'))
        def request_changed(*args):
            page.set_loading(tab.request is not None)
            for manager in self.model_managers:
                manager.update_unload_buttons()
        tab.connect('request-changed', request_changed)
        tab.connect('chat-updated', self.on_chat_updated)
        self.tab_view.set_selected_page(page)
        return tab

    def new_tab(self):
        return self._add_tab(GenerationTab(mode='generate', storage=self.storage))

    def new_chat_tab(self):
        chat = self.storage.create_chat()
        self.add_history_row(chat, prepend=True)
        return self._add_tab(GenerationTab(mode='chat', chat_id=chat['id'], storage=self.storage))

    def open_chat_tab(self, chat_data):
        for tab in self.tabs():
            if getattr(tab.strategy, 'chat_id', None) == chat_data['id']:
                self.tab_view.set_selected_page(self.tab_view.get_page(tab))
                return tab
        return self._add_tab(GenerationTab(mode='chat', chat_id=chat_data['id'],
                                           initial_history=chat_data.get('messages', []), storage=self.storage))

    def load_history_sidebar(self):
        if self._allow_close:
            return
        self.history_sidebar.remove_all()
        self.chat_rows.clear()
        self.pinned_section = Adw.SidebarSection(title=_('Pinned'))
        self.recent_section = Adw.SidebarSection(title=_('Recent'))
        self.history_sidebar.append(self.pinned_section)
        self.history_sidebar.append(self.recent_section)
        for chat in self.storage.get_all_chats():
            self.add_history_row(chat)
        self.on_tab_switched()

    def _fill_history_menu(self, menu, item):
        menu.remove_all()
        for label, action in [(_('Unpin Chat') if item.is_pinned else _('Pin Chat'), 'history_pin'),
                              (_('Rename Chat'), 'history_rename'), (_('Delete Chat'), 'history_delete')]:
            entry = Gio.MenuItem.new(label, None)
            entry.set_action_and_target_value('win.' + action, GLib.Variant('s', item.chat_id))
            menu.append_item(entry)

    def _setup_sidebar_menu(self, sidebar, item):
        if item is not None:
            self._fill_history_menu(self._sidebar_menu, item)

    def add_history_row(self, chat, prepend=False):
        item = Adw.SidebarItem(title=chat.get('title', _('New Chat')), icon_name='chat-message-new-symbolic')
        item.chat_id = chat['id']
        item.is_pinned = chat.get('is_pinned', False)
        item.set_tooltip(item.get_title())
        menu = Gio.Menu()
        self._fill_history_menu(menu, item)
        button = Gtk.MenuButton(icon_name='view-more-symbolic', menu_model=menu, tooltip_text=_('Chat actions'))
        button.add_css_class('flat')
        item.set_suffix(button)
        section = self.pinned_section if item.is_pinned else self.recent_section
        (section.prepend if prepend else section.append)(item)
        self.chat_rows[item.chat_id] = item

    def _remove_history_item(self, chat_id):
        item = self.chat_rows.pop(chat_id, None)
        if item and item.get_section():
            item.get_section().remove(item)

    def on_history_activated(self, sidebar, index):
        item = sidebar.get_item(index)
        if item:
            chat = self.storage.get_chat(item.chat_id)
            if chat:
                self.open_chat_tab(chat)
                if self.split_view.get_collapsed():
                    self.split_view.set_show_sidebar(False)

    def on_tab_switched(self, *args):
        page = self.tab_view.get_selected_page()
        chat_id = getattr(page.get_child().strategy, 'chat_id', None) if page else None
        item = self.chat_rows.get(chat_id)
        self.history_sidebar.set_selected(item.get_index() if item else Gtk.INVALID_LIST_POSITION)

    def on_chat_updated(self, tab, chat_id, title):
        self.update_tab_title(chat_id, title)
        self.load_history_sidebar()

    def update_tab_title(self, chat_id, title):
        for tab in self.tabs():
            if getattr(tab.strategy, 'chat_id', None) == chat_id:
                tab.title = title

    def pin_chat(self, chat_id):
        item = self.chat_rows.get(chat_id)
        if item:
            self.storage.update_chat_pinned(chat_id, not item.is_pinned, on_done=self.load_history_sidebar)

    def rename_chat(self, chat_id):
        item = self.chat_rows.get(chat_id)
        if not item:
            return
        dialog = Adw.AlertDialog(heading=_('Rename Chat'), body=_('Enter a new title for this chat.'))
        entry = Gtk.Entry(text=item.get_title(), activates_default=True)
        dialog.set_extra_child(entry)
        dialog.add_response('cancel', _('Cancel'))
        dialog.add_response('save', _('Save'))
        dialog.set_default_response('save')
        dialog.set_close_response('cancel')
        def response(dialog, choice):
            title = entry.get_text().strip()
            if choice == 'save' and title and not self._shutting_down:
                def saved():
                    self.update_tab_title(chat_id, title)
                    self.load_history_sidebar()
                self.storage.update_title(chat_id, title, on_done=saved)
        dialog.connect('response', response)
        dialog.present(self)

    def delete_chat(self, chat_id):
        for tab in self.tabs():
            if getattr(tab.strategy, 'chat_id', None) == chat_id:
                self.close_tab(tab, delete=True)
        self.storage.delete_chat(chat_id, on_done=self.load_history_sidebar)
        self._remove_history_item(chat_id)

    def close_selected_tab(self, *args):
        page = self.tab_view.get_selected_page()
        if page:
            self.tab_view.close_page(page)

    def close_tab(self, tab, delete=False):
        if delete and isinstance(tab.strategy, ChatStrategy):
            tab.strategy.deleted = True
        if not tab.closing:
            self.tab_view.close_page(self.tab_view.get_page(tab))

    def _on_close_page(self, view, page):
        tab = page.get_child()
        if self._shutting_down:
            view.close_page_finish(page, False)
            return True
        if tab.closing:
            return True
        def remove():
            if isinstance(tab.strategy, ChatStrategy) and not tab.strategy.history and not tab.strategy.deleted:
                self.storage.delete_chat(tab.strategy.chat_id)
                self._remove_history_item(tab.strategy.chat_id)
            view.close_page_finish(page, True)
        tab.close_session(remove, delete=getattr(tab.strategy, 'deleted', False))
        return True
