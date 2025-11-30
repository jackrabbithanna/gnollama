# main.py
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

import sys
#import os

# Fix for WebKit EGL errors
#os.environ["WEBKIT_DISABLE_DMABUF_RENDERER"] = "1"
#os.environ["WEBKIT_DISABLE_COMPOSITING_MODE"] = "1"
#os.environ["__NV_DISABLE_EXPLICIT_SYNC"] = "1"
#os.environ["G_MESSAGES_DEBUG"]="all"
import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')

from gi.repository import Gtk, Gio, Adw
from .window import GnollamaWindow


class GnollamaApplication(Adw.Application):
    """The main application singleton class."""

    def __init__(self):
        super().__init__(application_id='com.github.jackrabbithanna.Gnollama',
                         flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
                         resource_base_path='/com/github/jackrabbithanna/Gnollama')
        self.create_action('quit', lambda *_: self.quit(), ['<control>q'])
        self.create_action('about', self.on_about_action)
        self.create_action('preferences', self.on_preferences_action)

    def do_activate(self):
        """Called when the application is activated.

        We raise the application's main window, creating it if
        necessary.
        """
        win = self.props.active_window
        if not win:
            win = GnollamaWindow(application=self)
        win.present()

    def on_about_action(self, *args):
        """Callback for the app.about action."""
        about = Adw.AboutDialog(application_name='gnollama',
                                application_icon='com.github.jackrabbithanna.Gnollama',
                                developer_name='Jackrabbithanna',
                                version='0.1.0',
                                developers=['Jackrabbithanna'],
                                copyright='© 2025 Jackrabbithanna')
        # Translators: Replace "translator-credits" with your name/username, and optionally an email or URL.
        about.set_translator_credits(_('translator-credits'))
        about.present(self.props.active_window)

    def on_preferences_action(self, widget, _):
        """Callback for the app.preferences action."""
        settings = Gio.Settings.new('com.github.jackrabbithanna.Gnollama')
        
        pref_window = Adw.PreferencesWindow(transient_for=self.props.active_window)
        
        page = Adw.PreferencesPage()
        page.set_title('Server Settings')
        page.set_icon_name('network-server-symbolic')
        pref_window.add(page)
        
        group = Adw.PreferencesGroup()
        group.set_title('Ollama Configuration')
        page.add(group)
        
        # Add a label saying settings are now per-tab?
        label = Gtk.Label(label="Ollama Host configuration is now available in the Advanced Settings of each tab.")
        label.set_wrap(True)
        label.set_margin_top(12)
        label.set_margin_bottom(12)
        label.set_margin_start(12)
        label.set_margin_end(12)
        group.add(label)
        
        pref_window.present()

    def create_action(self, name, callback, shortcuts=None):
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


def main(version):
    """The application's entry point."""
    app = GnollamaApplication()
    return app.run(sys.argv)
