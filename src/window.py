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
from .session import ChatStrategy, display_chat_title
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
    section_stack = Gtk.Template.Child()

    def __init__(self, storage=None, **kwargs):
        super().__init__(**kwargs)
        content = self.get_content()
        self.set_content(None)
        self.toast_overlay = Adw.ToastOverlay(child=content)
        self.set_content(self.toast_overlay)
        icon_theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
        icon_theme.add_resource_path('/io/github/jackrabbithanna/Gnollama/icons')
        self.settings = Gio.Settings.new('io.github.jackrabbithanna.Gnollama')
        self.storage = storage if storage is not None else ChatStorage()
        self.storage.on_error = self._on_save_error
        from .widgets.knowledge_view import KnowledgeView
        self.knowledge_view = KnowledgeView(self.storage)
        self.section_stack.add_titled_with_icon(self.knowledge_view, 'knowledge', _('Knowledge'), 'folder-documents-symbolic')
        self.storage.knowledge.listeners.append(self._knowledge_changed)
        self._shutting_down = False
        self._allow_close = False
        self._cleanup_future = None
        self._save_error_dialog = None
        self.chat_rows = {}
        self.model_managers = []
        self._history_generation = 0
        self._history_limit = 100
        self._history_cache = []
        self._search_source = None
        sidebar_toolbar = self.history_sidebar.get_parent()
        sidebar_toolbar.set_content(None)
        self.history_stack = Gtk.Stack()
        self.history_stack.add_named(self.history_sidebar, 'history')
        self.search_results = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.history_stack.add_named(Gtk.ScrolledWindow(child=self.search_results, vexpand=True,
            hscrollbar_policy=Gtk.PolicyType.NEVER), 'search')
        sidebar_toolbar.set_content(self.history_stack)
        self.history_search = Gtk.SearchEntry(placeholder_text=_('Search conversations'), margin_start=6, margin_end=6)
        self.history_search.connect('search-changed', self._search_changed)
        sidebar_toolbar.add_top_bar(self.history_search)
        self.history_more = Gtk.Button(label=_('Load More'), visible=False)
        self.history_more.connect('clicked', self._more_history)
        sidebar_toolbar.add_bottom_bar(self.history_more)
        self.pinned_section = Adw.SidebarSection(title=_('Pinned'))
        self.recent_chats_section = Adw.SidebarSection(title=_('Recent chats'))
        self.recent_comparisons_section = Adw.SidebarSection(title=_('Recent comparisons'))
        self.drafts_section = Adw.SidebarSection(title=_('Drafts'))
        for section in (self.drafts_section, self.pinned_section,
                        self.recent_chats_section, self.recent_comparisons_section):
            self.history_sidebar.append(section)
        self.draft_rows = {}
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
        self.knowledge_view.set_sensitive(False)
        self.knowledge_view.close_dialogs()
        self.storage.knowledge.cancel_all()
        from . import ollama
        ollama.cancel_all()
        for tab in self.tabs():
            tab.draft.flush()
            tab.chat_input.cancel_fetches()
            for picker in getattr(tab, 'targets', []):
                picker.input.cancel_fetches()
            tab.knowledge_control.close_dialog()
            if tab._retrieval_dialog:
                tab._retrieval_dialog.close()
            if tab.options_panel._schema_dialog is not None:
                tab.options_panel._schema_dialog.close()
            if tab.options_panel._tools_dialog is not None:
                tab.options_panel._tools_dialog.close()
            if tab.options_panel._settings_dialog is not None:
                tab.options_panel._settings_dialog.close()
            for view in tab._tool_views:
                view.close_editor()
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
        if (not self.storage.services.idle or not self.storage.knowledge.idle
                or self.storage.pending_callbacks or any(tab.request or tab.chat_input.pending_imports for tab in self.tabs())):
            return True
        if self.storage.writer.error is not None:
            self._on_save_error(self.storage.writer.error)
            return True
        if self._cleanup_future is None:
            for tab in self.tabs():
                tab.draft.flush()
            self._cleanup_future = self.storage.cleanup_empty_chats()
        if not self.storage.writer.idle:
            return True
        self.storage.writer.shutdown()
        self.storage.knowledge.shutdown()
        self.storage.services.shutdown(wait=False)
        self._allow_close = True
        self.close()
        return False

    def _on_save_error(self, error):
        if self._save_error_dialog is not None or self._allow_close:
            return
        dialog = Adw.AlertDialog(heading=_('Changes could not be saved'),
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
                self.knowledge_view.set_sensitive(True)
                self.storage.knowledge.closed = False
                ollama.resume()
                for tab in self.tabs():
                    if not tab.closing:
                        tab.on_host_changed()
        dialog.connect('response', response)
        dialog.present(self)

    def _setup_actions(self):
        actions = [('new_tab', lambda *args: self.new_tab()),
                   ('new_comparison', lambda *args: self.new_comparison_tab()),
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
        for name, callback in [('history_pin', self.pin_chat), ('history_rename', self.rename_chat), ('history_delete', self.delete_chat), ('history_export_json', lambda id: self.export_chat(id, 'json')), ('history_export_md', lambda id: self.export_chat(id, 'markdown')),
                               ('draft_open', self.open_draft), ('draft_discard', self.discard_draft)]:
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
        return dialog

    def _forget_manager(self, dialog):
        if dialog in self.model_managers:
            self.model_managers.remove(dialog)
        return False

    def is_model_busy(self, host, model):
        from .services import model_key
        key = model_key(host, model)
        if self.storage.services.models.busy(host, model) or self.storage.knowledge.busy(host, model):
            return True
        for tab in self.tabs():
            if not tab.request:
                continue
            states = tab.request.states.values() if hasattr(tab.request, 'states') else [tab.request]
            if any(model_key(state.settings['host'], state.settings['model']) == key for state in states):
                return True
        return False

    def _knowledge_changed(self):
        for manager in self.model_managers:
            manager.update_unload_buttons()

    def on_hosts_changed(self):
        for tab in self.tabs():
            tab.update_hosts()
        for manager in self.model_managers:
            manager.update_hosts()
        self.knowledge_view.refresh()

    def _add_tab(self, tab):
        self.section_stack.set_visible_child_name('chats')
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

    def new_comparison_tab(self):
        from .widgets.comparison_view import ComparisonTab
        return self._add_tab(ComparisonTab(self.storage))

    def new_tab(self):
        return self._add_tab(GenerationTab(mode='generate', storage=self.storage))

    def new_chat_tab(self):
        chat = self.storage.create_chat()
        self.add_history_row(chat, prepend=True)
        return self._add_tab(GenerationTab(mode='chat', chat_id=chat['id'], storage=self.storage))

    def open_chat_tab(self, chat_data):
        self.section_stack.set_visible_child_name('chats')
        for tab in self.tabs():
            if getattr(tab.strategy, 'chat_id', None) == chat_data['id']:
                self.tab_view.set_selected_page(self.tab_view.get_page(tab))
                return tab
        if chat_data.get('kind') == 'comparison':
            from .widgets.comparison_view import ComparisonTab
            return self._add_tab(ComparisonTab(self.storage, saved=chat_data))
        return self._add_tab(GenerationTab(mode='chat', chat_id=chat_data['id'],
                                           initial_history=chat_data.get('messages', []), storage=self.storage, chat_data=chat_data))

    def _search_changed(self, *args):
        self._history_limit = 100
        if self._search_source:
            GLib.source_remove(self._search_source)
        self._search_source = GLib.timeout_add(250, self.load_history_sidebar)

    def _more_history(self, *args):
        self.load_history_sidebar(append=True)

    def load_history_sidebar(self, append=False):
        self._search_source = None
        if self._allow_close:
            return False
        self._history_generation += 1
        generation = self._history_generation
        query = self.history_search.get_text()
        previous = list(self._history_cache) if append else []
        offset = len(previous)
        def read():
            try:
                chats = self.storage.list_history(query, 101, offset)
                drafts = self.storage.list_drafts()
                GLib.idle_add(deliver, chats, drafts)
            except Exception as exc:
                from .widgets.feedback import toast
                GLib.idle_add(toast, self, str(exc))
        def deliver(chats, drafts):
            if generation != self._history_generation or self._allow_close:
                return False
            self.history_stack.set_visible_child_name('search' if query else 'history')
            more = len(chats) > 100
            self._history_cache = previous + chats[:100]
            chats = self._history_cache
            wanted = {c['id'] for c in chats}
            if not query:
                wanted.update(getattr(t.strategy, 'chat_id', None) for t in self.tabs() if not t.closing)
            for id in list(self.chat_rows):
                if id not in wanted:
                    self._remove_history_item(id)
            if query:
                self.search_results.remove_all()
                from types import SimpleNamespace
                for chat in chats:
                    row = Adw.ActionRow(title=chat['title'], subtitle=chat.get('snippet') or '',
                        activatable=True, use_markup=False, subtitle_lines=2, title_lines=1)
                    item = SimpleNamespace(chat_id=chat['id'], match_uid=chat.get('match_uid'))
                    row.connect('activated', lambda row, item=item: self._open_history_item(item))
                    self.search_results.append(row)
            for chat in chats:
                item = self.chat_rows.get(chat['id'])
                if item and item.get_section() != self._history_section(chat):
                    self._remove_history_item(chat['id'])
                    item = None
                if item:
                    item.set_title(display_chat_title(chat['title']))
                    item.set_tooltip(chat.get('snippet') or item.get_title())
                    item.match_uid = chat.get('match_uid')
                else:
                    self.add_history_row(chat)
            positions = {self.pinned_section: 0, self.recent_chats_section: 0,
                         self.recent_comparisons_section: 0}
            for chat in chats:
                item = self.chat_rows[chat['id']]
                section = item.get_section()
                position = positions[section]
                if item.get_section_index() != position:
                    section.remove(item)
                    section.insert(item, position)
                positions[section] += 1
            for id in list(self.draft_rows):
                if id not in {d['id'] for d in drafts}:
                    self.drafts_section.remove(self.draft_rows.pop(id))
            for draft in drafts:
                item = self.draft_rows.get(draft['id'])
                if item is None:
                    item = Adw.SidebarItem(icon_name='gnollama-draft-symbolic')
                    item.draft_id, item.chat_id = draft['id'], draft['chat_id']
                    item.is_pinned = False
                    self.drafts_section.append(item)
                    self.draft_rows[draft['id']] = item
                item.set_title(draft['title'] or _('Draft'))
            self.history_more.set_visible(more)
            self.on_tab_switched()
            return False
        self.storage.services.control.submit(read)
        return False

    def _fill_history_menu(self, menu, item):
        menu.remove_all()
        for label, action in [(_('Unpin Chat') if item.is_pinned else _('Pin Chat'), 'history_pin'),
                              (_('Rename Chat'), 'history_rename'), (_('Export Markdown'), 'history_export_md'),
                              (_('Export JSON'), 'history_export_json'), (_('Delete Chat'), 'history_delete')]:
            entry = Gio.MenuItem.new(label, None)
            entry.set_action_and_target_value('win.' + action, GLib.Variant('s', item.chat_id))
            menu.append_item(entry)

    def _setup_sidebar_menu(self, sidebar, item):
        self._sidebar_menu.remove_all()
        if item is None:
            return
        if hasattr(item, 'draft_id'):
            for label, action in [(_('Continue'), 'draft_open'), (_('Discard Draft'), 'draft_discard')]:
                entry = Gio.MenuItem.new(label, None)
                entry.set_action_and_target_value('win.' + action, GLib.Variant('s', item.draft_id))
                self._sidebar_menu.append_item(entry)
        else:
            self._fill_history_menu(self._sidebar_menu, item)

    def open_draft(self, draft_id):
        self._open_history_item(self.draft_rows.get(draft_id))

    def discard_draft(self, draft_id):
        for tab in self.tabs():
            if tab.draft.id == draft_id:
                tab.discard_draft()
        self.storage.delete_draft(draft_id, on_done=self.load_history_sidebar)

    def _history_section(self, chat):
        if chat.get('is_pinned'):
            return self.pinned_section
        return self.recent_comparisons_section if chat.get('kind') == 'comparison' else self.recent_chats_section

    def add_history_row(self, chat, prepend=False):
        item = Adw.SidebarItem(title=display_chat_title(chat.get('title', 'New Chat')), icon_name='gnollama-chats-symbolic')
        item.chat_id = chat['id']
        item.is_pinned = chat.get('is_pinned', False)
        item.match_uid = chat.get('match_uid')
        item.set_tooltip(item.get_title())
        menu = Gio.Menu()
        self._fill_history_menu(menu, item)
        button = Gtk.MenuButton(icon_name='view-more-symbolic', menu_model=menu, tooltip_text=_('Chat actions'))
        button.add_css_class('flat')
        item.set_suffix(button)
        section = self._history_section(chat)
        (section.prepend if prepend else section.append)(item)
        self.chat_rows[item.chat_id] = item

    def _remove_history_item(self, chat_id):
        item = self.chat_rows.pop(chat_id, None)
        if item and item.get_section():
            item.get_section().remove(item)

    def on_history_activated(self, sidebar, index):
        self._open_history_item(sidebar.get_item(index))

    def _open_history_item(self, item):
        if item is None:
            return
        if self.split_view.get_collapsed():
            self.split_view.set_show_sidebar(False)
        if hasattr(item, 'draft_id'):
            for tab in self.tabs():
                if tab.draft.id == item.draft_id and not tab.closing:
                    self.tab_view.set_selected_page(self.tab_view.get_page(tab))
                    return
        def read():
            draft = self.storage.get_draft(item.draft_id) if hasattr(item, 'draft_id') else None
            chat = self.storage.conversation_page(item.chat_id, getattr(item, 'match_uid', None)) if item.chat_id else None
            if chat and chat.get('kind') == 'comparison':
                chat = self.storage.export_snapshot(chat['id'])
            GLib.idle_add(deliver, chat, draft)
        def deliver(chat, draft):
            if self._shutting_down:
                return False
            if chat:
                tab = self.open_chat_tab(chat)
                if draft:
                    tab.restore_draft(draft)
                if getattr(item, 'match_uid', None):
                    tab.show_message(item.match_uid)
            elif draft:
                for tab in self.tabs():
                    if tab.draft.id == draft['id']:
                        self.tab_view.set_selected_page(self.tab_view.get_page(tab))
                        return False
                if draft['mode'] == 'comparison':
                    from .widgets.comparison_view import ComparisonTab
                    self._add_tab(ComparisonTab(self.storage, draft=draft))
                else:
                    self._add_tab(GenerationTab(mode=draft['mode'], storage=self.storage, draft=draft))
            return False
        self.storage.services.control.submit(read)

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
                tab.title = display_chat_title(title)

    def export_chat(self, chat_id, format):
        from .export import choose_export
        choose_export(self, self.storage, chat_id, format)

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
            if choice == 'save' and title and title != item.get_title() and not self._shutting_down:
                def saved():
                    self.update_tab_title(chat_id, title)
                    self.load_history_sidebar()
                self.storage.update_title(chat_id, title, on_done=saved)
        dialog.connect('response', response)
        dialog.present(self)

    def delete_chat(self, chat_id):
        item = self.chat_rows.get(chat_id)
        dialog = Adw.AlertDialog(heading=_('Delete Conversation?'),
            body=_('Permanently delete “{0}” and its messages?').format(item.get_title() if item else _('Conversation')))
        dialog.add_response('cancel', _('Cancel'))
        dialog.add_response('delete', _('Delete'))
        dialog.set_response_appearance('delete', Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response('cancel')
        dialog.set_close_response('cancel')
        dialog.connect('response', lambda d, choice: self._delete_chat_confirmed(chat_id)
                       if choice == 'delete' and not self._shutting_down else None)
        dialog.present(self)
        return dialog

    def _delete_chat_confirmed(self, chat_id):
        self._history_generation += 1
        for tab in self.tabs():
            if getattr(tab.strategy, 'chat_id', None) == chat_id:
                self.close_tab(tab, delete=True)
        self.storage.delete_chat(chat_id, on_done=self.load_history_sidebar)
        self._remove_history_item(chat_id)

    def close_selected_tab(self, *args):
        if self.section_stack.get_visible_child_name() != 'chats':
            return
        page = self.tab_view.get_selected_page()
        if page:
            self.tab_view.close_page(page)

    def close_tab(self, tab, delete=False):
        if delete:
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
            def finish():
                view.close_page_finish(page, True)
                self.load_history_sidebar()
            if isinstance(tab.strategy, ChatStrategy) and not tab.strategy.deleted:
                self.storage.cleanup_empty_chat(tab.strategy.chat_id, on_done=finish)
            else:
                finish()
        tab.close_session(remove, delete=getattr(tab.strategy, 'deleted', False))
        return True
