"""Multiline text input with an explicit draft interface and IME-safe sending."""
from gi.repository import Gtk, Gdk, GObject, Pango


class Composer(Gtk.Box):
    __gtype_name__ = 'GnollamaComposer'
    __gsignals__ = {'activate': (GObject.SignalFlags.RUN_FIRST, None, ()),
                    'changed': (GObject.SignalFlags.RUN_FIRST, None, ())}

    def __init__(self, **kwargs):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, **kwargs)
        self._preedit = False
        self.send_on_enter = True
        self.view = Gtk.TextView(hexpand=True, wrap_mode=Gtk.WrapMode.WORD_CHAR, top_margin=8, bottom_margin=8,
                                 left_margin=8, right_margin=8, accepts_tab=False)
        self.view.connect('preedit-changed', lambda view, text: setattr(self, '_preedit', bool(text)))
        self.scrolled = Gtk.ScrolledWindow(hexpand=True, child=self.view, hscrollbar_policy=Gtk.PolicyType.NEVER,
            min_content_height=40, max_content_height=184, propagate_natural_height=True)
        overlay = Gtk.Overlay(hexpand=True, child=self.scrolled)
        self.placeholder = Gtk.Label(xalign=0, yalign=0, margin_start=8, margin_top=8,
                                     can_target=False, ellipsize=Pango.EllipsizeMode.END)
        self.placeholder.add_css_class('dim-label')
        overlay.add_overlay(self.placeholder)
        self.append(overlay)
        self.add_css_class('editor-frame')
        self.view.get_buffer().connect('changed', self._changed)
        self.keys = Gtk.EventControllerKey()
        self.keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        self.keys.connect('key-pressed', self._key_pressed)
        self.view.add_controller(self.keys)

    def _key_pressed(self, controller, keyval, keycode, state):
        if keyval not in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) or self._preedit:
            return False
        if state & Gdk.ModifierType.SHIFT_MASK:
            return False
        if self.send_on_enter or state & Gdk.ModifierType.CONTROL_MASK:
            self.emit('activate')
            return True
        return False

    def _changed(self, buffer):
        self.placeholder.set_visible(not buffer.get_char_count())
        self.emit('changed')

    def read_draft(self):
        buffer = self.view.get_buffer()
        return buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)

    def restore_draft(self, text):
        self.view.get_buffer().set_text(text or '')

    def clear_draft(self):
        self.restore_draft('')

    # Compatibility for imported settings and callers migrating from Gtk.Entry.
    get_text = read_draft
    set_text = restore_draft

    def set_placeholder_text(self, text):
        self.placeholder.set_text(text)

    def set_width_chars(self, width):
        pass

    def grab_focus(self):
        return self.view.grab_focus()
