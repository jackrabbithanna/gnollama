from typing import Any, Dict, List, Optional
from gettext import gettext as _, ngettext
from gi.repository import Gtk, GObject, Gio, GLib, Gdk, Pango
from ..model_summary import prices, summary_parts
from ..providers import RequestCancelled, generates, supports
from .search import searchable
from .composer import Composer  # noqa: F401 - registers the type chat_input.ui names

@Gtk.Template(resource_path='/io/github/jackrabbithanna/Tureen/widgets/chat_input.ui')
class ChatInput(Gtk.Box):
    __gtype_name__ = 'ChatInput'
    __gsignals__ = {'capabilities-changed': (GObject.SignalFlags.RUN_FIRST, None, ()),
                    'attachments-ready': (GObject.SignalFlags.RUN_FIRST, None, ())}

    connection_box = Gtk.Template.Child()
    model_dropdown: Gtk.DropDown = Gtk.Template.Child()
    thinking_dropdown: Gtk.DropDown = Gtk.Template.Child()
    model_summary: Gtk.Label = Gtk.Template.Child()
    model_summary_cell = Gtk.Template.Child()
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
        
        self.selected_image_paths: List[str] = []
        self.restored_images: List[str] = []
        self.pending_imports = 0
        self._image_generation = 0
        self.entry.set_placeholder_text(_('Type a message...'))
        self._host = None
        self._host_identity = None
        self.discovery_enabled = True
        # Which list of the host's models is offered: 'chat', or 'decision' or 'image', whose
        # models are each described by their entry in that list.
        self.kind = 'chat'
        self._models_loading = False
        self._setting_models = False
        self._selection_explicit = False
        self.pending_model_selection = None  # The model to select once the list that holds it arrives.
        self.services = None
        self._fetch_id = 0
        self._capability_id = 0
        self._fetch_cancel = None
        self._capability_cancel = None
        self._model_details: Dict[str, Any] = {}
        self._model_list_notice = ''
        self._model_placeholder = False
        self._running = False
        self._desired_thinking = None
        self._thinking_values: List[Any] = []
        self.image_support = None
        # Set where another widget chooses the model: why images cannot be attached, or '' where they can.
        self.image_refusal = None
        self.tool_support = None
        self.awaiting_tools = False
        self.capabilities_loading = False
        self.unavailable = ''  # Why this host cannot be sent to at all, if it cannot.
        self.has_history_images = False
        factory = Gtk.SignalListItemFactory()
        factory.connect('setup', lambda f, item: item.set_child(Gtk.Label(ellipsize=Pango.EllipsizeMode.END, max_width_chars=24, xalign=0)))
        factory.connect('bind', lambda f, item: item.get_child().set_text(item.get_item().get_string()))
        self.model_dropdown.set_factory(factory)
        searchable(self.model_dropdown)
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
        self.entry.connect('paste-attachment', self.on_paste_attachment)
        self.model_dropdown.connect('notify::selected-item', self._model_changed)
        self.thinking_dropdown.connect('notify::selected-item', self._thinking_changed)

    def _thinking_changed(self, *args):
        self._desired_thinking = self.get_thinking_value()

    def reset_thinking_options(self):
        """Offer every thinking level, as before a model's details are known."""
        self._set_thinking_options(None)

    def _set_thinking_options(self, details):
        values = [None, False, True, 'low', 'medium', 'high', 'max']
        labels = [_('Model default'), _('Off'), _('On'), _('Low'), _('Medium'), _('High'), _('Max')]
        if details is not None:
            family = details.get('details', {}).get('family', '').replace('-', '').lower()
            if details.get('thinking_levels') is not None:
                offered = [(value, label) for value, label in zip(values, labels)
                           if value is None or value in details['thinking_levels']]
                values, labels = [v for v, _label in offered], [label for _v, label in offered]
            elif family == 'gptoss':
                values = [None, 'low', 'medium', 'high']
                labels = [_('Model default'), _('Low'), _('Medium'), _('High')]
            elif supports(details, 'thinking') is False:
                values, labels = [None], [_('Model default')]
        self._setting_thinking = True
        desired = self._desired_thinking
        self._thinking_values = values
        self.thinking_dropdown.set_model(Gtk.StringList.new(labels))
        self.thinking_dropdown.set_selected(values.index(desired) if desired in values else 0)
        self.thinking_dropdown.set_sensitive(len(values) > 1)
        self._setting_thinking = False

    def detach_thinking(self):
        """Hand the thinking control to a view that places it elsewhere."""
        row = self.thinking_dropdown.get_parent()
        cell = row.get_parent()
        cell.set_child(None)
        self.connection_box.remove(cell)
        return row

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
        blocked = (self.pending_imports or self.capabilities_loading or self.unavailable or
                   (self.image_support is False and self.has_draft_images()))
        self.send_button.set_sensitive(running or (self.get_selected_model() is not None and not blocked and not self.awaiting_tools))
        self.entry.set_sensitive(not self.awaiting_tools)

    def set_unavailable(self, reason):
        """Block sending with an explanation, or allow it again with ''."""
        self.unavailable = reason
        self.update_capability_controls()

    def set_model_details(self, details, loading=False):
        self.capabilities_loading = loading
        # None where the server did not say.
        self.image_support = supports(details, 'vision')
        self.tool_support = supports(details, 'tools')
        self._set_thinking_options(details)
        self._show_summary('' if loading else ' · '.join(summary_parts(details)), bool(prices(details)))
        self.update_capability_controls()
        self.emit('capabilities-changed')

    def _show_summary(self, text, priced):
        """The line beside the selector that describes the selected model; gone when there is nothing to say."""
        self.model_summary.set_text(text)
        self.model_summary.set_tooltip_text(
            _('Prompt and completion prices in US dollars per million tokens, as listed when the model list was read.')
            if text and priced else None)
        self.model_summary_cell.set_visible(bool(text))
        # Read out with the selector, which is where the focus is.
        self.model_dropdown.update_property([Gtk.AccessibleProperty.DESCRIPTION], [text])

    def update_capability_controls(self):
        self.attach_button.set_sensitive(self.get_selected_model() is not None and
                                         not self.capabilities_loading and self.image_support is not False and not self.awaiting_tools)
        if self.unavailable:
            notice = self.unavailable
        elif not self.get_selected_model():
            notice = self._model_list_notice
        elif self.capabilities_loading:
            notice = _('Checking image support…')
        elif self.image_support is False:
            # The summary beside the selector says the model takes no images; this says what that stops.
            needed = ([_('Remove draft images or select a vision model to send.')] if self.has_draft_images() else []) + \
                     ([_('Earlier images remain saved; only text history will be sent.')] if self.has_history_images else [])
            notice = ' '.join([_('This model does not support images.')] + needed) if needed else ''
        else:
            notice = ''
        refused = self.image_support is False and not self.capabilities_loading and self.get_selected_model()
        self.attach_button.set_tooltip_text(_('This model does not support images.') if refused else _('Attach Image'))
        if self.image_refusal is not None:
            self.attach_button.set_sensitive(self.image_support is True)
            self.attach_button.set_tooltip_text(self.image_refusal or _('Attach Image'))
        self.capability_notice.set_text(notice)
        self.capability_notice.set_visible(bool(notice))
        self.set_running(self._running)

    def set_image_support(self, supported, refusal=''):
        """Allow or refuse attachments for a model chosen in another widget; `refusal` says why not."""
        self.image_support, self.image_refusal = bool(supported), refusal
        self.update_capability_controls()

    def set_models(self, models):
        pending = getattr(self, 'pending_model_selection', None)
        self._selection_explicit = bool(pending)
        self._model_placeholder = bool(models and pending and pending not in models)
        placeholder = {'decision': _('Select a decision model'),
                       'image': _('Select an image model')}.get(self.kind) or _('Select a chat model')
        labels = [placeholder] + models if self._model_placeholder else models
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

    def selected_details(self):
        """What is known of the selected model: its details, or its list entry for a decision or image model;
        None if neither."""
        return self._model_details.get(self.get_selected_model())

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
    def _not_chat(details):
        """Whether details or a list entry say a model embeds or decides and does not chat."""
        return not generates(details)

    def _empty_model_notice(self):
        if self.kind == 'decision':
            return _('No decision models found on this server.')
        if self.kind == 'image':
            return _('No image models found on this server.')
        if self.kind == 'chat' and self._host is not None and self._host.empty_notice:
            return _(self._host.empty_notice)
        if self._host is None or self._host.capabilities.model_management:
            return _('No chat models found. Pull one in Manage Models, then refresh.')
        # A hosted service, whose models the user cannot add to.
        if self._host.fixed_url:
            return _('No cloud models are available. Try refreshing later.')
        return _('No models found on this server. Load one there, then refresh.')

    def fetch_models(self, host, refresh=False, host_id=None):
        if not self.discovery_enabled:
            return
        identity = host_id or (host.key if host else None)
        if identity == self._host_identity and not getattr(self, 'pending_model_selection', None):
            self.pending_model_selection = self.get_selected_model()
        self.cancel_fetches()
        self._host = host
        self._host_identity = identity
        request_id = self._fetch_id
        self._model_details = {}
        checking = {'decision': _('Checking decision models…'),
                    'image': _('Checking image models…')}.get(self.kind) or _('Checking chat models…')
        self._model_list_notice = checking if host else ''
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
                    if self.kind != 'chat':
                        models.append(model)
                        details[model] = tag
                        continue
                    info = self.services.catalog.cached_details(host, model)
                    # Some servers say so in the list itself.
                    if self._not_chat(info) or self._not_chat(tag):
                        continue
                    models.append(model)
                    if info is not None:
                        details[model] = info
            except RequestCancelled:
                return
            except Exception as exc:
                error = str(exc)
            # By name, whatever order the server lists them in.
            GLib.idle_add(deliver, sorted(models, key=str.casefold), details, error)
        catalog = self.services.catalog
        request = {'decision': catalog.request_decision_models,
                   'image': catalog.request_image_models}.get(self.kind, catalog.request_models)
        future = request(host, cancel, refresh)
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
        if self.kind != 'chat':
            # The list entry is all that is known of a decision or image model.
            self.set_model_details(self._model_details.get(model))
            return
        cached = self.services.catalog.cached_details(host, model) if host and model else None
        if cached is not None:
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
            if self._not_chat(details):
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
            except RequestCancelled:
                return
            except Exception:
                details = None  # Older hosts retain manual controls.
            GLib.idle_add(deliver, details)
        future = self.services.catalog.request_details(host, model, cancel)
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

    def on_paste_attachment(self, composer, clipboard, kind):
        """Attaches a pasted image or copied image files; other files paste as text."""
        if kind == 'image':
            def textured(clipboard, result):
                try:
                    texture = clipboard.read_texture_finish(result)
                except GLib.Error as exc:
                    self._toast(str(exc.message))
                    return
                if texture:
                    self.attach_pasted([texture])
            clipboard.read_texture_async(None, textured)
            return

        def text_read(clipboard, result):
            try:
                text = clipboard.read_text_finish(result)
            except GLib.Error:
                return
            if text:
                composer.insert_text(text)

        def files_read(clipboard, result):
            try:
                value = clipboard.read_value_finish(result)
                files = value.get_files() if value else []
            except GLib.Error:
                files = []
            paths = [f.get_path() for f in files if f.get_path() and
                     Gio.content_type_guess(f.get_basename(), None)[0].startswith('image/')]
            if paths:
                self.attach_pasted(paths)
            else:
                clipboard.read_text_async(None, text_read)
        clipboard.read_value_async(Gdk.FileList, GLib.PRIORITY_DEFAULT, None, files_read)

    def attach_pasted(self, sources):
        if self.image_support is False:
            self._toast(_('This model does not support images.'))
            return
        self.import_images(sources)

    def _toast(self, message):
        from .feedback import toast
        toast(self, message)

    def import_images(self, sources):
        """Adds draft images from file paths or Gdk.Texture objects."""
        import base64
        limit = 50 * 1024 * 1024
        self.pending_imports += 1
        self.set_running(self._running)
        generation = self._image_generation
        def read():
            images, errors = [], []
            for source in sources:
                try:
                    if isinstance(source, Gdk.Texture):
                        raw = source.save_to_png_bytes().get_data()
                    else:
                        with open(source, 'rb') as stream:
                            raw = stream.read(limit + 1)
                    if len(raw) > limit:
                        raise ValueError(_('Files must be no larger than 50 MiB.'))
                    if not isinstance(source, Gdk.Texture):
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
                    self._toast('\n'.join(errors))
                return False
            GLib.idle_add(deliver)
        self.services.control.submit(read)

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

    def add_images(self, images):
        """Attach more images, keeping those attached and those still being read."""
        self.restored_images.extend(images)
        self.update_image_preview()

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
