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

from gettext import gettext as _
from gi.repository import Adw, Gtk, Gio, GLib, Gdk, GObject
from .tab import GenerationTab
from collections import ChainMap
from .session import ChatStrategy, display_chat_title
from .host_manager import HostManagerDialog
from .model_manager import ModelManagerDialog


@Gtk.Template(resource_path='/io/github/jackrabbithanna/Gnollama/window.ui')
class GnollamaWindow(Adw.ApplicationWindow):
    __gtype_name__ = 'GnollamaWindow'

    section_stack = Gtk.Template.Child()
    narrow = Gtk.Template.Child()

    def __init__(self, context, **kwargs):
        super().__init__(**kwargs)
        content = self.get_content()
        self.set_content(None)
        self.toast_overlay = Adw.ToastOverlay(child=content)
        self.set_content(self.toast_overlay)
        icon_theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
        icon_theme.add_resource_path('/io/github/jackrabbithanna/Gnollama/icons')
        self.context = context
        self.storage = context.storage
        self.storage.on_error = self._on_save_error
        # Chats, decisions, and images each have a section: its own sidebar and tabs.
        from .widgets.workspace_section import KINDS, WorkspaceSection
        self.sections = {}
        for kind in KINDS:
            section = WorkspaceSection(kind)
            self.sections[kind.name] = section
            self.section_stack.add_titled_with_icon(section.split_view, kind.name, section.title, kind.icon)
            self.narrow.add_setter(section.split_view, 'collapsed', True)
        chats = self.sections['chats']
        # The chats section, by the names the window had before there were several.
        self.tab_view, self.history_sidebar, self.split_view = chats.tab_view, chats.sidebar, chats.split_view
        from .widgets.knowledge_view import KnowledgeView
        self.knowledge_view = KnowledgeView(self.context)
        self.section_stack.add_titled_with_icon(self.knowledge_view, 'knowledge', _('Knowledge'), 'folder-documents-symbolic')
        self.context.knowledge.listeners.append(self._knowledge_changed)
        self._shutting_down = False
        self._allow_close = False
        self._cleanup_future = None
        self._save_error_dialog = None
        self.model_managers = []
        from .widgets.history_sidebar import HistorySidebar
        self._sidebar_menu = Gio.Menu()
        for section in self.sections.values():
            section.history = HistorySidebar(self, section)
            section.sidebar.set_menu_model(self._sidebar_menu)
            section.sidebar.connect('setup-menu', self._setup_sidebar_menu)
            section.sidebar.connect('activated', self.on_history_activated)
            section.tab_view.connect('notify::selected-page', lambda *args, s=section: self.on_tab_switched(s))
            section.tab_view.connect('close-page', self._on_close_page)
        # Every section's rows, for the actions on a session wherever it is listed.
        self.chat_rows = ChainMap(*(section.history.chat_rows for section in self.sections.values()))
        self.draft_rows = ChainMap(*(section.history.draft_rows for section in self.sections.values()))
        self.history = chats.history
        self.history_search = self.history.search
        self._setup_actions()
        self.connect('close-request', self.on_close_request)
        self.load_css()
        self.load_history_sidebar()
        self.new_chat_tab()

    def tabs(self):
        """The tabs of every section."""
        return [tab for section in self.sections.values() for tab in section.tabs()]

    def section_of(self, tab):
        return next((section for section in self.sections.values() if section.holds(tab)), None)

    def visible_section(self):
        """The section shown, or None while Knowledge is."""
        return self.sections.get(self.section_stack.get_visible_child_name())

    def select_tab(self, tab):
        section = self.section_of(tab)
        self.section_stack.set_visible_child_name(section.name)
        section.tab_view.set_selected_page(section.tab_view.get_page(tab))

    def on_close_request(self, *args):
        if self._allow_close:
            return False
        self.request_shutdown()
        return True

    def request_shutdown(self):
        if self._shutting_down:
            return
        self._shutting_down = True
        for section in self.sections.values():
            section.history.invalidate()
            section.history.popover.popdown()
            section.set_sensitive(False)
        self.knowledge_view.set_sensitive(False)
        self.knowledge_view.close_dialogs()
        self.context.cancel_requests()
        for tab in self.tabs():
            tab.prepare_shutdown()
        for window in list(Gtk.Window.list_toplevels()):
            parent = window.get_transient_for()
            while parent is not None and parent is not self:
                parent = parent.get_transient_for()
            if parent is self:
                window.close()
        # Manager dialogs cancel their own requests when closed.
        dialogs = self.get_dialogs()
        for dialog in [dialogs.get_item(i) for i in range(dialogs.get_n_items())]:
            if hasattr(dialog, 'requests'):
                dialog.force_close()
        GLib.timeout_add(50, self._poll_shutdown)

    def _poll_shutdown(self):
        if not self._shutting_down:
            return False
        if (not self.context.idle or any(tab.request or tab.chat_input.pending_imports for tab in self.tabs())):
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
        self.context.shutdown()
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
                self._shutting_down = False
                self._cleanup_future = None
                for section in self.sections.values():
                    section.set_sensitive(True)
                self.knowledge_view.set_sensitive(True)
                self.context.resume_requests()
                for tab in self.tabs():
                    if not tab.closing:
                        tab.on_host_changed()
        dialog.connect('response', response)
        dialog.present(self)

    def _setup_actions(self):
        actions = [('new_tab', lambda *args: self.new_tab()),
                   ('new_comparison', lambda *args: self.new_comparison_tab()),
                   ('new_model_conversation', lambda *args: self.new_model_conversation_tab()),
                   ('new_decision', lambda *args: self.new_decision_tab()),
                   ('new_image', lambda *args: self.new_image_tab()),
                   ('new_chat_tab', lambda *args: self.new_chat_tab()),
                   ('clear_history', self.on_clear_history),
                   ('manage_hosts', self.on_manage_hosts),
                   ('manage_models', self.on_manage_models),
                   ('close_tab', self.close_selected_tab),
                   ('next_tab', lambda *args: self._in_section(lambda s: s.tab_view.select_next_page())),
                   ('previous_tab', lambda *args: self._in_section(lambda s: s.tab_view.select_previous_page())),
                   ('toggle_sidebar', lambda *args: self._in_section(
                       lambda s: s.split_view.set_show_sidebar(not s.split_view.get_show_sidebar())))]
        for name, callback in actions:
            action = Gio.SimpleAction.new(name, None)
            action.connect('activate', lambda action, param, cb=callback: cb(action, param) if not self._shutting_down else None)
            self.add_action(action)
        for name, callback in [('history_pin', self.pin_chat), ('history_rename', self.rename_chat), ('history_delete', self.delete_chat), ('history_export_json', lambda id: self.export_chat(id, 'json')), ('history_export_md', lambda id: self.export_chat(id, 'markdown')),
                               ('draft_open', self.open_draft), ('draft_discard', self.discard_draft)]:
            action = Gio.SimpleAction.new(name, GLib.VariantType.new('s'))
            action.connect('activate', lambda a, p, cb=callback: cb(p.get_string()) if not self._shutting_down else None)
            self.add_action(action)

    def _in_section(self, act):
        section = self.visible_section()
        if section is not None:
            act(section)

    def load_css(self):
        # Adw.Application loads style.css from its resource base path at startup.
        # Windows created without it (tests, layout tools) load the stylesheet here.
        if isinstance(self.get_application(), Adw.Application):
            return
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
                self.clear_history()
        dialog.connect('response', response)
        dialog.present(self)

    def clear_history(self):
        """Delete every saved conversation and close the tabs that show one."""
        for tab in self.tabs():
            # Every chat tab, and any other kind once it has a session: comparisons, model
            # conversations, decisions, and images keep theirs in the chats that are deleted.
            if isinstance(tab.strategy, ChatStrategy) or tab.strategy.chat_id is not None:
                self.close_tab(tab, delete=True)
        self.storage.clear_all_chats()
        shown = self.section_stack.get_visible_child_name()
        self.new_chat_tab()
        # The new chat waits in Chats; the section the user was in stays shown.
        self.section_stack.set_visible_child_name(shown)
        self.storage.after_pending_writes(self.load_history_sidebar)

    def on_manage_hosts(self, *args):
        dialog = HostManagerDialog(self.context, on_hosts_changed_cb=self.on_hosts_changed)
        dialog.present(self)

    def on_manage_models(self, *args):
        dialog = ModelManagerDialog(self.context, is_model_busy=self.is_model_busy)
        self.model_managers.append(dialog)
        dialog.connect('closed', lambda *args: self._forget_manager(dialog))
        dialog.present(self)
        return dialog

    def _forget_manager(self, dialog):
        if dialog in self.model_managers:
            self.model_managers.remove(dialog)
        return False

    def is_model_busy(self, host, model):
        from .services import model_key
        key = model_key(host, model)
        if self.context.services.models.busy(host, model) or self.context.knowledge.busy(host, model):
            return True
        for tab in self.tabs():
            if not tab.request:
                continue
            states = tab.request.states.values() if hasattr(tab.request, 'states') else [tab.request]
            if any(model_key(state.connection, state.settings.target.model) == key for state in states):
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
        from .widgets.workspace_section import section_name
        section = self.sections[section_name(getattr(tab, 'mode', 'chat'))]
        self.section_stack.set_visible_child_name(section.name)
        page = section.tab_view.append(tab)
        tab.bind_property('title', page, 'title', GObject.BindingFlags.SYNC_CREATE)
        icons = dict(chat='network-server-symbolic', decision='gnollama-decision-symbolic',
                     image='gnollama-image-symbolic')
        page.set_icon(Gio.ThemedIcon.new(icons.get(tab.mode, 'edit-find-symbolic')))
        def request_changed(*args):
            page.set_loading(tab.request is not None)
            for manager in self.model_managers:
                manager.update_unload_buttons()
        tab.connect('request-changed', request_changed)
        tab.connect('chat-updated', self.on_chat_updated)
        section.tab_view.set_selected_page(page)
        return tab

    def new_comparison_tab(self):
        from .widgets.comparison_view import ComparisonTab
        return self._add_tab(ComparisonTab(self.context))

    def new_model_conversation_tab(self):
        from .widgets.model_conversation_view import ModelConversationTab
        return self._add_tab(ModelConversationTab(self.context))

    def new_decision_tab(self):
        from .widgets.decision_view import DecisionTab
        return self._add_tab(DecisionTab(self.context))

    def new_image_tab(self):
        from .widgets.image_view import ImageTab
        return self._add_tab(ImageTab(self.context))

    def new_tab(self):
        return self._add_tab(GenerationTab(mode='generate', context=self.context))

    def new_chat_tab(self):
        chat = self.storage.create_chat()
        self.add_history_row(chat, prepend=True)
        return self._add_tab(GenerationTab(mode='chat', chat_id=chat['id'], context=self.context))

    def open_chat_tab(self, chat_data):
        for tab in self.tabs():
            if getattr(tab.strategy, 'chat_id', None) == chat_data['id']:
                self.select_tab(tab)
                return tab
        if chat_data.get('kind') == 'comparison':
            from .widgets.comparison_view import ComparisonTab
            return self._add_tab(ComparisonTab(self.context, saved=chat_data))
        if chat_data.get('kind') == 'model_conversation':
            from .widgets.model_conversation_view import ModelConversationTab
            return self._add_tab(ModelConversationTab(self.context, saved=chat_data))
        if chat_data.get('kind') == 'decision':
            from .widgets.decision_view import DecisionTab
            return self._add_tab(DecisionTab(self.context, saved=chat_data))
        if chat_data.get('kind') == 'image':
            from .widgets.image_view import ImageTab
            return self._add_tab(ImageTab(self.context, saved=chat_data))
        return self._add_tab(GenerationTab(mode='chat', chat_id=chat_data['id'],
                                           initial_history=chat_data.get('messages', []), context=self.context, chat_data=chat_data))

    def load_history_sidebar(self):
        """Reload every section's sidebar."""
        for section in self.sections.values():
            section.history.reload()
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
        if item is None or hasattr(item, 'category_toggle'):
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
        return self.history.section(chat)

    def add_history_row(self, chat, prepend=False):
        self.history.add_chat(chat)

    def _remove_history_item(self, chat_id):
        for section in self.sections.values():
            section.history.remove_chat(chat_id)

    def _listing(self, item):
        """The section whose sidebar lists this row."""
        return next((section for section in self.sections.values()
                     if item in section.history.chat_rows.values() or item in section.history.draft_rows.values()
                     or item in section.history.footers.values()), self.sections['chats'])

    def on_history_activated(self, sidebar, index):
        self._open_history_item(sidebar.get_item(index))

    def _open_history_item(self, item):
        if item is None:
            return
        section = self._listing(item)
        if hasattr(item, 'category_toggle'):
            section.history.toggle(item.category_toggle)
            return
        section.history.popover.popdown()
        if section.split_view.get_collapsed():
            section.split_view.set_show_sidebar(False)
        if hasattr(item, 'draft_id'):
            for tab in self.tabs():
                if tab.draft.id == item.draft_id and not tab.closing:
                    self.select_tab(tab)
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
                        self.select_tab(tab)
                        return False
                if draft['mode'] == 'comparison':
                    from .widgets.comparison_view import ComparisonTab
                    self._add_tab(ComparisonTab(self.context, draft=draft))
                elif draft['mode'] == 'model_conversation':
                    from .widgets.model_conversation_view import ModelConversationTab
                    self._add_tab(ModelConversationTab(self.context, draft=draft))
                elif draft['mode'] == 'decision':
                    from .widgets.decision_view import DecisionTab
                    self._add_tab(DecisionTab(self.context, draft=draft))
                elif draft['mode'] == 'image':
                    from .widgets.image_view import ImageTab
                    self._add_tab(ImageTab(self.context, draft=draft))
                else:
                    self._add_tab(GenerationTab(mode=draft['mode'], context=self.context, draft=draft))
            return False
        self.context.services.control.submit(read)

    def on_tab_switched(self, section=None):
        """Select, in a section's sidebar, the session its selected tab shows."""
        for section in [section] if section is not None else self.sections.values():
            page = section.tab_view.get_selected_page()
            chat_id = getattr(page.get_child().strategy, 'chat_id', None) if page else None
            item = section.history.chat_rows.get(chat_id)
            section.sidebar.set_selected(item.get_index() if item else Gtk.INVALID_LIST_POSITION)

    def on_chat_updated(self, tab, chat_id, title):
        self.update_tab_title(chat_id, title)
        section = self.section_of(tab)
        if section is None:
            self.load_history_sidebar()
        else:
            section.history.reload()

    def update_tab_title(self, chat_id, title):
        for tab in self.tabs():
            if getattr(tab.strategy, 'chat_id', None) == chat_id:
                tab.title = display_chat_title(title)

    def export_chat(self, chat_id, format):
        from .export import choose_export
        choose_export(self, self.context, chat_id, format)

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
        for section in self.sections.values():
            section.history.invalidate()
        for tab in self.tabs():
            if getattr(tab.strategy, 'chat_id', None) == chat_id:
                self.close_tab(tab, delete=True)
        self.storage.delete_chat(chat_id, on_done=self.load_history_sidebar)
        self._remove_history_item(chat_id)

    def close_selected_tab(self, *args):
        section = self.visible_section()
        page = section.tab_view.get_selected_page() if section else None
        if page:
            section.tab_view.close_page(page)

    def close_tab(self, tab, delete=False):
        if delete:
            tab.strategy.deleted = True
        section = self.section_of(tab)
        if not tab.closing and section is not None:
            section.tab_view.close_page(section.tab_view.get_page(tab))

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
