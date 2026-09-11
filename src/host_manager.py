from typing import Any, List, Dict, Optional, Callable
from gi.repository import Adw, Gtk, Gio, GLib, GObject
from .storage import ChatStorage
from . import ollama
from .session import ViewRequests
from .credentials import CredentialError

@Gtk.Template(resource_path='/io/github/jackrabbithanna/Gnollama/host_edit_dialog.ui')
class HostEditDialog(Adw.Dialog):
    """Dialog for adding or editing an Ollama host."""
    __gtype_name__ = 'HostEditDialog'
    __gsignals__ = {'response': (GObject.SignalFlags.RUN_FIRST, None, (str,))}

    name_entry: Gtk.Entry = Gtk.Template.Child()
    hostname_entry: Gtk.Entry = Gtk.Template.Child()
    default_check: Gtk.CheckButton = Gtk.Template.Child()
    provider_dropdown = Gtk.Template.Child()
    fields_box = Gtk.Template.Child()
    save_button = Gtk.Template.Child()
    cancel_button = Gtk.Template.Child()

    def __init__(self, host: Optional[Dict[str, Any]] = None, has_session=False, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.set_title(_("Edit Host") if host else _("Add Host"))
        self.host = host
        self.has_key = bool(has_session or (host and host.get('credential_id')))
        self.busy = False
        self.session_only = False
        self._server_url = host['hostname'] if host and not ollama.is_cloud(host) else ''
        self.provider_dropdown.set_model(Gtk.StringList.new([_('Ollama Server'), _('Ollama Cloud')]))
        self.key_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.api_key_entry = Gtk.PasswordEntry(show_peek_icon=True, hexpand=True)
        self.api_key_entry.set_direction(Gtk.TextDirection.LTR)
        self.key_box.append(Gtk.Label(label=_('API key'), xalign=0, mnemonic_widget=self.api_key_entry))
        self.key_box.append(self.api_key_entry)
        self.key_box.append(Gtk.Label(label=_('Leave blank to keep the existing key.') if self.has_key else
                                     _('Stored securely in your desktop keyring.'), xalign=0, wrap=True))
        self.key_box.append(Gtk.LinkButton(uri='https://ollama.com/settings/keys', label=_('Create an API key')))
        self.fields_box.insert_child_after(self.key_box, self.hostname_entry)
        self.hostname_entry.set_direction(Gtk.TextDirection.LTR)
        self.validation_error = Gtk.Label(xalign=0, wrap=True, visible=False)
        self.validation_error.add_css_class('error')
        self.fields_box.append(self.validation_error)
        self.session_button = Gtk.Button(label=_('Use for This Session'), visible=False)
        self.fields_box.append(self.session_button)
        self.save_button.connect('clicked', lambda *_: self.emit('response', 'save'))
        self.cancel_button.connect('clicked', lambda *_: self.close())
        self.session_button.connect('clicked', self._use_session)
        self.name_entry.connect('changed', self.validate_fields)
        self.hostname_entry.connect('changed', self.validate_fields)
        self.api_key_entry.connect('changed', self.validate_fields)
        self.provider_dropdown.connect('notify::selected', self._provider_changed)
        if host:
            self.name_entry.set_text(host['name'])
            self.hostname_entry.set_text(host['hostname'])
            self.default_check.set_active(host.get("default", False))
            self.provider_dropdown.set_selected(1 if ollama.is_cloud(host) else 0)
        self._provider_changed()
        self.validate_fields()

    @property
    def provider(self):
        return 'ollama_cloud' if self.provider_dropdown.get_selected() == 1 else 'ollama'

    def _provider_changed(self, *args):
        cloud = self.provider == 'ollama_cloud'
        if cloud:
            if self.hostname_entry.get_text() != ollama.CLOUD_URL:
                self._server_url = self.hostname_entry.get_text()
            self.hostname_entry.set_text(ollama.CLOUD_URL)
            if not self.name_entry.get_text().strip():
                self.name_entry.set_text(_('Ollama Cloud'))
        elif self.hostname_entry.get_text() == ollama.CLOUD_URL:
            self.hostname_entry.set_text(self._server_url)
        self.hostname_entry.set_editable(not cloud)
        self.key_box.set_visible(cloud)
        self.session_button.set_visible(False)
        self.session_only = False
        self.validate_fields()

    def _use_session(self, *args):
        self.session_only = True
        self.emit('response', 'save')

    def get_response_enabled(self, response):
        return self.save_button.get_sensitive() if response == 'save' else not self.busy

    def set_busy(self, busy):
        self.busy = busy
        self.fields_box.set_sensitive(not busy)
        self.cancel_button.set_sensitive(not busy)
        self.set_can_close(not busy)
        self.validate_fields()

    def validate_fields(self, *args):
        error = ''
        if not self.name_entry.get_text().strip():
            error = _('Enter a name for this server.')
        else:
            try:
                ollama.validate_host(self.hostname_entry.get_text().strip())
            except (ValueError, ollama.OllamaError) as exc:
                error = str(exc)
        if not error and self.provider == 'ollama_cloud':
            key = self.api_key_entry.get_text()
            if key or not self.has_key:
                try:
                    from .credentials import CredentialStore
                    CredentialStore.validate(key)
                except CredentialError as exc:
                    error = str(exc)
        self.save_button.set_sensitive(not error and not self.busy)
        self.validation_error.set_text(error)
        self.validation_error.set_visible(bool(error) and bool(args))

@Gtk.Template(resource_path='/io/github/jackrabbithanna/Gnollama/host_manager.ui')
class HostManagerDialog(Adw.Window):
    """Dialog for managing Ollama host configurations."""
    __gtype_name__ = 'HostManagerDialog'

    hosts_group: Adw.PreferencesGroup = Gtk.Template.Child()
    add_button: Gtk.Button = Gtk.Template.Child()

    def __init__(self, storage: ChatStorage, on_hosts_changed_cb: Optional[Callable[[], None]] = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.requests = ViewRequests(self)
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
            default_str = _("Default")
            row.set_subtitle(f"{host['hostname']} ({default_str})")
        else:
            row.set_subtitle(host['hostname'])
        
        info_btn = Gtk.Button(label=_("Test Connection"))
        info_btn.set_valign(Gtk.Align.CENTER)
        info_btn.add_css_class("flat")
        info_btn.connect("clicked", self.on_info_clicked, host)
        info_btn.set_tooltip_text(_("Test Connection"))
        row.add_suffix(info_btn)
        
        row.set_activatable(True)
        row.connect('activated', lambda *args: self.show_edit_dialog(host))
        row.set_tooltip_text(_('Edit Server'))

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
        dialog = Adw.AlertDialog(
            heading=host['name'],
            body=_("Checking connection…")
        )
        dialog.add_response("close", _("Close"))
        dialog.set_default_response("close")
        dialog.set_close_response("close")
        dialog.present(self)
        cancel = self.requests.new_cancel()
        dialog.connect('closed', lambda *_: cancel.cancel())

        def fetch_version_thread() -> None:
            try:
                if ollama.is_cloud(host):
                    models = ollama.fetch_models(ollama.CLOUD_URL, cancellable=cancel)
                    msg = _('Ollama Cloud is reachable. Available models: {0}\nYour API key will be checked when you generate a response.').format(len(models))
                else:
                    version = ollama.get_version(host['hostname'], cancellable=cancel)
                    msg = _("Connected\nOllama Version: {0}").format(version)
            except ollama.OllamaError as e:
                msg = _("Connection failed\n{0}").format(str(e))
            self.requests.deliver(dialog.set_body, msg, cancellable=cancel)
            
        from .session import worker
        worker.submit(fetch_version_thread)

    def on_edit_clicked(self, btn: Gtk.Button, host: Dict[str, Any]) -> None:
        """Callback for the 'Edit' button."""
        self.show_edit_dialog(host)

    def show_edit_dialog(self, host: Optional[Dict[str, Any]] = None) -> None:
        """Shows a dialog to add or edit a host."""
        dialog = HostEditDialog(host=host, has_session=bool(host and self.storage.credentials.has_session(host['id'])))
        
        if not host and not self.storage.get_all_hosts():
            dialog.default_check.set_active(True)
        
        def on_response(dialog: HostEditDialog, response: str) -> None:
            if response != 'save' or dialog.busy or not dialog.get_response_enabled('save'):
                return
            values = dict(name=dialog.name_entry.get_text().strip(),
                          hostname=dialog.hostname_entry.get_text().strip(),
                          is_default=dialog.default_check.get_active(),
                          host_id=host['id'] if host else None, provider=dialog.provider,
                          api_key=dialog.api_key_entry.get_text(), session_only=dialog.session_only)
            dialog.set_busy(True)

            def completed(error):
                dialog.set_busy(False)
                if error:
                    dialog.validation_error.set_text(str(error))
                    dialog.validation_error.set_visible(True)
                    dialog.session_button.set_visible(isinstance(error, CredentialError) and
                        dialog.provider == 'ollama_cloud' and bool(dialog.api_key_entry.get_text()))
                    dialog.session_only = False
                else:
                    dialog.api_key_entry.set_text('')
                    dialog.close()
                    if not self.requests.closed:
                        self.load_hosts()
                    if self.on_hosts_changed_cb:
                        self.on_hosts_changed_cb()
                return False

            def save():
                error = None
                try:
                    self.storage.save_host(**values)
                except Exception as exc:
                    error = exc
                finally:
                    values.pop('api_key', None)
                GLib.idle_add(completed, error)
            from .session import worker
            worker.submit(save)
            
        dialog.connect("response", on_response)
        dialog.present(self)

    def on_delete_clicked(self, btn: Gtk.Button, host: Dict[str, Any], row: Adw.ActionRow) -> None:
        """Shows a confirmation dialog before deleting a host."""
        dialog = Adw.AlertDialog(
            heading=_("Delete Host"),
            body=_("Are you sure you want to delete {0}?").format(host['name'])
        )
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("delete", _("Delete"))
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        
        def on_response(dialog: Adw.AlertDialog, response: str) -> None:
            if response == "delete":
                def completed(error):
                    if error and not self.requests.closed:
                        notice = Adw.AlertDialog(heading=_('Delete Failed'), body=str(error))
                        notice.add_response('close', _('Close'))
                        notice.present(self)
                    elif not error:
                        if not self.requests.closed:
                            self.load_hosts()
                        if self.on_hosts_changed_cb:
                            self.on_hosts_changed_cb()
                    return False
                def delete():
                    error = None
                    try:
                        self.storage.delete_host(host['id'])
                    except Exception as exc:
                        error = exc
                    GLib.idle_add(completed, error)
                from .session import worker
                worker.submit(delete)
            dialog.close()
            
        dialog.connect("response", on_response)
        dialog.present(self)
