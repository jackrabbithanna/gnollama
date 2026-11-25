"""The runs of an image session: what was asked of which model, how it ended, and the images it made."""
from gettext import gettext as _
import base64
from decimal import Decimal
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk
from ..image_generation import LABELS, OPTIONS, extension
from ..model_summary import amount
from .decision_answers import line
from .decision_images import Bounded, load, texture_of, thumbnail
from .feedback import toast

# The longest side an image is shown at, in pixels; small enough for two to a row in a narrow window.
SHOWN = 256


def settings_text(options):
    """The settings a run was sent with, as named in the form, in the order the form shows them."""
    names = [name for name in OPTIONS if name in options] + sorted(name for name in options if name not in OPTIONS)
    return ' · '.join(f'{_(LABELS[name]) if name in LABELS else name}: {options[name]}' for name in names)


class ImageCard(Gtk.Box):
    """One generated image and what can be done with it. An image that cannot be read can still be saved."""
    def __init__(self, image, index, description, submit=None, use=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.image, self.index = image, index
        self.submit, self.use = submit, use
        self.seen = None
        self.allowed = False
        self._cancel = Gio.Cancellable()
        self.connect('destroy', lambda *args: self._cancel.cancel())
        self.picture = Gtk.Picture(can_shrink=False, halign=Gtk.Align.START, alternative_text=description,
                                   visible=False)
        self.append(self.picture)
        self.placeholder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, visible=False,
                                   width_request=SHOWN // 2)
        self.placeholder.append(Gtk.Image(icon_name='image-missing-symbolic', pixel_size=48))
        self.placeholder.append(line(_('This image cannot be shown here. Use Save Image… to open it in another app.'), 'dim-label'))
        self.append(self.placeholder)
        buttons = Gtk.Box(spacing=6)
        self.save_button = self._button(buttons, 'document-save-as-symbolic', _('Save Image…'), self.save)
        self.copy_button = self._button(buttons, 'edit-copy-symbolic', _('Copy Image'), self.copy)
        self.use_button = self._button(buttons, 'insert-image-symbolic', _('Use as Reference'), self.reference)
        self.append(buttons)
        self._update_buttons()
        load(image['data'], self._shown, submit)

    @staticmethod
    def _button(box, icon, label, callback):
        button = Gtk.Button(icon_name=icon, tooltip_text=label, css_classes=['flat'])
        button.update_property([Gtk.AccessibleProperty.LABEL], [label])
        button.connect('clicked', lambda *args: callback())
        box.append(button)
        return button

    def _shown(self, seen):
        self.seen = seen
        if seen is None:
            self.placeholder.set_visible(True)
        else:
            self.picture.set_paintable(Bounded(seen.texture, SHOWN))
            self.picture.set_visible(True)
        self._update_buttons()

    def allow_reference(self, allowed):
        self.allowed = allowed
        self._update_buttons()

    def _update_buttons(self):
        self.copy_button.set_sensitive(self.seen is not None)
        self.use_button.set_sensitive(self.seen is not None and self.allowed and self.use is not None)

    def file_name(self):
        return f'image-{self.index + 1}.{extension(self.image["media_type"])}'

    def save(self):
        dialog = Gtk.FileDialog(title=_('Save Image'), initial_name=self.file_name())
        def selected(dialog, result):
            try:
                self.write(dialog.save_finish(result))
            except GLib.Error as exc:
                if not exc.matches(Gtk.dialog_error_quark(), Gtk.DialogError.DISMISSED):
                    toast(self, exc.message)
        dialog.save(self.get_root(), self._cancel, selected)

    def write(self, file, done=lambda error: None):
        """Write the image as it came, not as it is shown; `done` gets the error, or None."""
        def saved(file, result):
            try:
                file.replace_contents_finish(result)
                error = None
            except GLib.Error as exc:
                error = exc.message
                if not self._cancel.is_cancelled():
                    toast(self, error)
            done(error)
        file.replace_contents_bytes_async(GLib.Bytes.new(base64.b64decode(self.image['data'])), None, False,
                                          Gio.FileCreateFlags.NONE, self._cancel, saved)

    def copy(self, done=lambda: None):
        """Put the image, at its full size, on the clipboard; read on a worker where there is one."""
        def full():
            try:
                return texture_of(self.image['data'])
            except (ValueError, GLib.Error):
                return None

        def ready(texture):
            if texture is not None:
                # As a texture, not its subtype, so that it is offered as image data.
                self.get_clipboard().set_content(Gdk.ContentProvider.new_for_value(GObject.Value(Gdk.Texture, texture)))
                toast(self, _('Copied'))
            done()
            return False
        if self.submit:
            self.submit(lambda: GLib.idle_add(ready, full()))
        else:
            ready(full())

    def reference(self):
        if self.use_button.get_sensitive():
            self.use(self.image['data'])


class ImageRunView(Gtk.Box):
    """One run: its prompt, references, settings and cost, how it ended, and its images.

    With `submit`, which runs a function on a worker, images are read there. `use(image)` adds an
    image to the editor's references, where `allow_references` allows it.
    """
    def __init__(self, run, submit=None, use=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.submit, self.use = submit, use
        self.allowed = False
        self.title = line('', 'heading')
        self.spinner = Gtk.Spinner(visible=False)
        head = Gtk.Box(spacing=8)
        head.append(self.spinner)
        head.append(self.title)
        self.append(head)
        self.prompt = line('')
        self.append(self.prompt)
        self.references = Adw.WrapBox(child_spacing=6, line_spacing=6, visible=bool(run.get('references')))
        for image in run.get('references') or []:
            self.references.append(thumbnail(image, _('Image sent with this run'), submit))
        self.append(self.references)
        self.settings = line('', 'dim-label', 'caption')
        self.append(self.settings)
        self.gallery = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, column_spacing=12, row_spacing=12,
                                   min_children_per_line=1, max_children_per_line=4, homogeneous=False)
        self.append(self.gallery)
        self.text = line('')
        self.append(self.text)
        self.usage = line('', 'dim-label', 'caption')
        self.append(self.usage)
        self.error = line('', 'error')
        self.append(self.error)
        self.cards = []
        self.update(run)

    def update(self, run):
        self.run = run
        status = run['status']
        target = run.get('target') or {}
        ended = {'failed': _('Failed'), 'cancelled': _('Cancelled'), 'interrupted': _('Interrupted'),
                 'running': _('Running…')}.get(status)
        parts = [_('Run {0}').format(run['position'] + 1), target.get('model'), target.get('host_name'), ended]
        self.title.set_text(' · '.join(part for part in parts if part))
        self.spinner.set_visible(status == 'running')
        self.spinner.set_spinning(status == 'running')
        self.prompt.set_text(run['prompt'])
        self.settings.set_text(settings_text(run.get('options') or {}))
        self.settings.set_visible(bool(self.settings.get_text()))
        self.gallery.remove_all()
        self.cards = [ImageCard(image, index, run['prompt'], self.submit, self.use)
                      for index, image in enumerate(run.get('images') or [])]
        for card in self.cards:
            card.allow_reference(self.allowed)
            self.gallery.append(card)
        self.gallery.set_visible(bool(self.cards))
        self.text.set_text(run.get('text') or '')
        self.text.set_visible(bool(self.text.get_text()))
        cost = (run.get('result') or {}).get('cost')
        # Translators: what a request cost, in US dollars.
        self.usage.set_text(_('Cost: ${0}').format(amount(Decimal(str(cost)))) if isinstance(cost, (int, float)) else '')
        self.usage.set_visible(bool(self.usage.get_text()))
        error = run.get('error') or ''
        self.error.set_text(error)
        self.error.set_visible(bool(error))

    def allow_references(self, allowed):
        """Offer each image as a reference, or not, as the selected model takes them."""
        self.allowed = allowed
        for card in self.cards:
            card.allow_reference(allowed)
