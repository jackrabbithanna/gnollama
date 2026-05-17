from typing import List, Optional, Any, Dict
from gi.repository import Gtk, GObject, GLib
from ..bubbles import UserBubble, AiBubble

@Gtk.Template(resource_path='/io/github/jackrabbithanna/Gnollama/widgets/message_list.ui')
class MessageList(Gtk.ScrolledWindow):
    """Encapsulates the chat message list and scrolling behavior."""
    __gtype_name__ = 'MessageList'

    list_box: Gtk.ListBox = Gtk.Template.Child()

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._user_scrolling = False
        
        # Connect auto-scroll
        vadjustment = self.get_vadjustment()
        if vadjustment:
             vadjustment.connect("value-changed", self.on_scroll)
             
    def on_scroll(self, adjustment: Gtk.Adjustment) -> None:
        """Detects if the user has scrolled up to disable auto-scrolling."""
        if adjustment.get_value() < adjustment.get_upper() - adjustment.get_page_size() - 20:
            self._user_scrolling = True
        else:
            self._user_scrolling = False

    def auto_scroll(self) -> None:
        """Scrolls to the bottom of the chat view if user isn't scrolling."""
        if not self._user_scrolling:
            adj = self.get_vadjustment()
            if adj:
                adj.set_value(adj.get_upper() - adj.get_page_size())

    def add_user_message(self, text: str, images: Optional[List[str]] = None) -> None:
        """Adds a user message bubble."""
        bubble = UserBubble(text, images=images)
        self.list_box.append(bubble)
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
        self.list_box.append(row)
        GLib.idle_add(self.auto_scroll)

    def add_ai_bubble(self, bubble: AiBubble) -> None:
        """Adds an AI bubble."""
        self.list_box.append(bubble)
        GLib.idle_add(self.auto_scroll)
        
    def clear(self) -> None:
        """Clears all messages."""
        child = self.list_box.get_first_child()
        while child:
            next_child = child.get_next_sibling()
            self.list_box.remove(child)
            child = next_child
