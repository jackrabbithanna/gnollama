from gi.repository import Adw, Gtk, Gio, GLib, Gdk
from .storage import ChatStorage
from . import ollama
import threading
import html

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
        
        self.load_css()

    def load_css(self):
        css_provider = Gtk.CssProvider()
        css = """
        .template-bg {
            background-color: alpha(@window_fg_color, 0.05);
            border-radius: 6px;
            padding: 10px;
            font-family: monospace;
        }
        """
        css_provider.load_from_data(css.encode('utf-8'))
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            css_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

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
        selected_index = self.host_dropdown.get_selected()
        if selected_index == Gtk.INVALID_LIST_POSITION:
            return
        host = self.hosts[selected_index]
        hostname = host['hostname']

        dialog = Adw.MessageDialog(
            transient_for=self,
            heading=model.get('name', 'Model Details'),
        )
        dialog.add_response("close", _("Close"))
        dialog.set_default_response("close")
        dialog.set_close_response("close")
        
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        main_box.set_margin_start(12)
        main_box.set_margin_end(12)
        main_box.set_margin_top(12)
        main_box.set_margin_bottom(12)
        
        # Create basic details
        details = model.get('details', {})
        
        def add_basic_section(title, content):
            section_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            title_label = Gtk.Label()
            title_label.set_markup(f"<b>{title}:</b>")
            title_label.set_halign(Gtk.Align.START)
            section_box.append(title_label)
            
            content_label = Gtk.Label(label=str(content) if content else "N/A")
            content_label.set_halign(Gtk.Align.START)
            content_label.set_wrap(True)
            content_label.set_selectable(True)
            content_label.set_xalign(0)
            section_box.append(content_label)
            main_box.append(section_box)

        add_basic_section(_("Name"), model.get('name'))
        add_basic_section(_("Model"), model.get('model'))
        add_basic_section(_("Modified At"), model.get('modified_at'))
        add_basic_section(_("Size"), f"{model.get('size', 'N/A')} bytes")
        add_basic_section(_("Digest"), model.get('digest'))
        
        add_basic_section(_("Format"), details.get('format'))
        add_basic_section(_("Family"), details.get('family'))
        
        families = details.get('families', [])
        add_basic_section(_("Families"), ", ".join(families) if families else None)
            
        add_basic_section(_("Parameter Size"), details.get('parameter_size'))
        add_basic_section(_("Quantization Level"), details.get('quantization_level'))
        
        main_box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        
        # Expander for "Show Details"
        expander = Gtk.Expander(label=_("Show Details"))
        details_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        details_box.set_margin_start(6)
        details_box.set_margin_end(6)
        details_box.set_margin_top(6)
        details_box.set_margin_bottom(6)
        
        loading_label = Gtk.Label(label=_("Loading details..."))
        details_box.append(loading_label)
        
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_min_content_height(300)
        scrolled.set_min_content_width(500)
        scrolled.set_child(details_box)
        expander.set_child(scrolled)
        main_box.append(expander)
        
        dialog.set_extra_child(main_box)
        dialog.present()
        
        def fetch_details_thread():
            show_data = ollama.show_model(hostname, model['name'])
            GLib.idle_add(self.update_details_label, details_box, show_data)
            
        thread = threading.Thread(target=fetch_details_thread)
        thread.daemon = True
        thread.start()

    def update_details_label(self, box, data):
        # Clear loading label
        while True:
            child = box.get_first_child()
            if not child:
                break
            box.remove(child)
            
        if not data:
            box.append(Gtk.Label(label=_("Failed to load details.")))
            return
            
        def add_section(title, content, use_bg=False):
            section_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            title_label = Gtk.Label()
            title_label.set_markup(f"<b>{title}:</b>")
            title_label.set_halign(Gtk.Align.START)
            section_box.append(title_label)
            
            if not content:
                content = "N/A"
            
            if isinstance(content, dict):
                content_text = "\n".join([f"  {k}: {v}" for k, v in content.items()])
            else:
                content_text = str(content)
                
            content_label = Gtk.Label(label=content_text)
            content_label.set_halign(Gtk.Align.START)
            content_label.set_wrap(True)
            content_label.set_selectable(True)
            content_label.set_xalign(0)
            
            if use_bg:
                content_label.add_css_class("template-bg")
            
            section_box.append(content_label)
            box.append(section_box)

        add_section(_("Parameters"), data.get("parameters"))
        add_section(_("Modified At"), data.get("modified_at"))
        add_section(_("Details"), data.get("details"))
        add_section(_("Template"), data.get("template"), use_bg=True)
        add_section(_("Capabilities"), data.get("capabilities"))
        add_section(_("Model Info"), data.get("model_info"))
        add_section(_("License"), data.get("license"))

