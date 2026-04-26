import sys
from typing import List, Optional, Any, Callable
import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')

from gi.repository import Gtk, Gio, Adw
from .window import GnollamaWindow

class GnollamaApplication(Adw.Application):
    """The main application singleton class."""

    def __init__(self) -> None:
        super().__init__(application_id='io.github.jackrabbithanna.Gnollama',
                         flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
                         resource_base_path='/io/github/jackrabbithanna/Gnollama')
        self.create_action('quit', lambda *_: self.quit(), ['<control>q'])
        self.create_action('about', self.on_about_action)
        self.set_accels_for_action('win.new_chat_tab', ['<control>n'])

    def do_activate(self) -> None:
        """Called when the application is activated.

        We raise the application's main window, creating it if
        necessary.
        """
        win = self.props.active_window
        if not win:
            win = GnollamaWindow(application=self)
        win.present()

    def on_about_action(self, *args: Any) -> None:
        """Callback for the app.about action."""
        about = Adw.AboutDialog(application_name='gnollama',
                                application_icon='io.github.jackrabbithanna.Gnollama',
                                developer_name='Jackrabbithanna',
                                version='0.7.0',
                                developers=['Jackrabbithanna'],
                                copyright='© 2026 Jackrabbithanna')
        # Translators: Replace "translator-credits" with your name/username, and optionally an email or URL.
        about.set_translator_credits(_('translator-credits'))
        about.present(self.props.active_window)

    def create_action(self, name: str, callback: Callable, shortcuts: Optional[List[str]] = None) -> None:
        """Add an application action.

        Args:
            name: the name of the action
            callback: the function to be called when the action is
              activated
            shortcuts: an optional list of accelerators
        """
        action = Gio.SimpleAction.new(name, None)
        action.connect("activate", callback)
        self.add_action(action)
        if shortcuts:
            self.set_accels_for_action(f"app.{name}", shortcuts)

def main(version: str) -> int:
    """The application's entry point."""
    app = GnollamaApplication()
    return app.run(sys.argv)
