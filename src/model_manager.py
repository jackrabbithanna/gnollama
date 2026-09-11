from typing import Any, List, Dict, Optional, Union, Callable
from gi.repository import Adw, Gtk, Gio, GLib, GObject
from .storage import ChatStorage
from . import ollama
from .session import ViewRequests
import threading
import json
from datetime import datetime


from .services import model_key


def running_model_subtitle(model):
    def size(key):
        value = model.get(key)
        return GLib.format_size(value) if isinstance(value, int) and value >= 0 else _('Unavailable')
    context = model.get('context_length')
    context = str(context) if isinstance(context, int) and context >= 0 else _('Unavailable')
    try:
        expiry = datetime.fromisoformat(model['expires_at'].replace('Z', '+00:00')).astimezone().strftime('%x %X')
    except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
        expiry = _('Unavailable')
    return _('Memory: {0} · VRAM: {1}\nContext: {2} · Expires: {3}').format(size('size'), size('size_vram'), context, expiry)

@Gtk.Template(resource_path='/io/github/jackrabbithanna/Gnollama/model_details_view.ui')
class ModelDetailsView(Adw.Window):
    """Window for displaying detailed information about a model."""
    __gtype_name__ = 'ModelDetailsView'

    main_box: Gtk.Box = Gtk.Template.Child()

    def __init__(self, transient_for: Gtk.Window, model_name: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.requests = ViewRequests(self)
        self.set_transient_for(transient_for)
        self.set_title(f"{_('Model Details')}: {model_name}")

@Gtk.Template(resource_path='/io/github/jackrabbithanna/Gnollama/model_manager.ui')
class ModelManagerDialog(Adw.Window):
    """Dialog for managing Ollama models (listing, info, deletion, pulling)."""
    __gtype_name__ = 'ModelManagerDialog'

    host_dropdown: Gtk.DropDown = Gtk.Template.Child()
    models_group: Adw.PreferencesGroup = Gtk.Template.Child()
    refresh_button: Gtk.Button = Gtk.Template.Child()
    pull_button: Gtk.Button = Gtk.Template.Child()
    model_stack = Gtk.Template.Child()
    running_group = Gtk.Template.Child()
    running_status = Gtk.Template.Child()

    def __init__(self, storage: ChatStorage, is_model_busy=None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.requests = ViewRequests(self)
        self.storage: ChatStorage = storage
        self.model_rows: List[Adw.ActionRow] = []
        self.host_list: List[Dict[str, Any]] = []
        self._fetch_cancel = None
        self._running_cancel = None
        self._running_pending = False
        self._poll_id = None
        self._running_rows = []
        self.is_model_busy = is_model_busy or (lambda host, model: False)
        self.model_stack.connect('notify::visible-child-name', self._view_changed)
        self.connect('map', self._mapped)
        self.connect('unmap', self._unmapped)
        
        self.refresh_button.connect("clicked", self.on_refresh_clicked)
        self.pull_button.connect("clicked", self.on_pull_clicked)
        self.host_dropdown.connect("notify::selected-item", self.on_host_changed)
        
        self.update_hosts()

    def update_hosts(self) -> None:
        """Reloads the host list from storage."""
        selected = self.get_selected_host()
        hosts = self.storage.get_all_hosts()
        self.host_list = hosts
        
        host_names = [h['name'] for h in hosts]
        string_list = Gtk.StringList.new(host_names)
        self.host_dropdown.set_model(string_list)
        
        target_idx = 0
        for i, h in enumerate(hosts):
            if h.get('default', False):
                target_idx = i
                break
        if selected:
            for i, h in enumerate(hosts):
                if h['id'] == selected['id']:
                    target_idx = i
                    break
        self.host_dropdown.set_selected(target_idx)
        self.fetch_models_for_selected_host()
        self._reset_running()
        self.refresh_running()

    def get_selected_host(self) -> Optional[Dict[str, Any]]:
        """Returns the currently selected host dictionary."""
        idx = self.host_dropdown.get_selected()
        if idx != Gtk.INVALID_LIST_POSITION and idx < len(self.host_list):
            return self.host_list[idx]
        return None

    def on_host_changed(self, dropdown: Gtk.DropDown, pspec: Any) -> None:
        """Callback for host selection changes."""
        self.fetch_models_for_selected_host()
        self._reset_running()
        self.refresh_running()

    def on_refresh_clicked(self, btn: Gtk.Button) -> None:
        self.storage.services.catalog.invalidate()
        """Callback for the 'Refresh' button."""
        if self.model_stack.get_visible_child_name() == 'running':
            self.refresh_running()
        else:
            self.fetch_models_for_selected_host()

    def _mapped(self, *args):
        if self._poll_id is None and not ollama.is_cloud(self.get_selected_host()):
            self._poll_id = GLib.timeout_add_seconds(5, self._poll_running)
        self.refresh_running()

    def _unmapped(self, *args):
        if self._poll_id is not None:
            GLib.source_remove(self._poll_id)
            self._poll_id = None
        self._reset_running()

    def _view_changed(self, *args):
        running = self.model_stack.get_visible_child_name() == 'running'
        self.pull_button.set_visible(not running and not ollama.is_cloud(self.get_selected_host()))
        if running:
            self.refresh_running()
        else:
            self._reset_running()

    def _reset_running(self):
        if self._running_cancel:
            self._running_cancel.cancel()
        self._running_pending = False
        for row, _host, _model, _button in self._running_rows:
            self.running_group.remove(row)
        self._running_rows.clear()
        self.running_status.set_text('')

    def _poll_running(self):
        if ollama.is_cloud(self.get_selected_host()):
            self._poll_id = None
            return False
        self.update_unload_buttons()
        self.refresh_running()
        return True

    def refresh_running(self):
        if (self.requests.closed or not self.get_mapped() or self.model_stack.get_visible_child_name() != 'running'
                or self._running_pending):
            return
        host = self.get_selected_host()
        if ollama.is_cloud(host):
            return
        if not host:
            self.running_status.set_text(_('No host configured.'))
            return
        self._running_pending = True
        cancel = self._running_cancel = self.requests.new_cancel()
        if not self._running_rows:
            self.running_status.set_text(_('Loading running models…'))
        def completed(models, error):
            self._running_pending = False
            if error:
                self.running_status.set_text(_('Could not refresh running models: {0}').format(error))
                return
            self.update_running_models(host['hostname'], models)
        def fetch():
            try:
                models = ollama.fetch_running_models(host['hostname'], cancellable=cancel)
                self.requests.deliver(completed, models, None, cancellable=cancel)
            except ollama.OllamaError as exc:
                self.requests.deliver(completed, [], str(exc), cancellable=cancel)
        from .session import worker
        self.storage.services.control.submit(fetch)

    def update_running_models(self, hostname, models):
        for row, _host, _model, _button in self._running_rows:
            self.running_group.remove(row)
        self._running_rows.clear()
        self.running_status.set_text('' if models else _('No models are loaded in memory.'))
        for model in models:
            name = model.get('name') or model.get('model')
            if not name:
                continue
            row = Adw.ActionRow(title=name, subtitle=running_model_subtitle(model), use_markup=False)
            button = Gtk.Button(label=_('Unload'), valign=Gtk.Align.CENTER,
                                tooltip_text=_('Release this model from memory; keep its downloaded files.'))
            button.connect('clicked', self.on_unload_clicked, hostname, name)
            row.add_suffix(button)
            self.running_group.add(row)
            self._running_rows.append((row, hostname, name, button))
        self.update_unload_buttons()

    def update_unload_buttons(self):
        for _row, host, model, button in self._running_rows:
            busy = self.is_model_busy(host, model)
            unloading = model_key(host, model) in self.storage.services.models.reserved
            button.set_sensitive(not busy and not unloading)
            button.set_label(_('Unloading…') if unloading else _('Unload'))
            button.set_tooltip_text(_('A chat or embedding job is using this model in Gnollama.') if busy else
                                    _('Release this model from memory; keep its downloaded files.'))

    def on_unload_clicked(self, button, host, model):
        if ollama.is_cloud(self.get_selected_host()):
            return
        key = model_key(host, model)
        if self.requests.closed or not self.storage.knowledge.reserve_model(host, model, self.is_model_busy):
            return
        self.update_unload_buttons()
        cancel = self.requests.new_cancel()
        def completed(error):
            self.storage.services.models.reserved.discard(key)
            if self.requests.closed:
                return False
            self.update_unload_buttons()
            selected = self.get_selected_host()
            if selected and ollama.validate_host(selected['hostname']) == key[0]:
                if error and not cancel.is_cancelled():
                    self.show_error(_('Could not unload model'), error)
                if self._running_cancel:
                    self._running_cancel.cancel()
                self._running_pending = False
                self.refresh_running()
            return False
        def unload():
            error = None
            try:
                ollama.unload_model(host, model, cancellable=cancel)
            except ollama.OllamaError as exc:
                error = str(exc)
            finally:
                GLib.idle_add(completed, error)
        from .session import worker
        self.storage.services.control.submit(unload)

    def on_pull_clicked(self, btn: Gtk.Button) -> None:
        """Callback for the 'Pull' button."""
        host = self.get_selected_host()
        if not host or ollama.is_cloud(host):
            return
        dialog = PullModelDialog(self, host['hostname'])
        dialog.present()

    def fetch_models_for_selected_host(self) -> None:
        """Fetches models from the currently selected host and updates the list."""
        if self._fetch_cancel is not None:
            self._fetch_cancel.cancel()
        self.update_models_list([])
        host = self.get_selected_host()
        cloud = ollama.is_cloud(host)
        running_page = self.model_stack.get_page(self.model_stack.get_child_by_name('running'))
        if cloud:
            self.model_stack.set_visible_child_name('installed')
            self._reset_running()
            if self._poll_id is not None:
                GLib.source_remove(self._poll_id)
                self._poll_id = None
        running_page.set_visible(not cloud)
        self.model_stack.get_page(self.model_stack.get_child_by_name('installed')).set_title(
            _('Available Models') if cloud else _('Installed'))
        self._view_changed()
        if self.get_mapped() and not cloud:
            self._mapped()
        if not host:
            return
        cancel = self._fetch_cancel = self.requests.new_cancel()

        def thread_func() -> None:
            try:
                models = ollama.fetch_model_details(self.storage.connection(host), cancellable=cancel)
                self.requests.deliver(self.update_models_list, models, cancellable=cancel)
            except ollama.OllamaError as e:
                self.requests.deliver(self.show_error, _("Connection Error"), str(e), cancellable=cancel)
            
        from .session import worker
        self.storage.services.control.submit(thread_func)

    def update_models_list(self, models: List[Dict[str, Any]]) -> None:
        """Updates the UI with a new list of models."""
        for row in self.model_rows:
            self.models_group.remove(row)
        self.model_rows.clear()
        
        for model in models:
            self.add_model_row(model)

    def add_model_row(self, model: Dict[str, Any]) -> None:
        """Adds a single model row to the preferences group."""
        row = Adw.ActionRow()
        row.set_title(model['name'])
        
        cloud = ollama.is_cloud(self.get_selected_host())
        details = model.get('details') or {}
        parts = [details.get('parameter_size'), details.get('format')]
        size = model.get('size')
        if not cloud and isinstance(size, (int, float)) and size >= 0:
            parts.insert(1, GLib.format_size(int(size)))
        row.set_subtitle(' | '.join(str(part) for part in parts if part) or
                         (_('Ollama Cloud') if cloud else _('Unavailable')))
        
        info_btn = Gtk.Button.new_from_icon_name("dialog-information-symbolic")
        info_btn.set_valign(Gtk.Align.CENTER)
        info_btn.add_css_class("flat")
        info_btn.connect("clicked", self.on_model_info_clicked, model)
        info_btn.set_tooltip_text(_("Model Details"))
        row.add_suffix(info_btn)
        
        del_btn = Gtk.Button.new_from_icon_name("user-trash-symbolic")
        del_btn.set_valign(Gtk.Align.CENTER)
        del_btn.add_css_class("flat")
        del_btn.connect("clicked", self.on_model_delete_clicked, model)
        del_btn.set_tooltip_text(_("Delete Model"))
        if not cloud:
            row.add_suffix(del_btn)
        
        self.models_group.add(row)
        self.model_rows.append(row)

    def on_model_info_clicked(self, btn: Gtk.Button, model: Dict[str, Any]) -> None:
        """Handles info button click to show model details."""
        host = self.get_selected_host()
        if not host:
            return
            
        view = ModelDetailsView(self, model['name'])
        spinner = Gtk.Spinner()
        spinner.start()
        spinner.set_halign(Gtk.Align.CENTER)
        spinner.set_margin_top(24)
        spinner.set_margin_bottom(24)
        spinner.set_size_request(32, 32)
        view.main_box.append(spinner)
        view.present()
        cancel = view.requests.new_cancel()
        self.requests._cancellables.add(cancel)

        def thread_func() -> None:
            try:
                data = ollama.show_model(self.storage.connection(host), model['name'], cancellable=cancel)
                view.requests.deliver(view.main_box.remove, spinner, cancellable=cancel)
                view.requests.deliver(self.populate_model_details, view, data, model, cancellable=cancel)
            except ollama.OllamaError as e:
                view.requests.deliver(view.close, cancellable=cancel)
                self.requests.deliver(self.show_error, _("Failed to fetch details"), str(e), cancellable=cancel)
                
        from .session import worker
        self.storage.services.control.submit(thread_func)

    def show_error(self, title: str, msg: str) -> None:
        """Displays an error message dialog."""
        dialog = Adw.AlertDialog(
            heading=title,
            body=msg
        )
        dialog.add_response("close", _("Close"))
        dialog.present(self)

    def on_model_delete_clicked(self, btn: Gtk.Button, model: Dict[str, Any]) -> None:
        """Handles delete button click to remove a model."""
        host = self.get_selected_host()
        if not host or ollama.is_cloud(host):
            return
        if self.is_model_busy(host['hostname'], model['name']) or model_key(host['hostname'], model['name']) in self.storage.services.models.reserved:
            self.show_error(_('Model Is Busy'), _('Wait for active chats and embedding jobs before deleting this model.'))
            return
        usage = self.storage.db.model_embedding_usage(model.get('digest', ''))
        body = _('Are you sure you want to delete {0}?').format(model['name'])
        cleanup = Gtk.CheckButton(label=_('Also delete matching vectors; keep source text'))
        if usage['indexes']:
            body += '\n\n' + _('Stored data using this model: {0} documents, {1} indexes, {2} vectors. Kept vectors can be used again with the same model digest on a compatible host.').format(usage['documents'], usage['indexes'], usage['vectors'])
        if usage['collections']:
            body += '\n\n' + _('Collections using this configuration: {0}. Deleting vectors keeps their documents and membership, but they will need embeddings rebuilt before searching.').format(usage['collections'])
            
        dialog = Adw.AlertDialog(
            heading=_("Delete Model?"),
            body=body
        )
        if usage['indexes']:
            dialog.set_extra_child(cleanup)
        dialog.set_default_response('cancel')
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("delete", _("Delete"))
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        
        def on_response(d: Adw.AlertDialog, response: str) -> None:
            if response == "delete":
                delete_vectors = cleanup.get_active()
                if delete_vectors and self.storage.knowledge.indexing_digest(model['digest']):
                    self.show_error(_('Model Is Busy'), _('Wait for all indexing jobs using this model before deleting its vectors.'))
                    return
                if not self.storage.knowledge.reserve_model(host['hostname'], model['name'], self.is_model_busy):
                    self.show_error(_('Model Is Busy'), _('Wait for active chats and embedding jobs before deleting this model.'))
                    return
                key = model_key(host['hostname'], model['name'])
                cancel = self.requests.new_cancel()
                def thread_func() -> None:
                    try:
                        self.storage.services.catalog.invalidate()
                        ollama.delete_model(host['hostname'], model['name'], cancellable=cancel)
                        if delete_vectors:
                            self.storage._submit(self.storage.db.delete_model_embeddings, model['digest'],
                                                 on_done=self.storage.knowledge.changed)
                        self.storage.knowledge.changed()
                        self.requests.deliver(self.fetch_models_for_selected_host)
                    except ollama.OllamaError as e:
                        self.requests.deliver(self.show_error, _("Delete Failed"), str(e), cancellable=cancel)
                    finally:
                        self.storage.services.models.reserved.discard(key)
                from .session import worker
                self.storage.services.control.submit(thread_func)
            d.close()
            
        dialog.connect("response", on_response)
        dialog.present(self)

    def populate_model_details(self, view: Any, show_data: Dict[str, Any], tag_data: Dict[str, Any]) -> None:
        """Populates the model details window."""
        
        def add_field(label: str, value: Any, collapsible: bool = False) -> None:
            if value is None or value == "":
                return
            
            if isinstance(value, (dict, list)):
                value_str = json.dumps(value, indent=2)
            else:
                value_str = str(value)

            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            
            if collapsible:
                expander = Gtk.Expander(label=label)
                expander.set_expanded(False)
                
                content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
                content_box.add_css_class("card")
                content_box.add_css_class("margin-top-6")
                
                content_label = Gtk.Label(label=value_str)
                content_label.set_halign(Gtk.Align.START)
                content_label.set_xalign(0)
                content_label.set_wrap(True)
                content_label.set_selectable(True)
                content_label.add_css_class("margin-12")
                
                content_box.append(content_label)
                expander.set_child(content_box)
                box.append(expander)
            else:
                title_label = Gtk.Label(label=label)
                title_label.set_halign(Gtk.Align.START)
                title_label.add_css_class("heading")
                box.append(title_label)
                
                content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
                content_box.add_css_class("card")
                
                content_label = Gtk.Label(label=value_str)
                content_label.set_halign(Gtk.Align.START)
                content_label.set_xalign(0)
                content_label.set_wrap(True)
                content_label.set_selectable(True)
                content_label.add_css_class("margin-12")
                
                content_box.append(content_label)
                box.append(content_box)
            
            view.main_box.append(box)

        # Priority properties from tags
        tag_keys = ["name", "model", "remote_model", "remote_host", "modified_at", "size", "digest"]
        for k in tag_keys:
            if k in tag_data:
                label = k.replace("_", " ").title()
                val = tag_data[k]
                if k == "size" and isinstance(val, (int, float)):
                    val = f"{val / (1024*1024*1024):.2f} GB ({val} bytes)"
                add_field(_(label), val)
        
        # Details from tags
        tag_details = tag_data.get("details") or {}
        detail_keys = ["format", "family", "families", "parameter_size", "quantization_level"]
        for k in detail_keys:
            if k in tag_details:
                label = f"Detail: {k.replace('_', ' ').title()}"
                add_field(_(label), tag_details[k])

        # Show properties (non-collapsible first)
        show_keys = ["parameters", "capabilities"]
        for k in show_keys:
            if k in show_data:
                label = k.replace("_", " ").title()
                add_field(_(label), show_data[k])

        # Add everything else from both sources that hasn't been added yet
        merged = tag_data.copy()
        merged.update(show_data)
        
        # We'll also handle the collapsible ones later
        collapsible_keys = ["template", "license", "modelfile", "model_info"]
        already_added = set(tag_keys) | set(show_keys) | set(collapsible_keys) | {"details", "tensors"}
        
        for k, v in merged.items():
            if k not in already_added:
                label = k.replace("_", " ").title()
                add_field(_(label), v)

        # Collapsible properties at the bottom
        for k in collapsible_keys:
            if k in show_data:
                label = k.replace("_", " ").title()
                if k == "modelfile": label = "Modelfile"
                add_field(_(label), show_data[k], collapsible=True)

@Gtk.Template(resource_path='/io/github/jackrabbithanna/Gnollama/pull_model_dialog.ui')
class PullModelDialog(Adw.Window):
    """Dialog for pulling (downloading) a new model from a host."""
    __gtype_name__ = 'PullModelDialog'

    cancel_btn: Gtk.Button = Gtk.Template.Child()
    pull_btn: Gtk.Button = Gtk.Template.Child()
    model_name_entry: Gtk.Entry = Gtk.Template.Child()
    insecure_check: Gtk.CheckButton = Gtk.Template.Child()
    status_label: Gtk.Label = Gtk.Template.Child()
    status_textview: Gtk.TextView = Gtk.Template.Child()

    progress_bar = Gtk.Template.Child()

    def __init__(self, transient_for: Gtk.Window, hostname: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.requests = ViewRequests(self)
        self.set_transient_for(transient_for)
        from .services import Services
        self.services = transient_for.storage.services if hasattr(transient_for, 'storage') else Services()
        self.hostname: str = hostname
        self.pulling: bool = False
        self.pull_future: Optional[Any] = None

        self.cancel_btn.connect("clicked", self.on_cancel_clicked)
        self.pull_btn.connect("clicked", self.on_pull_clicked)

    def on_cancel_clicked(self, btn: Gtk.Button) -> None:
        """Handles cancel action, stops pulling if active."""
        if self.pulling:
            self.pulling = False
            self._pull_cancel.cancel()
        self.requests.close()
        self.close()

    def on_pull_clicked(self, btn: Gtk.Button) -> None:
        """Starts the model pull process."""
        model_name = self.model_name_entry.get_text().strip()
        if not model_name:
            return
            
        self.model_name_entry.set_sensitive(False)
        self.insecure_check.set_sensitive(False)
        self.pull_btn.set_sensitive(False)
        self.pulling = True
        self._pull_cancel = self.requests.new_cancel()
        
        buffer = self.status_textview.get_buffer()
        buffer.set_text("")
        self.status_label.set_text(_("Starting download…"))
        
        from .session import worker
        self.pull_future = self.services.transfer.submit(self.pull_task, model_name, self.insecure_check.get_active())

    def pull_task(self, model_name: str, insecure: bool) -> None:
        """Thread worker to stream pull status."""
        try:
            for response in ollama.pull(self.hostname, model_name, insecure, cancellable=self._pull_cancel):
                if not self.pulling:
                    break
                self.requests.deliver(self.update_status, response)
            self.requests.deliver(self.pull_finished)
        except ollama.RequestCancelled:
            pass
        except Exception as e:
            self.requests.deliver(self.update_status, {"error": str(e)})
            self.requests.deliver(self.pull_finished)

    def update_status(self, response: Dict[str, Any]) -> None:
        """Updates the status log in the UI."""
        buffer = self.status_textview.get_buffer()
        end_iter = buffer.get_end_iter()
        
        if "error" in response:
            buffer.insert(end_iter, f"{_('Error')}: {response['error']}\n")
            self.status_label.set_text(_("Error occurred."))
            return
            
        status = response.get("status", "")
        line = status
        if "total" in response and "completed" in response:
            total_mb = response["total"] / (1024 * 1024)
            completed_mb = response["completed"] / (1024 * 1024)
            line += f" ({completed_mb:.1f} MB / {total_mb:.1f} MB)"
            
        buffer.insert(end_iter, f"{line}\n")
        
        mark = buffer.create_mark(None, buffer.get_end_iter(), False)
        self.status_textview.scroll_to_mark(mark, 0.0, True, 0.0, 1.0)
        self.status_label.set_text(status)
        total = response.get('total', 0)
        completed = response.get('completed', 0)
        if total:
            self.progress_bar.set_fraction(min(1, max(0, completed / total)))
            self.progress_bar.set_text(_('{0:.0f}%').format(100 * completed / total))
        elif status == 'success':
            self.progress_bar.set_fraction(1)
            self.progress_bar.set_text(_('Download complete'))
        else:
            self.progress_bar.pulse()
            self.progress_bar.set_text(status)

    def pull_finished(self) -> None:
        self.services.catalog.invalidate()
        """Cleans up after the pull process ends."""
        self.pulling = False
        self.cancel_btn.set_label(_("Close"))
        self.cancel_btn.add_css_class("suggested-action")
        self.pull_btn.set_visible(False)
        parent = self.get_transient_for()
        if parent and hasattr(parent, 'fetch_models_for_selected_host'):
            parent.fetch_models_for_selected_host()

# Translators: Dummy definitions for dynamic key extraction by xgettext
def _dummy_extractions():
    _("Name")
    _("Model")
    _("Remote Model")
    _("Remote Host")
    _("Modified At")
    _("Size")
    _("Digest")
    _("Detail: Format")
    _("Detail: Family")
    _("Detail: Families")
    _("Detail: Parameter Size")
    _("Detail: Quantization Level")
    _("Parameters")
    _("Capabilities")
    _("Template")
    _("License")
    _("Modelfile")
    _("Model Info")
