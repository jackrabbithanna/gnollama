"""Shared host/model selection with optional participant settings."""
from gettext import gettext as _
from gi.repository import Gtk
from ..chat_controller import ComposerInput, ModelLimits
from ..decision import check_request
from ..image_generation import ImageParameters, unavailable
from .chat_input import ChatInput
from .options_panel import OptionsPanel


class TargetPicker(Gtk.Box):
    def __init__(self, context, changed, target=None, participant=False, kind='chat'):
        """`kind` is the list of models offered: 'chat', 'decision', or 'image'."""
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6, hexpand=True)
        self.participant = participant
        self.context, self.limits = context, ModelLimits(context)
        self.panel = OptionsPanel()
        self.panel.storage = context.storage
        self.panel.update_hosts()
        self.input = ChatInput()
        self.input.services = context.services
        self.input.kind = kind
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
            if kind != 'chat':
                # A host that cannot decide or make images stays listed; it says why and lists no models.
                capabilities = context.capabilities(host) if host else None
                reason = '' if not host else (
                    check_request((), (), capabilities) if kind == 'decision' else unavailable(capabilities))
                self.input.set_unavailable(reason)
                host = None if reason else host
            self.input.fetch_models(context.connection(host) if host else None,
                                    host_id=host['id'] if host else None)
            changed()
        self.refresh = refresh
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
        def limits_changed(*args):
            if participant:
                self.panel.set_capabilities(self.capabilities(), context.capabilities(self.panel.get_selected_host()))
            changed()
        # Emitted for every change of host or model, and again when a model's details arrive.
        self.input.connect('capabilities-changed', limits_changed)
        refresh()

    def capabilities(self):
        """What the selected model can be sent, or None with no host; validation checks the same record."""
        host = self.panel.get_selected_host()
        return self.limits.of(host, self.input.get_selected_model()) if host else None

    def decision_capabilities(self):
        """What the selected decision model can be asked, or None with no host."""
        host = self.panel.get_selected_host()
        return self.context.connection(host).decision_capabilities(self.input.selected_details()) if host else None

    def image_parameters(self):
        """What the selected image model takes; nothing with no host or model."""
        host = self.panel.get_selected_host()
        if host is None or not self.input.get_selected_model():
            return ImageParameters()
        return self.context.connection(host).image_parameters(self.input.selected_details())

    def select(self, target):
        """Choose a saved host and model. A host that is gone leaves the one selected."""
        self.input.pending_model_selection = target.get('model')
        for index, host in enumerate(self.panel.host_list):
            if host['id'] == target.get('host_id') and index != self.panel.host_dropdown.get_selected():
                self.panel.host_dropdown.set_selected(index)
                return
        self.refresh()

    def value(self):
        host = self.panel.get_selected_host()
        value = dict(host_id=host['id'] if host else None, model=self.input.get_selected_model())
        if self.participant:
            value.update(panel=self.panel.snapshot_draft(), thinking=self.input.get_thinking_value())
        return value

    def request_settings(self):
        """This participant's RequestSettings; its host URL is resolved when the run starts."""
        return ComposerInput(
            host=self.panel.get_selected_host(), model=self.input.get_selected_model(),
            system=self.system_entry.read_draft() or None, options=self.panel.get_options_from_ui(),
            thinking=self.input.get_thinking_value(), request=dict(keep_alive=self.panel.get_keep_alive()),
            logprobs=self.panel.logprobs_enabled(), top_logprobs=self.panel.get_logprobs(),
            show_stats=self.panel.stats_check.get_active(), unsent=self.held()).settings()

    def held(self):
        # A participant has no output mode or tools to hold.
        return {key: value for key, value in self.panel.get_unsent().items()
                if key not in ('output_mode', 'tools_enabled')}

    def close(self):
        self.input.cancel_fetches()
        self.panel.close_dialogs()
