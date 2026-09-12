"""Shared host/model selection with optional participant settings."""
from gi.repository import Gtk
from .. import ollama
from .chat_input import ChatInput
from .options_panel import OptionsPanel


class TargetPicker(Gtk.Box):
    def __init__(self, storage, changed, target=None, participant=False):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6, hexpand=True)
        self.participant = participant
        self.panel = OptionsPanel()
        self.panel.storage = storage
        self.panel.update_hosts()
        self.input = ChatInput()
        self.input.services = storage.services
        self.input.connection_box.get_parent().remove(self.input.connection_box)
        self.input.connection_box.prepend(self.panel.host_row)
        self.input.thinking_dropdown.get_parent().set_visible(participant)
        self.append(self.input.connection_box)
        self.input.capability_notice.get_parent().remove(self.input.capability_notice)
        self.append(self.input.capability_notice)
        if participant:
            self.system_entry = self.panel.system_prompt_entry
            self.system_entry.get_parent().remove(self.system_entry)
            self.panel.system_group.set_visible(False)
            self.append(Gtk.Label(label=_('System prompt'), xalign=0, mnemonic_widget=self.system_entry))
            self.append(self.system_entry)
            self.panel.remove(self.panel.settings_button)
            self.panel.settings_button.set_label(_('Advanced settings…'))
            self.append(self.panel.settings_button)
            self.panel.watch_draft(changed)
            self.input.thinking_dropdown.connect('notify::selected', changed)
        def refresh(*args):
            host = self.panel.get_selected_host()
            if participant:
                self.panel.set_cloud(bool(host and ollama.is_cloud(host)))
            self.input.fetch_models(storage.connection(host) if host else None,
                                    host_id=host['id'] if host else None)
            changed()
        if target:
            if participant:
                self.panel.restore_draft(target.get('panel', {}))
                self.input.load_thinking_val(target.get('thinking'))
            if participant and not any(host['id'] == target.get('host_id') for host in self.panel.host_list):
                self.panel.host_dropdown.set_selected(Gtk.INVALID_LIST_POSITION)
            for index, host in enumerate(self.panel.host_list):
                if host['id'] == target.get('host_id'):
                    self.panel.host_dropdown.set_selected(index)
            self.input.pending_model_selection = target.get('model')
        self.panel.host_dropdown.connect('notify::selected-item', refresh)
        self.input.model_dropdown.connect('notify::selected', lambda *args: changed())
        self.input.connect('capabilities-changed', lambda *args: changed())
        refresh()

    def value(self):
        host = self.panel.get_selected_host()
        value = dict(host_id=host['id'] if host else None, model=self.input.get_selected_model())
        if self.participant:
            value.update(panel=self.panel.snapshot_draft(), thinking=self.input.get_thinking_value())
        return value

    def request_settings(self):
        value = self.value()
        return dict(host_id=value['host_id'], model=value['model'], system=self.system_entry.read_draft() or None,
                    options=self.panel.get_options_from_ui(), thinking=self.input.get_thinking_value(),
                    keep_alive=self.panel.get_keep_alive(), logprobs=self.panel.logprobs_check.get_active(),
                    top_logprobs=self.panel.get_logprobs(), show_stats=self.panel.stats_check.get_active())

    def close(self):
        self.input.cancel_fetches()
        if self.panel._settings_dialog:
            self.panel._settings_dialog.close()
