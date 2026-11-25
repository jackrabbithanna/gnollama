"""Keep the application responsive while opening and upgrading its database."""
import threading
from gettext import gettext as _
from gi.repository import Adw, Gtk, GLib
from .app_context import AppContext


class StartupWindow(Adw.ApplicationWindow):
    def __init__(self, application, on_ready):
        super().__init__(application=application, title=_('Tureen'), default_width=480, default_height=360)
        self.on_ready = on_ready
        self.busy = False
        self.closing = False
        self.toolbar = Adw.ToolbarView()
        self.toolbar.add_top_bar(Adw.HeaderBar())
        self.page = Adw.StatusPage(title=_('Opening Tureen'))
        self.controls = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                                halign=Gtk.Align.CENTER)
        self.spinner = Adw.Spinner(width_request=32, height_request=32)
        self.retry = Gtk.Button(label=_('Retry'))
        self.retry.add_css_class('suggested-action')
        self.retry.connect('clicked', lambda *args: self.start())
        self.close_button = Gtk.Button(label=_('Close'))
        self.close_button.connect('clicked', lambda *args: self.close())
        for widget in (self.spinner, self.retry, self.close_button):
            self.controls.append(widget)
        self.page.set_child(self.controls)
        self.toolbar.set_content(self.page)
        self.set_content(self.toolbar)
        self.connect('close-request', self._close_requested)
        self.start()

    def _close_requested(self, *args):
        if self.busy:
            self.closing = True
            self.page.set_description(_('Finishing the database operation before closing…'))
            self.close_button.set_sensitive(False)
            return True
        return False

    def start(self):
        if self.busy or self.closing:
            return
        self.busy = True
        self.retry.set_visible(False)
        self.spinner.set_visible(True)
        self.page.set_title(_('Opening Tureen'))
        self.page.set_description(_('Checking the database…'))

        def progress(message):
            def deliver():
                if self.busy and not self.closing:
                    self.page.set_description(message)
                return False
            GLib.idle_add(deliver)

        def open_context():
            context, error = None, None
            try:
                context = AppContext(progress=progress)
            except Exception as exc:
                error = exc
            GLib.idle_add(self._completed, context, error)

        threading.Thread(target=open_context, name='TureenStartup', daemon=False).start()

    def _completed(self, context, error):
        self.busy = False
        if self.closing:
            if context is not None:
                context.shutdown()
            self.destroy()
            return False
        if error is None:
            try:
                self.on_ready(context)
                self.destroy()
                return False
            except Exception as exc:
                context.shutdown()
                error = exc
        self.spinner.set_visible(False)
        self.retry.set_visible(True)
        self.page.set_title(_('Could Not Open the Database'))
        message = str(error)
        backup = getattr(error, 'backup_path', None)
        if backup:
            message += '\n\n' + _('Database backup: {0}').format(backup)
        self.page.set_description(message)
        return False
