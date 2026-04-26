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
    pull_model_btn = Gtk.Template.Child()

    def __init__(self, storage, **kwargs):
        super().__init__(**kwargs)
        self.storage = storage
        self.hosts = []
        self.model_rows = []
        
        self.load_hosts()
        
        # Connect to dropdown changes
        self.host_dropdown.connect("notify::selected", self.on_host_selected)
        self.pull_model_btn.connect("clicked", self.on_pull_model_clicked)
        
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
        
    def on_pull_model_clicked(self, btn):
        selected_index = self.host_dropdown.get_selected()
        if selected_index == Gtk.INVALID_LIST_POSITION or not self.hosts:
            return
        host = self.hosts[selected_index]
        hostname = host['hostname']
        
        dialog = PullModelDialog(transient_for=self, hostname=hostname)
        dialog.present()
        
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
        
        # Delete button
        delete_btn = Gtk.Button.new_from_icon_name("user-trash-symbolic")
        delete_btn.set_valign(Gtk.Align.CENTER)
        delete_btn.add_css_class("flat")
        delete_btn.connect("clicked", self.on_delete_clicked, model)
        delete_btn.set_tooltip_text(_("Delete model"))
        row.add_suffix(delete_btn)
        
        self.models_group.add(row)
        self.model_rows.append(row)

    def on_delete_clicked(self, btn, model):
        model_name = model.get('name')
        if not model_name:
            return
            
        dialog = Adw.MessageDialog(
            transient_for=self,
            heading=_("Delete Model"),
            body=_("Are you sure you want to delete the model '{0}'?").format(model_name)
        )
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("delete", _("Delete"))
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        
        dialog.connect("response", self.on_delete_confirm, model_name)
        dialog.present()

    def on_delete_confirm(self, dialog, response, model_name):
        if response != "delete":
            return
            
        selected_index = self.host_dropdown.get_selected()
        if selected_index == Gtk.INVALID_LIST_POSITION:
            return
        host = self.hosts[selected_index]
        hostname = host['hostname']
        
        def delete_thread():
            success, error = ollama.delete_model(hostname, model_name)
            GLib.idle_add(self.on_delete_finished, success, error)
            
        thread = threading.Thread(target=delete_thread)
        thread.daemon = True
        thread.start()

    def on_delete_finished(self, success, error):
        if not success:
            error_dialog = Adw.MessageDialog(
                transient_for=self,
                heading=_("Error"),
                body=_("Failed to delete model: {0}").format(error)
            )
            error_dialog.add_response("ok", _("Ok"))
            error_dialog.set_default_response("ok")
            error_dialog.set_close_response("ok")
            error_dialog.present()
        else:
            self.fetch_models_for_selected_host()

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


class PullModelDialog(Adw.Window):
    def __init__(self, transient_for, hostname, **kwargs):
        super().__init__(**kwargs)
        self.set_transient_for(transient_for)
        self.set_modal(True)
        self.set_title(_("Pull Model"))
        self.set_default_size(500, 400)
        self.hostname = hostname

        toolbar_view = Adw.ToolbarView()
        self.set_content(toolbar_view)

        header_bar = Adw.HeaderBar()
        header_bar.set_show_end_title_buttons(False)
        header_bar.set_show_start_title_buttons(False)
        toolbar_view.add_top_bar(header_bar)

        self.cancel_btn = Gtk.Button(label=_("Cancel"))
        self.cancel_btn.connect("clicked", self.on_cancel_clicked)
        header_bar.pack_start(self.cancel_btn)

        self.pull_btn = Gtk.Button(label=_("Pull model"))
        self.pull_btn.add_css_class("suggested-action")
        self.pull_btn.connect("clicked", self.on_pull_clicked)
        header_bar.pack_end(self.pull_btn)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_start(16)
        box.set_margin_end(16)
        box.set_margin_top(16)
        box.set_margin_bottom(16)

        name_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        name_box.append(Gtk.Label(label=_("Model name"), halign=Gtk.Align.START))
        self.model_name_entry = Gtk.Entry()
        self.model_name_entry.set_placeholder_text(_("Name of the model to download"))
        name_box.append(self.model_name_entry)
        box.append(name_box)

        insecure_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.insecure_check = Gtk.CheckButton(label=_("Insecure"))
        desc_label = Gtk.Label(label=_("Allow downloading over insecure connections"))
        desc_label.add_css_class("dim-label")
        desc_label.set_halign(Gtk.Align.START)
        desc_label.set_margin_start(28)
        insecure_box.append(self.insecure_check)
        insecure_box.append(desc_label)
        box.append(insecure_box)

        self.status_label = Gtk.Label(label="")
        self.status_label.set_halign(Gtk.Align.START)
        self.status_label.set_wrap(True)
        box.append(self.status_label)
        
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        self.status_textview = Gtk.TextView()
        self.status_textview.set_editable(False)
        self.status_textview.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.status_textview.set_cursor_visible(False)
        scrolled.set_child(self.status_textview)
        box.append(scrolled)

        toolbar_view.set_content(box)
        
        self.pulling = False
        self.pull_thread = None

    def on_cancel_clicked(self, btn):
        if self.pulling:
            self.pulling = False
        self.close()

    def on_pull_clicked(self, btn):
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
        
        self.pull_thread = threading.Thread(target=self.pull_task, args=(model_name, self.insecure_check.get_active()))
        self.pull_thread.daemon = True
        self.pull_thread.start()

    def pull_task(self, model_name, insecure):
        try:
            for response in ollama.pull(self.hostname, model_name, insecure):
                if not self.pulling:
                    break
                GLib.idle_add(self.update_status, response)
            GLib.idle_add(self.pull_finished)
        except Exception as e:
            GLib.idle_add(self.update_status, {"error": str(e)})
            GLib.idle_add(self.pull_finished)

    def update_status(self, response):
        buffer = self.status_textview.get_buffer()
        end_iter = buffer.get_end_iter()
        
        if "error" in response:
            buffer.insert(end_iter, f"Error: {response['error']}\n")
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

    def pull_finished(self):
        self.pulling = False
        self.cancel_btn.set_label(_("Dismiss"))
        self.cancel_btn.add_css_class("suggested-action")
        self.pull_btn.set_visible(False)
        parent = self.get_transient_for()
        if parent and hasattr(parent, 'fetch_models_for_selected_host'):
            parent.fetch_models_for_selected_host()

