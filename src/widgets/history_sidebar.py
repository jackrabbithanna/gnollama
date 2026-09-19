"""Grouped history, independent expansion, and keyboard title completion."""
from collections import deque
import time
from gi.repository import Gtk, Adw, Gio, GLib, Gdk, Pango
from ..history import CATEGORIES, category
from ..session import display_chat_title


ICONS = dict(drafts='gnollama-draft-symbolic', pinned='pin-active-symbolic',
             chat='gnollama-chats-symbolic', comparison='gnollama-comparison-symbolic',
             model_conversation='gnollama-model-conversation-symbolic')


class HistorySidebar:
    def __init__(self, window):
        self.window, self.storage, self.sidebar = window, window.storage, window.history_sidebar
        self.chat_rows = window.chat_rows
        self.draft_rows = {}
        self.sections, self.footers = {}, {}
        self.browse_expanded, self.search_expanded = set(), set()
        self.generation = 0
        self.search_source = self.render_source = None
        self.pending = False
        self._inflight = 0
        self._needs_reload = False
        self.closed = False
        self._suggestions = []
        self._suggestion_query = ''
        self._dismissed_query = None
        self._preedit = False
        self.placeholder = self.sidebar.get_placeholder()
        self.search = Gtk.SearchEntry(placeholder_text=_('Search conversations'), margin_start=6, margin_end=6)
        self.search.connect('changed', self._query_changed)
        self.search.connect('stop-search', lambda *a: self.search.set_text(''))
        self.search.connect('unrealize', self._unrealize)
        self.search.get_delegate().connect('preedit-changed', lambda widget, text: setattr(self, '_preedit', bool(text)))
        self.sidebar.get_parent().add_top_bar(self.search)
        self.focus = Gtk.EventControllerFocus()
        self.focus.connect('enter', lambda *a: self._show_suggestions())
        self.focus.connect('leave', lambda *a: self.popover.popdown())
        self.search.add_controller(self.focus)
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect('key-pressed', self._key_pressed)
        self.search.add_controller(keys)
        self.keys = keys
        self.suggestion_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE,
                                           activate_on_single_click=True, focusable=False)
        self.suggestion_list.update_property([Gtk.AccessibleProperty.LABEL], [_('Search suggestions')])
        self.suggestion_list.connect('row-activated', self._complete)
        self.popover = Gtk.Popover(autohide=False, has_arrow=False, position=Gtk.PositionType.BOTTOM,
            child=Gtk.ScrolledWindow(child=self.suggestion_list, hscrollbar_policy=Gtk.PolicyType.NEVER,
                max_content_height=280, propagate_natural_height=True))
        titles = (_('Drafts'), _('Pinned'), _('Recent chats'), _('Recent comparisons'), _('Recent model conversations'))
        attrs = ('drafts_section', 'pinned_section', 'recent_chats_section', 'recent_comparisons_section',
                 'recent_model_conversations_section')
        for key, title, attr in zip(CATEGORIES, titles, attrs):
            section = Adw.SidebarSection(title=title)
            self.sidebar.append(section)
            self.sections[key] = section
            setattr(window, attr, section)
            footer = Adw.SidebarItem(title=_('Show more'), icon_name='pan-down-symbolic')
            footer.category_toggle = key
            self.footers[key] = footer

    @property
    def expanded(self):
        return self.search_expanded if self.search.get_text().strip() else self.browse_expanded

    def invalidate(self):
        self.generation += 1
        for name in ('search_source', 'render_source'):
            source = getattr(self, name)
            if source is not None:
                GLib.source_remove(source)
                setattr(self, name, None)

    def _unrealize(self, *args):
        self.invalidate()
        self._needs_reload = False
        self.popover.popdown()
        if self.popover.get_parent():
            self.popover.unparent()

    def _query_changed(self, *args):
        self.invalidate()
        self.pending = True
        self.search_expanded.clear()
        self._dismissed_query = None
        self.popover.popdown()
        self._suggestions = []
        self.search_source = GLib.timeout_add(150, self.reload)

    def toggle(self, key):
        if key in self.expanded:
            self.expanded.remove(key)
        else:
            self.expanded.add(key)
        self._footer_label(key)
        self.reload()

    def _footer_label(self, key):
        footer = self.footers[key]
        expanded = key in self.expanded
        footer.set_title(_('Show less') if expanded else _('Show more'))
        footer.set_icon_name('pan-up-symbolic' if expanded else 'pan-down-symbolic')
        footer.set_tooltip(self.sections[key].get_title() + ' · ' + footer.get_title())

    def reload(self):
        self.invalidate()
        if self.closed or self.window._allow_close or self.window._shutting_down:
            return False
        generation = self.generation
        query, expanded = self.search.get_text(), set(self.expanded)
        self.pending = True
        if self._inflight >= 2:
            self._needs_reload = True
            return False
        self._needs_reload = False
        self._inflight += 1
        def read():
            try:
                result, error = self.storage.sidebar_snapshot(query, expanded), None
            except Exception as exc:
                result, error = None, str(exc)
            GLib.idle_add(deliver, result, error)
        def deliver(result, error):
            self._inflight -= 1
            if self._needs_reload:
                self.reload()
            if generation != self.generation or self.closed or self.window._allow_close or self.window._shutting_down:
                return False
            if error:
                self.pending = False
                from .feedback import toast
                toast(self.window, error)
                return False
            self._apply(result, query, expanded, generation)
            return False
        self.storage.services.control.submit(read)
        return False

    def _apply(self, result, query, expanded, generation):
        self.placeholder.set_title(_('No matching conversations') if query.strip() else _('No Saved Chats'))
        self._set_suggestions(result['suggestions'], query)
        groups = {key: rows if key in expanded else rows[:5] for key, rows in result['groups'].items()}
        wanted_chats = {row['id'] for key, rows in groups.items() if key != 'drafts' for row in rows}
        wanted_drafts = {row['id'] for row in groups['drafts']}
        jobs = deque()
        for id in list(self.chat_rows):
            if id not in wanted_chats:
                jobs.append(lambda id=id: self.remove_chat(id))
        for id in list(self.draft_rows):
            if id not in wanted_drafts:
                jobs.append(lambda id=id: self._remove(self.draft_rows, id))
        for key in CATEGORIES:
            for position, row in enumerate(groups[key]):
                jobs.append(lambda key=key, row=row, position=position: self._upsert(key, row, position, query))
            jobs.append(lambda key=key: self._place_footer(key, bool(groups[key]) and
                (key in expanded or len(result['groups'][key]) > 5)))
        # AdwSidebar virtualizes rows. Batch item/menu construction as well so
        # expanding a large archive does not block streaming or keyboard input.
        def drain():
            if generation != self.generation or self.closed:
                return False
            started = time.monotonic()
            while jobs and time.monotonic() - started < .006:
                jobs.popleft()()
            self.window.on_tab_switched()
            if jobs:
                return True
            self.render_source = None
            self.pending = False
            return False
        self.render_source = GLib.idle_add(drain)

    def _place_footer(self, key, visible):
        footer = self.footers[key]
        section = self.sections[key]
        self._footer_label(key)
        if footer.get_section():
            footer.get_section().remove(footer)
        if visible:
            section.append(footer)

    def _upsert(self, key, row, position, query):
        rows = self.draft_rows if key == 'drafts' else self.chat_rows
        item = rows.get(row['id'])
        if item is None:
            item = Adw.SidebarItem(icon_name=ICONS[key])
            rows[row['id']] = item
            if key == 'drafts':
                item.draft_id = row['id']
                item.chat_id = row['chat_id']
                item.is_pinned = False
            else:
                item.chat_id = row['id']
                item.actions_menu = Gio.Menu()
                item.actions_button = Gtk.MenuButton(icon_name='view-more-symbolic',
                    menu_model=item.actions_menu, tooltip_text=_('Chat actions'), css_classes=['flat'])
                item.set_suffix(item.actions_button)
        if key != 'drafts':
            pinned = bool(row.get('is_pinned'))
            if not hasattr(item, 'is_pinned') or pinned != item.is_pinned:
                item.is_pinned = pinned
                self.window._fill_history_menu(item.actions_menu, item)
        item.match_uid = row.get('match_uid')
        item.set_title((row['title'] or _('Draft')) if key == 'drafts' else display_chat_title(row['title']))
        item.set_subtitle(row.get('snippet') if query.strip() and item.match_uid else None)
        item.set_tooltip(row.get('snippet') or item.get_title())
        item.set_icon_name(ICONS[key])
        section = self.sections[key]
        if item.get_section() != section or item.get_section_index() != position:
            if item.get_section():
                item.get_section().remove(item)
            section.insert(item, position)

    def add_chat(self, chat):
        # New tabs enter the same limited, sorted query as all other records.
        self.invalidate()
        self.storage._submit(lambda: None, on_done=self.reload)

    def section(self, chat):
        return self.sections[category(chat)]

    @staticmethod
    def _remove(rows, id):
        item = rows.pop(id, None)
        if item and item.get_section():
            item.get_section().remove(item)

    def remove_chat(self, id):
        self._remove(self.chat_rows, id)

    def _set_suggestions(self, suggestions, query):
        self._suggestions, self._suggestion_query = suggestions, query
        self.suggestion_list.remove_all()
        for suggestion in suggestions:
            row = Gtk.ListBoxRow(focusable=False)
            row.title = suggestion['title']
            content = Gtk.Box(spacing=8, margin_start=8, margin_end=8, margin_top=8, margin_bottom=8)
            content.append(Gtk.Image(icon_name=ICONS[category(suggestion)]))
            content.append(Gtk.Label(label=row.title, xalign=0, ellipsize=Pango.EllipsizeMode.END, max_width_chars=24, hexpand=True))
            row.set_child(content)
            row.set_tooltip_text(row.title)
            self.suggestion_list.append(row)
        self.suggestion_list.unselect_all()
        self._show_suggestions()

    def _show_suggestions(self):
        query = self.search.get_text()
        if (self._suggestions and self._suggestion_query == query and self._dismissed_query != query
                and self.search.get_mapped() and self.focus.contains_focus() and not self.window._shutting_down):
            if not self.popover.get_parent():
                self.popover.set_parent(self.search)
            self.popover.set_size_request(max(180, self.search.get_width()), -1)
            self.popover.popup()
        else:
            self.popover.popdown()

    def _complete(self, listbox, row):
        if row is None:
            return
        self.search.set_text(row.title)
        self.search.set_position(-1)
        self._dismissed_query = row.title
        self.popover.popdown()
        self.search.grab_focus()
        self.reload()

    def _key_pressed(self, controller, keyval, keycode, state):
        if self._preedit:
            return False
        if state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK | Gdk.ModifierType.SUPER_MASK):
            return False
        if keyval == Gdk.KEY_Escape and self.popover.get_visible():
            self._dismissed_query = self.search.get_text()
            self.popover.popdown()
            return True
        if not self._suggestions or self._suggestion_query != self.search.get_text():
            return False
        row = self.suggestion_list.get_selected_row()
        if keyval in (Gdk.KEY_Down, Gdk.KEY_Up):
            self._dismissed_query = None
            self._show_suggestions()
            index = row.get_index() if row else (-1 if keyval == Gdk.KEY_Down else len(self._suggestions))
            index = max(0, min(len(self._suggestions) - 1, index + (1 if keyval == Gdk.KEY_Down else -1)))
            self.suggestion_list.select_row(self.suggestion_list.get_row_at_index(index))
            return True
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_Tab) and row and self.popover.get_visible():
            self._complete(self.suggestion_list, row)
            return True
        return False
