from typing import Any, List, Dict, Optional, Union, Callable
from gi.repository import Adw, Gtk, Gio, GLib, GObject
from .storage import ChatStorage
from . import ollama
import threading

@Gtk.Template(resource_path='/io/github/jackrabbithanna/Gnollama/model_details_view.ui')
class ModelDetailsView(Adw.Window):
    """Window for displaying detailed information about a model."""
    __gtype_name__ = 'ModelDetailsView'

    main_box: Gtk.Box = Gtk.Template.Child()

    def __init__(self, transient_for: Gtk.Window, model_name: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
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

    def __init__(self, storage: ChatStorage, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.storage: ChatStorage = storage
        self.model_rows: List[Adw.ActionRow] = []
        self.host_list: List[Dict[str, Any]] = []
        
        self.refresh_button.connect("clicked", self.on_refresh_clicked)
        self.pull_button.connect("clicked", self.on_pull_clicked)
        self.host_dropdown.connect("notify::selected-item", self.on_host_changed)
        
        self.update_hosts()

    def update_hosts(self) -> None:
        """Reloads the host list from storage."""
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
        self.host_dropdown.set_selected(target_idx)
        self.fetch_models_for_selected_host()

    def get_selected_host(self) -> Optional[Dict[str, Any]]:
        """Returns the currently selected host dictionary."""
        idx = self.host_dropdown.get_selected()
        if idx != Gtk.INVALID_LIST_POSITION and idx < len(self.host_list):
            return self.host_list[idx]
        return None

    def on_host_changed(self, dropdown: Gtk.DropDown, pspec: Any) -> None:
        """Callback for host selection changes."""
        self.fetch_models_for_selected_host()

    def on_refresh_clicked(self, btn: Gtk.Button) -> None:
        """Callback for the 'Refresh' button."""
        self.fetch_models_for_selected_host()

    def on_pull_clicked(self, btn: Gtk.Button) -> None:
        """Callback for the 'Pull' button."""
        host = self.get_selected_host()
        if not host:
            return
        dialog = PullModelDialog(self, host['hostname'])
        dialog.present()

    def fetch_models_for_selected_host(self) -> None:
        """Fetches models from the currently selected host and updates the list."""
        host = self.get_selected_host()
        if not host:
            return
            
        def thread_func() -> None:
            models = ollama.fetch_model_details(host['hostname'])
            GLib.idle_add(self.update_models_list, models)
            
        thread = threading.Thread(target=thread_func, daemon=True)
        thread.start()

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
        
        size_gb = model['size'] / (1024 * 1024 * 1024)
        row.set_subtitle(f"{model['details']['parameter_size']} | {size_gb:.2f} GB | {model['details']['format']}")
        
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
        row.add_suffix(del_btn)
        
        self.models_group.add(row)
        self.model_rows.append(row)

    def on_model_info_clicked(self, btn: Gtk.Button, model: Dict[str, Any]) -> None:
        """Handles info button click to show model details."""
        host = self.get_selected_host()
        if not host:
            return
            
        dialog = Adw.MessageDialog(
            transient_for=self,
            heading=_("Fetching details...")
        )
        dialog.present()
        
        def thread_func() -> None:
            data, error = ollama.show_model(host['hostname'], model['name'])
            GLib.idle_add(dialog.close)
            if data:
                GLib.idle_add(self.show_model_details, model['name'], data)
            else:
                GLib.idle_add(self.show_error, _("Failed to fetch details"), error)
                
        thread = threading.Thread(target=thread_func, daemon=True)
        thread.start()

    def show_error(self, title: str, msg: str) -> None:
        """Displays an error message dialog."""
        dialog = Adw.MessageDialog(
            transient_for=self,
            heading=title,
            body=msg
        )
        dialog.add_response("close", _("Close"))
        dialog.present()

    def on_model_delete_clicked(self, btn: Gtk.Button, model: Dict[str, Any]) -> None:
        """Handles delete button click to remove a model."""
        host = self.get_selected_host()
        if not host:
            return
            
        dialog = Adw.MessageDialog(
            transient_for=self,
            heading=_("Delete Model?"),
            body=_("Are you sure you want to delete {0}?").format(model['name'])
        )
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("delete", _("Delete"))
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        
        def on_response(d: Adw.MessageDialog, response: str) -> None:
            if response == "delete":
                def thread_func() -> None:
                    success, error = ollama.delete_model(host['hostname'], model['name'])
                    if success:
                        GLib.idle_add(self.fetch_models_for_selected_host)
                    else:
                        GLib.idle_add(self.show_error, _("Delete Failed"), error)
                threading.Thread(target=thread_func, daemon=True).start()
            d.close()
            
        dialog.connect("response", on_response)
        dialog.present()

    def show_model_details(self, model_name: str, data: Dict[str, Any]) -> None:
        """Displays a window with formatted model details."""
        view = ModelDetailsView(self, model_name)
        
        def add_section(title: str, content: Optional[str]) -> None:
            if not content:
                return
            section_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            
            label = Gtk.Label(label=title)
            label.set_halign(Gtk.Align.START)
            label.add_css_class("heading")
            section_box.append(label)
            
            details_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            details_box.add_css_class("card")
            
            content_label = Gtk.Label(label=content)
            content_label.set_halign(Gtk.Align.START)
            content_label.set_xalign(0)
            content_label.set_wrap(True)
            content_label.set_selectable(True)
            content_label.set_margin_start(12)
            content_label.set_margin_end(12)
            content_label.set_margin_top(12)
            content_label.set_margin_bottom(12)
            
            details_box.append(content_label)
            section_box.append(details_box)
            view.main_box.append(section_box)

        details = data.get("details", {})
        details_str = "\n".join([f"{k}: {v}" for k, v in details.items()])
        add_section(_("Details"), details_str)
        
        add_section(_("Modelfile"), data.get("modelfile"))
        add_section(_("Parameters"), data.get("parameters"))
        add_section(_("Template"), data.get("template"))
        add_section(_("License"), data.get("license"))
        
        view.present()

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

    def __init__(self, transient_for: Gtk.Window, hostname: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.set_transient_for(transient_for)
        self.hostname: str = hostname
        self.pulling: bool = False
        self.pull_thread: Optional[threading.Thread] = None

        self.cancel_btn.connect("clicked", self.on_cancel_clicked)
        self.pull_btn.connect("clicked", self.on_pull_clicked)

    def on_cancel_clicked(self, btn: Gtk.Button) -> None:
        """Handles cancel action, stops pulling if active."""
        if self.pulling:
            self.pulling = False
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
        
        buffer = self.status_textview.get_buffer()
        buffer.set_text("")
        self.status_label.set_text(_("Starting pull..."))
        
        self.pull_thread = threading.Thread(
            target=self.pull_task, 
            args=(model_name, self.insecure_check.get_active()),
            daemon=True
        )
        self.pull_thread.start()

    def pull_task(self, model_name: str, insecure: bool) -> None:
        """Thread worker to stream pull status."""
        try:
            for response in ollama.pull(self.hostname, model_name, insecure):
                if not self.pulling:
                    break
                GLib.idle_add(self.update_status, response)
            GLib.idle_add(self.pull_finished)
        except Exception as e:
            GLib.idle_add(self.update_status, {"error": str(e)})
            GLib.idle_add(self.pull_finished)

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

    def pull_finished(self) -> None:
        """Cleans up after the pull process ends."""
        self.pulling = False
        self.cancel_btn.set_label(_("Dismiss"))
        self.cancel_btn.add_css_class("suggested-action")
        self.pull_btn.set_visible(False)
        parent = self.get_transient_for()
        if parent and hasattr(parent, 'fetch_models_for_selected_host'):
            parent.fetch_models_for_selected_host()
