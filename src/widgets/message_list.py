from typing import List, Optional, Any, Dict
from gi.repository import Adw, Gtk, GObject, GLib
from ..bubbles import UserBubble, AiBubble

@Gtk.Template(resource_path='/io/github/jackrabbithanna/Gnollama/widgets/message_list.ui')
class MessageList(Gtk.Overlay):
    """Encapsulates the chat message list and scrolling behavior."""
    __gtype_name__ = 'MessageList'

    list_box: Gtk.ListBox = Gtk.Template.Child()

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._insert_index = None
        self._user_scrolling = False
        self._adjusting = False
        self._scroll_pending = False
        self.set_child(None)
        self.stack = Gtk.Stack()
        self.stack.add_named(self.list_box, 'messages')
        self.stack.add_named(Adw.StatusPage(title=_('Start a Conversation'),
            description=_('Choose a model and enter a message. Select knowledge collections to ask questions about your documents.'),
            icon_name='gnollama-chats-symbolic', css_classes=['compact']), 'empty')
        self.stack.set_visible_child_name('empty')
        self.scrolled = Gtk.ScrolledWindow(vexpand=True, hexpand=True, child=self.stack)
        self.set_child(self.scrolled)
        self.jump_button = Gtk.Button(label=_('Jump to latest'), halign=Gtk.Align.END,
                                      valign=Gtk.Align.END, margin_end=20, margin_bottom=12, visible=False)
        self.jump_button.add_css_class('osd')
        self.jump_button.connect('clicked', self.jump_to_latest)
        self.add_overlay(self.jump_button)
        
        # Connect auto-scroll
        vadjustment = self.get_vadjustment()
        if vadjustment:
             vadjustment.connect("value-changed", self.on_scroll)
             vadjustment.connect('changed', self._layout_changed)

    def get_vadjustment(self):
        return self.scrolled.get_vadjustment()

    def _layout_changed(self, adjustment):
        if not self._user_scrolling and not self._scroll_pending:
            self._scroll_pending = True
            GLib.idle_add(self._follow_layout)

    def _follow_layout(self):
        self._scroll_pending = False
        self.auto_scroll()
        return False

    def jump_to_latest(self, *args):
        self._user_scrolling = False
        self.auto_scroll()
             
    def on_scroll(self, adjustment: Gtk.Adjustment) -> None:
        """Detects if the user has scrolled up to disable auto-scrolling."""
        if self._adjusting or self._scroll_pending:
            return
        if adjustment.get_value() < adjustment.get_upper() - adjustment.get_page_size() - 20:
            self._user_scrolling = True
        else:
            self._user_scrolling = False
        self.jump_button.set_visible(self._user_scrolling)

    def auto_scroll(self) -> None:
        """Scrolls to the bottom of the chat view if user isn't scrolling."""
        if not self._user_scrolling:
            adj = self.get_vadjustment()
            if adj:
                self._adjusting = True
                adj.set_value(adj.get_upper() - adj.get_page_size())
                self._adjusting = False
                self.jump_button.set_visible(False)

    def add_user_message(self, text: str, images: Optional[List[str]] = None) -> None:
        """Adds a user message bubble."""
        bubble = UserBubble(text, images=images)
        self.stack.set_visible_child_name('messages')
        self._add_row(bubble)
        GLib.idle_add(self.auto_scroll)

    def add_system_message(self, text: str) -> None:
        """Adds a system message bubble."""
        row = Gtk.ListBoxRow()
        row.set_selectable(False)
        row.set_activatable(False)
        label = Gtk.Label(label=text)
        label.set_wrap(True)
        label.set_xalign(0)
        label.add_css_class("system-message")
        row.set_child(label)
        self.stack.set_visible_child_name('messages')
        self._add_row(row)
        GLib.idle_add(self.auto_scroll)

    def add_ai_bubble(self, bubble: AiBubble) -> None:
        """Adds an AI bubble."""
        self.stack.set_visible_child_name('messages')
        self._add_row(bubble)
        GLib.idle_add(self.auto_scroll)
        
    def _add_row(self, row):
        if self._insert_index is None:
            self.list_box.append(row)
        else:
            self.list_box.insert(row, self._insert_index)
            self._insert_index += 1
        self._last_row = row if isinstance(row, Gtk.ListBoxRow) else row.get_parent()

    def cancel_deliveries(self):
        child = self.list_box.get_first_child()
        while child:
            bubble = child if isinstance(child, AiBubble) else child.get_child()
            if isinstance(bubble, AiBubble):
                bubble.cancel_delivery()
            child = child.get_next_sibling()

    def clear(self) -> None:
        """Clears all messages."""
        child = self.list_box.get_first_child()
        while child:
            next_child = child.get_next_sibling()
            self.list_box.remove(child)
            child = next_child
        self.stack.set_visible_child_name('empty')
