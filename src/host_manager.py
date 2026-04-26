from gi.repository import Adw, Gtk, Gio, GLib
from .storage import ChatStorage

@Gtk.Template(resource_path='/io/github/jackrabbithanna/Gnollama/host_manager.ui')
class HostManagerDialog(Adw.Window):
    __gtype_name__ = 'HostManagerDialog'

    hosts_group = Gtk.Template.Child()
    add_button = Gtk.Template.Child()

    def __init__(self, storage, on_hosts_changed_cb=None, **kwargs):
        super().__init__(**kwargs)
        self.storage = storage
        self.on_hosts_changed_cb = on_hosts_changed_cb
        self.host_rows = []
        
        self.add_button.connect("clicked", self.on_add_clicked)
        
        self.load_hosts()

    def load_hosts(self):
        # Clear existing rows
        for row in self.host_rows:
            self.hosts_group.remove(row)
        self.host_rows.clear()
            
        hosts = self.storage.get_all_hosts()
        for host in hosts:
            self.add_host_row(host)

    def add_host_row(self, host):
        row = Adw.ActionRow()
        row.set_title(host['name'])
        if host.get('default', False):
            row.set_subtitle(host['hostname'] + " (Default)")
        else:
            row.set_subtitle(host['hostname'])
        
        # Edit button
        edit_btn = Gtk.Button.new_from_icon_name("document-open-symbolic")
        edit_btn.set_valign(Gtk.Align.CENTER)
        edit_btn.add_css_class("flat")
        edit_btn.connect("clicked", self.on_edit_clicked, host)
        row.add_suffix(edit_btn)
        
        # Delete button
        del_btn = Gtk.Button.new_from_icon_name("user-trash-symbolic")
        del_btn.set_valign(Gtk.Align.CENTER)
        del_btn.add_css_class("flat")
        del_btn.connect("clicked", self.on_delete_clicked, host, row)
        row.add_suffix(del_btn)
        
        self.hosts_group.add(row)
        self.host_rows.append(row)

    def on_add_clicked(self, btn):
        self.show_edit_dialog()

    def on_edit_clicked(self, btn, host):
        self.show_edit_dialog(host)

    def show_edit_dialog(self, host=None):
        dialog = Adw.MessageDialog(
            transient_for=self,
            heading=_("Edit Host") if host else _("Add Host"),
        )
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("save", _("Save"))
        dialog.set_default_response("save")
        dialog.set_close_response("cancel")
        
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        
        name_entry = Gtk.Entry()
        name_entry.set_placeholder_text(_("Name (e.g. Localhost)"))
        if host:
            name_entry.set_text(host['name'])
            
        hostname_entry = Gtk.Entry()
        hostname_entry.set_placeholder_text(_("Hostname (e.g. http://localhost:11434)"))
        if host:
            hostname_entry.set_text(host['hostname'])
            
        default_check = Gtk.CheckButton(label=_("Set as default host"))
        if host and host.get("default", False):
            default_check.set_active(True)
        elif not host and not self.storage.hosts:
            default_check.set_active(True)
            
        box.append(name_entry)
        box.append(hostname_entry)
        box.append(default_check)
        
        dialog.set_extra_child(box)
        
        def on_response(dialog, response):
            if response == "save":
                name = name_entry.get_text().strip()
                hostname = hostname_entry.get_text().strip()
                is_default = default_check.get_active()
                if name and hostname:
                    if host:
                        self.storage.update_host(host['id'], name, hostname, is_default)
                    else:
                        self.storage.add_host(name, hostname, is_default)
                    self.load_hosts()
                    if self.on_hosts_changed_cb:
                        self.on_hosts_changed_cb()
            dialog.close()
            
        dialog.connect("response", on_response)
        dialog.present()

    def on_delete_clicked(self, btn, host, row):
        dialog = Adw.MessageDialog(
            transient_for=self,
            heading=_("Delete Host"),
            body=f"Are you sure you want to delete {host['name']}?"
        )
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("delete", _("Delete"))
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        
        def on_response(dialog, response):
            if response == "delete":
                self.storage.delete_host(host['id'])
                self.load_hosts()
                if self.on_hosts_changed_cb:
                    self.on_hosts_changed_cb()
            dialog.close()
            
        dialog.connect("response", on_response)
        dialog.present()
