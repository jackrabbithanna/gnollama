from typing import Any, List, Dict, Optional, Callable
from gi.repository import Adw, Gtk, Gio, GLib
from .storage import ChatStorage
from . import ollama
import threading

@Gtk.Template(resource_path='/io/github/jackrabbithanna/Gnollama/host_edit_dialog.ui')
class HostEditDialog(Adw.MessageDialog):
    """Dialog for adding or editing an Ollama host."""
    __gtype_name__ = 'HostEditDialog'

    name_entry: Gtk.Entry = Gtk.Template.Child()
    hostname_entry: Gtk.Entry = Gtk.Template.Child()
    default_check: Gtk.CheckButton = Gtk.Template.Child()

    def __init__(self, host: Optional[Dict[str, Any]] = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.set_heading(_("Edit Host") if host else _("Add Host"))
        if host:
            self.name_entry.set_text(host['name'])
            self.hostname_entry.set_text(host['hostname'])
            self.default_check.set_active(host.get("default", False))

@Gtk.Template(resource_path='/io/github/jackrabbithanna/Gnollama/host_manager.ui')
class HostManagerDialog(Adw.Window):
    """Dialog for managing Ollama host configurations."""
    __gtype_name__ = 'HostManagerDialog'

    hosts_group: Adw.PreferencesGroup = Gtk.Template.Child()
    add_button: Gtk.Button = Gtk.Template.Child()

    def __init__(self, storage: ChatStorage, on_hosts_changed_cb: Optional[Callable[[], None]] = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.storage: ChatStorage = storage
        self.on_hosts_changed_cb: Optional[Callable[[], None]] = on_hosts_changed_cb
        self.host_rows: List[Adw.ActionRow] = []
        
        self.add_button.connect("clicked", self.on_add_clicked)
        self.load_hosts()

    def load_hosts(self) -> None:
        """Reloads the host list from storage."""
        for row in self.host_rows:
            self.hosts_group.remove(row)
        self.host_rows.clear()
            
        hosts = self.storage.get_all_hosts()
        for host in hosts:
            self.add_host_row(host)

    def add_host_row(self, host: Dict[str, Any]) -> None:
        """Adds a single host row to the preferences group."""
        row = Adw.ActionRow()
        row.set_title(host['name'])
        if host.get('default', False):
            row.set_subtitle(f"{host['hostname']} (Default)")
        else:
            row.set_subtitle(host['hostname'])
        
        info_btn = Gtk.Button.new_from_icon_name("dialog-information-symbolic")
        info_btn.set_valign(Gtk.Align.CENTER)
        info_btn.add_css_class("flat")
        info_btn.connect("clicked", self.on_info_clicked, host)
        info_btn.set_tooltip_text(_("Test Connection"))
        row.add_suffix(info_btn)
        
        edit_btn = Gtk.Button.new_from_icon_name("document-open-symbolic")
        edit_btn.set_valign(Gtk.Align.CENTER)
        edit_btn.add_css_class("flat")
        edit_btn.connect("clicked", self.on_edit_clicked, host)
        edit_btn.set_tooltip_text(_("Edit Host"))
        row.add_suffix(edit_btn)
        
        del_btn = Gtk.Button.new_from_icon_name("user-trash-symbolic")
        del_btn.set_valign(Gtk.Align.CENTER)
        del_btn.add_css_class("flat")
        del_btn.connect("clicked", self.on_delete_clicked, host, row)
        del_btn.set_tooltip_text(_("Delete Host"))
        row.add_suffix(del_btn)
        
        self.hosts_group.add(row)
        self.host_rows.append(row)

    def on_add_clicked(self, btn: Gtk.Button) -> None:
        """Callback for the 'Add' button."""
        self.show_edit_dialog()

    def on_info_clicked(self, btn: Gtk.Button, host: Dict[str, Any]) -> None:
        """Displays connection info and version for a host."""
        dialog = Adw.MessageDialog(
            transient_for=self,
            heading=host['name'],
            body=_("Fetching version...")
        )
        dialog.add_response("close", _("Close"))
        dialog.set_default_response("close")
        dialog.set_close_response("close")
        dialog.present()
        
        def fetch_version_thread() -> None:
            version, error = ollama.get_version(host['hostname'])
            if version:
                msg = _("Connected\nOllama Version: {0}").format(version)
            else:
                msg = _("Connection failed\n{0}").format(error)
            GLib.idle_add(dialog.set_body, msg)
            
        thread = threading.Thread(target=fetch_version_thread, daemon=True)
        thread.start()

    def on_edit_clicked(self, btn: Gtk.Button, host: Dict[str, Any]) -> None:
        """Callback for the 'Edit' button."""
        self.show_edit_dialog(host)

    def show_edit_dialog(self, host: Optional[Dict[str, Any]] = None) -> None:
        """Shows a dialog to add or edit a host."""
        dialog = HostEditDialog(host=host, transient_for=self)
        
        if not host and not self.storage.get_all_hosts():
            dialog.default_check.set_active(True)
        
        def on_response(dialog: HostEditDialog, response: str) -> None:
            if response == "save":
                name = dialog.name_entry.get_text().strip()
                hostname = dialog.hostname_entry.get_text().strip()
                is_default = dialog.default_check.get_active()
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

    def on_delete_clicked(self, btn: Gtk.Button, host: Dict[str, Any], row: Adw.ActionRow) -> None:
        """Shows a confirmation dialog before deleting a host."""
        dialog = Adw.MessageDialog(
            transient_for=self,
            heading=_("Delete Host"),
            body=_("Are you sure you want to delete {0}?").format(host['name'])
        )
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("delete", _("Delete"))
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        
        def on_response(dialog: Adw.MessageDialog, response: str) -> None:
            if response == "delete":
                self.storage.delete_host(host['id'])
                self.load_hosts()
                if self.on_hosts_changed_cb:
                    self.on_hosts_changed_cb()
            dialog.close()
            
        dialog.connect("response", on_response)
        dialog.present()
