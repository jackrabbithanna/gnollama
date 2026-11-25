"""One section of the window: a sidebar of saved sessions beside the tabs that show them.

Chats, decisions, and images each have one. A section lists and opens only its own
kinds of session and draft.
"""
from dataclasses import dataclass
from gettext import gettext as _
from gi.repository import Adw, GObject, Gtk


@dataclass(frozen=True)
class Kind:
    """What one section holds, and the words it shows."""
    name: str
    kinds: tuple       # session kinds (chats.kind) listed in its sidebar
    modes: tuple       # draft modes listed in its sidebar
    tab_modes: tuple   # tab modes it opens
    icon: str
    action: str


KINDS = (
    Kind('chats', ('chat', 'comparison', 'model_conversation'), ('chat', 'generate', 'comparison', 'model_conversation'),
         ('chat', 'generate', 'comparison', 'model_conversation'), 'tureen-chats-symbolic', 'win.new_chat_tab'),
    Kind('decisions', ('decision',), ('decision',), ('decision',), 'tureen-decision-symbolic', 'win.new_decision'),
    Kind('images', ('image',), ('image',), ('image',), 'tureen-image-symbolic', 'win.new_image'),
)


def section_name(mode):
    """The section that opens a tab, session, or draft of this mode or kind."""
    return next((kind.name for kind in KINDS if mode in kind.tab_modes), 'chats')


def labels(name):
    """(title, sidebar title, new, empty sidebar, search placeholder) of a section."""
    return {
        'chats': (_('Chats'), _('Saved Chats'), _('New Chat'), _('No Saved Chats'), _('Search conversations')),
        'decisions': (_('Decisions'), _('Saved Decisions'), _('New Decision'), _('No Saved Decisions'),
                      _('Search decisions')),
        'images': (_('Images'), _('Saved Images'), _('New Image'), _('No Saved Images'), _('Search images')),
    }[name]


class WorkspaceSection:
    """The widgets of one section, built in code since the three differ only in words."""

    def __init__(self, kind):
        self.kind = kind
        self.name = kind.name
        title, sidebar_title, new, empty, self.search_placeholder = labels(kind.name)
        self.title, self.empty_title = title, empty

        self.sidebar = Adw.Sidebar(placeholder=Adw.StatusPage(title=empty, icon_name=kind.icon))
        header = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False,
                               title_widget=Adw.WindowTitle(title=sidebar_title))
        header.pack_start(Gtk.Button(icon_name='list-add-symbolic', tooltip_text=new, action_name=kind.action))
        sidebar_view = Adw.ToolbarView(content=self.sidebar)
        sidebar_view.add_top_bar(header)

        self.tab_view = Adw.TabView()
        self.toggle = Gtk.ToggleButton(icon_name='tureen-sidebar-symbolic', tooltip_text=_('Toggle Sidebar'))
        bar = Adw.TabBar(view=self.tab_view, autohide=False, start_action_widget=self.toggle,
                         end_action_widget=Gtk.Button(icon_name='list-add-symbolic', tooltip_text=new,
                                                      action_name=kind.action, css_classes=['flat']))
        # With no tab open, the content says so and offers a new one; the bar stays for its buttons.
        start = Gtk.Button(label=new, action_name=kind.action, halign=Gtk.Align.CENTER,
                           css_classes=['pill', 'suggested-action'])
        self.empty = Adw.StatusPage(title=_('Nothing Open'), icon_name=kind.icon, child=start)
        self.content = Gtk.Stack()
        self.content.add_named(self.tab_view, 'tabs')
        self.content.add_named(self.empty, 'empty')
        self.tab_view.connect('notify::n-pages', lambda *args: self._show_content())
        self._show_content()
        content_view = Adw.ToolbarView(content=self.content)
        content_view.add_top_bar(bar)

        self.split_view = Adw.OverlaySplitView(min_sidebar_width=220, max_sidebar_width=300,
                                               sidebar=sidebar_view, content=content_view)
        self.split_view.bind_property('show-sidebar', self.toggle, 'active',
                                      GObject.BindingFlags.BIDIRECTIONAL | GObject.BindingFlags.SYNC_CREATE)
        self.history = None  # the HistorySidebar, once the window has made it

    def _show_content(self):
        self.content.set_visible_child_name('tabs' if self.tab_view.get_n_pages() else 'empty')

    def tabs(self):
        return [self.tab_view.get_nth_page(i).get_child() for i in range(self.tab_view.get_n_pages())]

    def holds(self, tab):
        return any(page.get_child() is tab for page in
                   (self.tab_view.get_nth_page(i) for i in range(self.tab_view.get_n_pages())))

    def set_sensitive(self, sensitive):
        self.tab_view.set_sensitive(sensitive)
        self.sidebar.set_sensitive(sensitive)
