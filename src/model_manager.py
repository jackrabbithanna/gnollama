from gi.repository import Adw, Gtk, Gio, GLib
from .storage import ChatStorage
from . import ollama
import threading

@Gtk.Template(resource_path='/io/github/jackrabbithanna/Gnollama/model_manager.ui')
class ModelManagerDialog(Adw.Window):
    __gtype_name__ = 'ModelManagerDialog'

    host_dropdown = Gtk.Template.Child()
    models_group = Gtk.Template.Child()

    def __init__(self, storage, **kwargs):
        super().__init__(**kwargs)
        self.storage = storage
        self.hosts = []
        self.model_rows = []
        
        self.load_hosts()
        
        # Connect to dropdown changes
        self.host_dropdown.connect("notify::selected", self.on_host_selected)

    def load_hosts(self):
        self.hosts = self.storage.get_all_hosts()
        
        if not self.hosts:
            return
            
        model = Gtk.StringList()
        default_index = 0
        for i, host in enumerate(self.hosts):
            display_name = f"{host['name']} ({host['hostname']})"
            if host.get('default', False):
                display_name += " - Default"
                default_index = i
            model.append(display_name)
            
        self.host_dropdown.set_model(model)
        self.host_dropdown.set_selected(default_index)
        
        self.fetch_models_for_selected_host()

    def on_host_selected(self, dropdown, pspec):
        self.fetch_models_for_selected_host()
        
    def fetch_models_for_selected_host(self):
        selected_index = self.host_dropdown.get_selected()
        if selected_index == Gtk.INVALID_LIST_POSITION or not self.hosts:
            return
            
        host = self.hosts[selected_index]
        hostname = host['hostname']
        
        # Clear existing
        for row in self.model_rows:
            self.models_group.remove(row)
        self.model_rows.clear()
        
        # Show loading indicator (could add a spinner, for now just a row)
        loading_row = Adw.ActionRow(title=_("Loading models..."))
        self.models_group.add(loading_row)
        self.model_rows.append(loading_row)

        def fetch_thread():
            models = ollama.fetch_model_details(hostname)
            GLib.idle_add(self.on_models_fetched, models)
            
        thread = threading.Thread(target=fetch_thread)
        thread.daemon = True
        thread.start()

    def on_models_fetched(self, models):
        # Clear loading
        for row in self.model_rows:
            self.models_group.remove(row)
        self.model_rows.clear()
        
        if not models:
            empty_row = Adw.ActionRow(title=_("No models found."))
            self.models_group.add(empty_row)
            self.model_rows.append(empty_row)
            return
            
        for model in models:
            self.add_model_row(model)

    def add_model_row(self, model):
        row = Adw.ActionRow()
        row.set_title(model.get('name', 'Unknown'))
        
        size = model.get('size', 0)
        size_gb = size / (1024 ** 3)
        row.set_subtitle(f"{size_gb:.2f} GB")
        
        # Info button
        info_btn = Gtk.Button.new_from_icon_name("dialog-information-symbolic")
        info_btn.set_valign(Gtk.Align.CENTER)
        info_btn.add_css_class("flat")
        info_btn.connect("clicked", self.on_info_clicked, model)
        info_btn.set_tooltip_text(_("Model Details"))
        row.add_suffix(info_btn)
        
        self.models_group.add(row)
        self.model_rows.append(row)

    def on_info_clicked(self, btn, model):
        dialog = Adw.MessageDialog(
            transient_for=self,
            heading=model.get('name', 'Model Details'),
        )
        dialog.add_response("close", _("Close"))
        dialog.set_default_response("close")
        dialog.set_close_response("close")
        
        # Create details text
        details = model.get('details', {})
        
        info_text = f"<b>Name:</b> {model.get('name', 'N/A')}\n"
        info_text += f"<b>Model:</b> {model.get('model', 'N/A')}\n"
        info_text += f"<b>Modified At:</b> {model.get('modified_at', 'N/A')}\n"
        info_text += f"<b>Size:</b> {model.get('size', 'N/A')} bytes\n"
        info_text += f"<b>Digest:</b> {model.get('digest', 'N/A')}\n\n"
        
        info_text += f"<b>Format:</b> {details.get('format', 'N/A')}\n"
        info_text += f"<b>Family:</b> {details.get('family', 'N/A')}\n"
        
        families = details.get('families', [])
        if families:
            info_text += f"<b>Families:</b> {', '.join(families)}\n"
        else:
            info_text += f"<b>Families:</b> N/A\n"
            
        info_text += f"<b>Parameter Size:</b> {details.get('parameter_size', 'N/A')}\n"
        info_text += f"<b>Quantization Level:</b> {details.get('quantization_level', 'N/A')}"
        
        label = Gtk.Label(label=info_text)
        label.set_use_markup(True)
        label.set_wrap(True)
        label.set_halign(Gtk.Align.START)
        label.set_margin_start(12)
        label.set_margin_end(12)
        label.set_margin_top(12)
        label.set_margin_bottom(12)
        
        dialog.set_extra_child(label)
        dialog.present()
