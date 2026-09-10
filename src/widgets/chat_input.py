from typing import List, Optional, Any, Dict, Callable
from gi.repository import Gtk, GObject, Gio, GdkPixbuf, GLib, Gdk, Pango
import threading
from .. import ollama

@Gtk.Template(resource_path='/io/github/jackrabbithanna/Gnollama/widgets/chat_input.ui')
class ChatInput(Gtk.Box):
    __gtype_name__ = 'ChatInput'
    __gsignals__ = {'capabilities-changed': (GObject.SignalFlags.RUN_FIRST, None, ())}

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
        self._host = None
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
        desired = self._desired_thinking
        self._thinking_values = values
        self.thinking_dropdown.set_model(Gtk.StringList.new(labels))
        self.thinking_dropdown.set_selected(values.index(desired) if desired in values else 0)
        self.thinking_dropdown.set_sensitive(len(values) > 1)

    def get_thinking_value(self):
        index = self.thinking_dropdown.get_selected()
        return self._thinking_values[index] if index < len(self._thinking_values) else None

    def load_thinking_val(self, value):
        self._desired_thinking = value
        if value in self._thinking_values:
            self.thinking_dropdown.set_selected(self._thinking_values.index(value))

    def set_running(self, running):
        self._running = running
        self.send_button.set_icon_name('media-playback-stop-symbolic' if running else 'mail-send-symbolic')
        self.send_button.set_tooltip_text(_('Stop response') if running else _('Send Message'))
        blocked = ((self.image_support is False and bool(self.selected_image_paths)) or
                   (self.capabilities_loading and (bool(self.selected_image_paths) or self.has_history_images)))
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
            if self.selected_image_paths:
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
        # GtkDropDown selects the first row even when asked to clear selection.
        # Keep unavailable saved models from silently becoming a different model.
        self._model_placeholder = bool(models and pending and pending not in models)
        labels = [_('Select a chat model')] + models if self._model_placeholder else models
        self.model_dropdown.set_model(Gtk.StringList.new(labels))
        if pending in models:
            self.select_model(pending)
        self.pending_model_selection = None
        self.set_running(self._running)

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
        for cancellable in (self._fetch_cancel, self._capability_cancel):
            if cancellable is not None:
                cancellable.cancel()

    def fetch_models(self, host):
        if host == self._host and not getattr(self, 'pending_model_selection', None):
            self.pending_model_selection = self.get_selected_model()
        self.cancel_fetches()
        self._host = host
        request_id = self._fetch_id
        self._model_details = {}
        self._model_list_notice = _('Checking chat models…') if host else ''
        self._model_placeholder = False
        # Clear old models immediately, but keep the desired saved selection.
        self.model_dropdown.set_model(Gtk.StringList.new([]))
        self.update_capability_controls()
        if not host:
            return
        cancel = self._fetch_cancel = Gio.Cancellable()

        def deliver(models, details, error):
            if request_id == self._fetch_id:
                self._model_details = details
                self._model_list_notice = error or ('' if models else
                    _('No chat models found. Pull one in Manage Models, then refresh.'))
                self.set_models(models)
                self.update_capability_controls()
            return False

        def fetch():
            models, details, error = [], {}, None
            try:
                for model in ollama.fetch_models(host, cancellable=cancel):
                    if cancel.is_cancelled():
                        return
                    try:
                        info = ollama.show_model(host, model, cancellable=cancel)
                    except ollama.RequestCancelled:
                        return
                    except ollama.OllamaError:
                        info = None  # Older hosts may not expose capabilities.
                    capabilities = info.get('capabilities') if isinstance(info, dict) else None
                    if (isinstance(capabilities, list) and 'embedding' in capabilities
                            and 'completion' not in capabilities):
                        continue
                    models.append(model)
                    details[model] = info
            except ollama.RequestCancelled:
                return
            except ollama.OllamaError as exc:
                error = str(exc)
            GLib.idle_add(deliver, models, details, error)
        from ..session import worker
        worker.submit(fetch)

    def _model_changed(self, *args):
        self._capability_id += 1
        request_id = self._capability_id
        if self._capability_cancel is not None:
            self._capability_cancel.cancel()
        model, host = self.get_selected_model(), self._host
        if model in self._model_details:
            self.set_model_details(self._model_details[model])
            return
        self.set_model_details(None, loading=bool(model and host))
        if not model or not host:
            return
        cancel = self._capability_cancel = Gio.Cancellable()

        def deliver(details):
            if request_id == self._capability_id:
                self.set_model_details(details)
            return False

        def fetch():
            try:
                details = ollama.show_model(host, model, cancellable=cancel)
            except ollama.RequestCancelled:
                return
            except ollama.OllamaError:
                details = None  # Older hosts retain manual controls.
            GLib.idle_add(deliver, details)
        from ..session import worker
        worker.submit(fetch)

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
                    for i in range(files.get_n_items()):
                        file = files.get_item(i)
                        path = file.get_path()
                        if path and path not in self.selected_image_paths:
                            self.selected_image_paths.append(path)
                    self.update_image_preview()
            except GLib.Error as e:
                if not (e.domain == 'gtk-dialog-error-quark' and e.code == 2):
                    print(f"Error selecting files: {e}")
                    
        dialog.open_multiple(parent_window, None, on_files_selected)

    def update_image_preview(self) -> None:
        """Updates the image preview UI based on selected paths."""
        self.update_capability_controls()
        child = self.image_preview_box.get_first_child()
        while child:
            next_child = child.get_next_sibling()
            self.image_preview_box.remove(child)
            child = next_child

        if not self.selected_image_paths:
            self.image_preview_scrolled.set_visible(False)
            self.image_label.set_text(_("No image selected"))
            self.clear_image_button.set_visible(False)
            return

        self.image_preview_scrolled.set_visible(True)
        count = len(self.selected_image_paths)
        self.image_label.set_text(_("{0} image(s) selected").format(count))
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

    def on_remove_single_image_clicked(self, btn: Gtk.Button, path: str) -> None:
        """Removes a single image from the selection."""
        if path in self.selected_image_paths:
            self.selected_image_paths.remove(path)
            self.update_image_preview()

    def on_clear_image_clicked(self, btn: Optional[Gtk.Button]) -> None:
        """Clears all selected images."""
        self.selected_image_paths = []
        self.update_image_preview()
