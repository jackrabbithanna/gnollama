from typing import List, Optional, Any, Dict, Callable
from gettext import ngettext
from gi.repository import Gtk, GObject, Gio, GdkPixbuf, GLib, Gdk, Pango
import threading
from .. import ollama
from .composer import Composer

@Gtk.Template(resource_path='/io/github/jackrabbithanna/Gnollama/widgets/chat_input.ui')
class ChatInput(Gtk.Box):
    __gtype_name__ = 'ChatInput'
    __gsignals__ = {'capabilities-changed': (GObject.SignalFlags.RUN_FIRST, None, ()),
                    'attachments-ready': (GObject.SignalFlags.RUN_FIRST, None, ())}

    connection_box = Gtk.Template.Child()
    model_dropdown: Gtk.DropDown = Gtk.Template.Child()
    thinking_dropdown: Gtk.DropDown = Gtk.Template.Child()
    entry: Gtk.Entry = Gtk.Template.Child()
    send_button: Gtk.Button = Gtk.Template.Child()
    
    image_preview_scrolled: Gtk.ScrolledWindow = Gtk.Template.Child()
    image_preview_box: Gtk.Box = Gtk.Template.Child()
    attach_button: Gtk.Button = Gtk.Template.Child()
    image_label: Gtk.Label = Gtk.Template.Child()
    clear_image_button: Gtk.Button = Gtk.Template.Child()
    capability_notice = Gtk.Template.Child()

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        
        self.selected_image_paths = []
        self.restored_images = []
        self.pending_imports = 0
        self._image_generation = 0
        self.entry.set_placeholder_text(_('Type a message...'))
        self._host = None
        self._host_identity = None
        self.discovery_enabled = True
        self._models_loading = False
        self._setting_models = False
        self._selection_explicit = False
        self.services = None
        self._fetch_id = 0
        self._capability_id = 0
        self._fetch_cancel = None
        self._capability_cancel = None
        self._model_details = {}
        self._model_list_notice = ''
        self._model_placeholder = False
        self._running = False
        self._desired_thinking = None
        self._thinking_values = []
        self.image_support = None
        self.tool_support = None
        self.awaiting_tools = False
        self.capabilities_loading = False
        self.has_history_images = False
        factory = Gtk.SignalListItemFactory()
        factory.connect('setup', lambda f, item: item.set_child(Gtk.Label(ellipsize=Pango.EllipsizeMode.END, max_width_chars=24, xalign=0)))
        factory.connect('bind', lambda f, item: item.get_child().set_text(item.get_item().get_string()))
        self.model_dropdown.set_factory(factory)
        self.model_dropdown.set_enable_search(True)
        click = Gtk.GestureClick(button=1)
        click.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        click.connect('pressed', self._retry_empty_models)
        self.model_dropdown.add_controller(click)
        focus = Gtk.EventControllerFocus()
        focus.connect('enter', self._retry_empty_models)
        self.model_dropdown.add_controller(focus)
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect('key-pressed', self._model_key_pressed)
        self.model_dropdown.add_controller(keys)
        self._set_thinking_options(None)
        self.attach_button.connect('clicked', self.on_attach_clicked)
        self.clear_image_button.connect('clicked', self.on_clear_image_clicked)
        self.model_dropdown.connect('notify::selected-item', self._model_changed)
        self.thinking_dropdown.connect('notify::selected-item', self._thinking_changed)

    def _thinking_changed(self, *args):
        self._desired_thinking = self.get_thinking_value()

    def _set_thinking_options(self, details):
        values = [None, False, True, 'low', 'medium', 'high', 'max']
        labels = [_('Model default'), _('Off'), _('On'), _('Low'), _('Medium'), _('High'), _('Max')]
        if details is not None:
            family = details.get('details', {}).get('family', '').replace('-', '').lower()
            if family == 'gptoss':
                values = [None, 'low', 'medium', 'high']
                labels = [_('Model default'), _('Low'), _('Medium'), _('High')]
            elif 'capabilities' in details and 'thinking' not in details['capabilities']:
                values, labels = [None], [_('Model default')]
        self._setting_thinking = True
        desired = self._desired_thinking
        self._thinking_values = values
        self.thinking_dropdown.set_model(Gtk.StringList.new(labels))
        self.thinking_dropdown.set_selected(values.index(desired) if desired in values else 0)
        self.thinking_dropdown.set_sensitive(len(values) > 1)
        self._setting_thinking = False

    def get_thinking_value(self):
        index = self.thinking_dropdown.get_selected()
        return self._thinking_values[index] if index < len(self._thinking_values) else None

    def load_thinking_val(self, value):
        self._desired_thinking = value
        if value in self._thinking_values:
            self.thinking_dropdown.set_selected(self._thinking_values.index(value))

    def set_running(self, running):
        self._running = running
        self.send_button.set_icon_name('media-playback-stop-symbolic' if running else 'system-search-symbolic')
        self.send_button.set_tooltip_text(_('Stop response') if running else _('Send Message'))
        blocked = (self.pending_imports or self.capabilities_loading or
                   (self.image_support is False and self.has_draft_images()))
        self.send_button.set_sensitive(running or (self.get_selected_model() is not None and not blocked and not self.awaiting_tools))
        self.entry.set_sensitive(not self.awaiting_tools)

    def set_model_details(self, details, loading=False):
        self.capabilities_loading = loading
        capabilities = details.get('capabilities') if isinstance(details, dict) else None
        self.image_support = ('vision' in capabilities) if isinstance(capabilities, list) else None
        self.tool_support = ('tools' in capabilities) if isinstance(capabilities, list) else None
        self._set_thinking_options(details)
        self.update_capability_controls()
        self.emit('capabilities-changed')

    def update_capability_controls(self):
        self.attach_button.set_sensitive(self.get_selected_model() is not None and
                                         not self.capabilities_loading and self.image_support is not False and not self.awaiting_tools)
        if not self.get_selected_model():
            notice = self._model_list_notice
        elif self.capabilities_loading:
            notice = _('Checking image support…')
        elif self.image_support is False:
            notice = _('This model does not support images.')
            if self.has_draft_images():
                notice += ' ' + _('Remove draft images or select a vision model to send.')
            if self.has_history_images:
                notice += ' ' + _('Earlier images remain saved; only text history will be sent.')
        elif self.image_support is None:
            notice = _('Image support unknown.') if self.get_selected_model() else ''
        else:
            notice = ''
        self.capability_notice.set_text(notice)
        self.capability_notice.set_visible(bool(notice))
        self.set_running(self._running)

    def set_models(self, models):
        pending = getattr(self, 'pending_model_selection', None)
        self._selection_explicit = bool(pending)
        self._model_placeholder = bool(models and pending and pending not in models)
        labels = [_('Select a chat model')] + models if self._model_placeholder else models
        self._setting_models = True
        try:
            self.model_dropdown.set_model(Gtk.StringList.new(labels))
            if pending in models:
                self.select_model(pending)
                self.pending_model_selection = None
        finally:
            self._setting_models = False
        self._model_changed(preserve_selection=True)

    def select_model(self, model_name):
        model = self.model_dropdown.get_model()
        if model:
            for i in range(model.get_n_items()):
                if model.get_item(i).get_string() == model_name:
                    self.model_dropdown.set_selected(i)
                    return

    def get_selected_model(self):
        if self._model_placeholder and self.model_dropdown.get_selected() == 0:
            return None
        item = self.model_dropdown.get_selected_item()
        return item.get_string() if item else None

    def cancel_fetches(self):
        self._fetch_id += 1
        self._capability_id += 1
        self._models_loading = False
        for cancellable in (self._fetch_cancel, self._capability_cancel):
            if cancellable is not None:
                cancellable.cancel()

    def _retry_empty_models(self, *args):
        if self.discovery_enabled and self._host and not self.get_selected_model() and not self._models_loading:
            self.fetch_models(self._host, refresh=True, host_id=self._host_identity)

    def _model_key_pressed(self, controller, key, code, state):
        if key in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_space, Gdk.KEY_Down, Gdk.KEY_F4):
            self._retry_empty_models()
        return False

    @staticmethod
    def _embedding_only(details):
        caps = details.get('capabilities') if isinstance(details, dict) else None
        return isinstance(caps, list) and 'embedding' in caps and 'completion' not in caps

    def _empty_model_notice(self):
        return (_('No cloud models are available. Try refreshing later.') if ollama.is_cloud(self._host) else
                _('No chat models found. Pull one in Manage Models, then refresh.'))

    def fetch_models(self, host, refresh=False, host_id=None):
        if not self.discovery_enabled:
            return
        identity = host_id or (host.host_id if isinstance(host, ollama.Connection) else host)
        if identity == self._host_identity and not getattr(self, 'pending_model_selection', None):
            self.pending_model_selection = self.get_selected_model()
        self.cancel_fetches()
        self._host = host
        self._host_identity = identity
        request_id = self._fetch_id
        self._model_details = {}
        self._model_list_notice = _('Checking chat models…') if host else ''
        self._model_placeholder = False
        self._models_loading = bool(host)
        self._setting_models = True
        self.model_dropdown.set_model(Gtk.StringList.new([]))
        self._setting_models = False
        self.set_model_details(None)
        if not host:
            return
        cancel = self._fetch_cancel = Gio.Cancellable()

        def deliver(models, details, error):
            if request_id == self._fetch_id and not cancel.is_cancelled():
                self._models_loading = False
                self._model_details = details
                self._model_list_notice = error or ('' if models else self._empty_model_notice())
                self.set_models(models)
                self.update_capability_controls()
            return False

        def finished(future):
            models, details, error = [], {}, None
            try:
                for tag in future.result():
                    model = tag['name'] if isinstance(tag, dict) else tag
                    info = self.services.catalog.cached_details(host, model) if self.services else None
                    if self._embedding_only(info):
                        continue
                    models.append(model)
                    if info is not None:
                        details[model] = info
            except ollama.RequestCancelled:
                return
            except Exception as exc:
                error = str(exc)
            GLib.idle_add(deliver, models, details, error)
        if self.services:
            future = self.services.catalog.request_models(host, cancel, refresh)
        else:
            from ..session import worker
            future = worker.submit(ollama.fetch_models, host, cancellable=cancel)
        future.add_done_callback(finished)

    def _model_changed(self, *args, preserve_selection=False):
        if self._setting_models:
            return
        self._capability_id += 1
        request_id = self._capability_id
        if self._capability_cancel is not None:
            self._capability_cancel.cancel()
        model, host = self.get_selected_model(), self._host
        if model and not preserve_selection:
            self._selection_explicit = True
            self.pending_model_selection = None
        cached = self._model_details.get(model)
        if self.services:
            cached = self.services.catalog.cached_details(host, model) if host and model else None
        if cached is not None or (not self.services and model in self._model_details):
            self.set_model_details(cached)
            return
        self.set_model_details(None, loading=bool(model and host))
        if not model or not host:
            return
        cancel = self._capability_cancel = Gio.Cancellable()

        def deliver(details):
            if request_id != self._capability_id or cancel.is_cancelled():
                return False
            self._model_details[model] = details
            if self._embedding_only(details):
                values = self.model_dropdown.get_model()
                remaining = [values.get_string(i) for i in range(values.get_n_items())
                             if values.get_string(i) != model and not (self._model_placeholder and i == 0)]
                if self._selection_explicit:
                    self.pending_model_selection = model
                self._model_list_notice = '' if remaining else self._empty_model_notice()
                self.set_models(remaining)
            else:
                self.set_model_details(details)
            return False

        def finished(future):
            try:
                details = future.result()
            except ollama.RequestCancelled:
                return
            except Exception:
                details = None  # Older hosts retain manual controls.
            GLib.idle_add(deliver, details)
        if self.services:
            future = self.services.catalog.request_details(host, model, cancel)
        else:
            from ..session import worker
            future = worker.submit(ollama.show_model, host, model, cancellable=cancel)
        future.add_done_callback(finished)

    def on_attach_clicked(self, btn: Gtk.Button) -> None:
        """Opens a file chooser to attach one or multiple images."""
        if not self.attach_button.get_sensitive():
            return
        parent_window = self.get_root()
        if not isinstance(parent_window, Gtk.Window):
            return

        dialog = Gtk.FileDialog()
        dialog.set_title(_("Select Images"))
        
        filters = Gio.ListStore.new(Gtk.FileFilter)
        image_filter = Gtk.FileFilter()
        image_filter.set_name(_("Images"))
        image_filter.add_mime_type("image/png")
        image_filter.add_mime_type("image/jpeg")
        filters.append(image_filter)
        dialog.set_filters(filters)
        
        def on_files_selected(dialog: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
            try:
                files = dialog.open_multiple_finish(result)
                if files:
                    paths = [files.get_item(i).get_path() for i in range(files.get_n_items())]
                    self.import_images([path for path in paths if path])
            except GLib.Error as e:
                if not (e.domain == 'gtk-dialog-error-quark' and e.code == 2):
                    print(f"Error selecting files: {e}")
                    
        dialog.open_multiple(parent_window, None, on_files_selected)

    def import_images(self, paths):
        import base64
        self.pending_imports += 1
        self.set_running(self._running)
        generation = self._image_generation
        def read():
            images, errors = [], []
            for path in paths:
                try:
                    with open(path, 'rb') as stream:
                        raw = stream.read(50 * 1024 * 1024 + 1)
                    if len(raw) > 50 * 1024 * 1024:
                        raise ValueError(_('Files must be no larger than 50 MiB.'))
                    Gdk.Texture.new_from_bytes(GLib.Bytes.new(raw))
                    images.append(base64.b64encode(raw).decode('ascii'))
                except (OSError, ValueError, GLib.Error) as exc:
                    errors.append(str(exc))
            def deliver():
                if generation == self._image_generation:
                    self.restored_images.extend(images)
                    self.update_image_preview()
                self.pending_imports -= 1
                self.update_capability_controls()
                self.emit('attachments-ready')
                if errors:
                    from .feedback import toast
                    toast(self, '\n'.join(errors))
                return False
            GLib.idle_add(deliver)
        from ..session import worker
        (self.services.control if self.services else worker).submit(read)

    def get_images(self):
        import base64
        images = list(self.restored_images)
        for path in self.selected_image_paths:
            with open(path, 'rb') as stream:
                raw = stream.read(50 * 1024 * 1024 + 1)
            if len(raw) > 50 * 1024 * 1024:
                raise ValueError(_('Files must be no larger than 50 MiB.'))
            Gdk.Texture.new_from_bytes(GLib.Bytes.new(raw))
            images.append(base64.b64encode(raw).decode('ascii'))
        return images

    def has_draft_images(self):
        return bool(self.selected_image_paths or self.restored_images)

    def restore_images(self, images):
        self._image_generation += 1
        self.selected_image_paths = []
        self.restored_images = list(images)
        self.update_image_preview()

    def update_image_preview(self) -> None:
        """Updates the image preview UI based on selected paths."""
        self.update_capability_controls()
        child = self.image_preview_box.get_first_child()
        while child:
            next_child = child.get_next_sibling()
            self.image_preview_box.remove(child)
            child = next_child

        if not self.has_draft_images():
            self.image_preview_scrolled.set_visible(False)
            self.image_label.set_text(_("No image selected"))
            self.clear_image_button.set_visible(False)
            self.entry.emit('changed')
            return

        self.image_preview_scrolled.set_visible(True)
        count = len(self.selected_image_paths) + len(self.restored_images)
        self.image_label.set_text(ngettext("{0} image selected", "{0} images selected", count).format(count))
        self.clear_image_button.set_visible(True)

        for path in self.selected_image_paths:
            try:
                with open(path, "rb") as f:
                    data = f.read()
                texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(data))
                img_widget = Gtk.Picture.new_for_paintable(texture)
                img_widget.set_size_request(80, 80)
                img_widget.set_content_fit(Gtk.ContentFit.SCALE_DOWN)
                
                remove_btn = Gtk.Button.new_from_icon_name("window-close-symbolic")
                remove_btn.add_css_class("osd")
                remove_btn.add_css_class("circular")
                remove_btn.set_valign(Gtk.Align.START)
                remove_btn.set_halign(Gtk.Align.END)
                remove_btn.connect("clicked", self.on_remove_single_image_clicked, path)
                
                overlay = Gtk.Overlay()
                overlay.set_child(img_widget)
                overlay.add_overlay(remove_btn)
                
                self.image_preview_box.append(overlay)
            except GLib.Error as e:
                print(f"Error loading image preview for {path}: {e}")

        import base64
        for index, encoded in enumerate(self.restored_images):
            try:
                texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(base64.b64decode(encoded)))
                picture = Gtk.Picture.new_for_paintable(texture)
                picture.set_size_request(80, 80)
                picture.set_content_fit(Gtk.ContentFit.SCALE_DOWN)
                remove = Gtk.Button(icon_name='window-close-symbolic', halign=Gtk.Align.END, valign=Gtk.Align.START)
                remove.set_tooltip_text(_('Clear Image'))
                overlay = Gtk.Overlay(child=picture)
                overlay.add_overlay(remove)
                remove.connect('clicked', lambda button, n=index: self._remove_restored(n))
                self.image_preview_box.append(overlay)
            except (ValueError, GLib.Error):
                self.capability_notice.set_text(_('Could not restore an image. Remove it before sending.'))
                self.capability_notice.set_visible(True)
        self.entry.emit('changed')

    def _remove_restored(self, index):
        del self.restored_images[index]
        self.update_image_preview()

    def on_remove_single_image_clicked(self, btn: Gtk.Button, path: str) -> None:
        """Removes a single image from the selection."""
        if path in self.selected_image_paths:
            self.selected_image_paths.remove(path)
            self.update_image_preview()

    def on_clear_image_clicked(self, btn: Optional[Gtk.Button]) -> None:
        """Clears all selected images."""
        self._image_generation += 1
        self.selected_image_paths = []
        self.restored_images = []
        self.update_image_preview()
