from gettext import gettext as _
from ..providers import capabilities
import math
from typing import Dict, Any, Callable, List, Optional
from gi.repository import Adw, Gtk, GObject, GLib, Pango
from ..structured import request_format
from .json_view import SchemaEditor
from .tool_view import ToolsEditor
from ..tool_calling import parse_tools, InvalidTools
from .search import searchable
from .composer import Composer

# The field of each sampling option, and the capability that some fields need.
OPTION_FIELDS = {'seed': 'seed_entry', 'temperature': 'temperature_entry', 'top_k': 'top_k_entry',
                 'top_p': 'top_p_entry', 'min_p': 'min_p_entry', 'num_ctx': 'num_ctx_entry',
                 'num_predict': 'num_predict_entry', 'stop': 'stop_entry'}
FIELD_FLAGS = {'top_k_entry': 'sampling_extras', 'min_p_entry': 'sampling_extras', 'num_ctx_entry': 'context_size',
               'top_logprobs_entry': 'logprobs'}

@Gtk.Template(resource_path='/io/github/jackrabbithanna/Tureen/widgets/options_panel.ui')
class OptionsPanel(Gtk.Box):
    """Common controls and reusable, adaptive chat settings."""
    __gtype_name__ = 'OptionsPanel'
    __gsignals__ = {'tools-options-changed': (GObject.SignalFlags.RUN_FIRST, None, ())}

    output_dropdown = Gtk.Template.Child()
    schema_button = Gtk.Template.Child()
    tools_box = Gtk.Template.Child()
    tools_check = Gtk.Template.Child()
    tools_button = Gtk.Template.Child()
    tools_notice = Gtk.Template.Child()
    settings_button = Gtk.Template.Child()

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.capabilities = capabilities(None)
        self._provider = self._reason = None
        self._settings_dialog = None
        self._build_settings()
        self.settings_button.connect('clicked', self.open_settings)
        self.storage = None
        self.host_list: List[Dict[str, Any]] = []
        self.schema_text = ''
        self._schema_dialog = None
        self._restoring_options = False
        self.tools_text = ''
        self._tools_dialog = None
        self.tools_available = False
        self.tool_support = None
        self.tools_loading = False
        self.output_modes = ['text', 'json', 'schema']
        self.keep_alive_values = [None, 0, 300, 1800, -1, 'custom']
        self.output_dropdown.set_model(Gtk.StringList.new([_('Text'), _('JSON'), _('JSON Schema')]))
        self.keep_alive_dropdown.set_model(Gtk.StringList.new([
            _('Server default'), _('Unload after reply'), _('Five minutes'),
            _('Thirty minutes'), _('Indefinitely'), _('Custom seconds')]))
        self.output_dropdown.connect('notify::selected', self._format_changed)
        self.keep_alive_dropdown.connect('notify::selected', self._keep_alive_changed)
        self.schema_button.connect('clicked', self.edit_schema)
        self.tools_check.connect('toggled', self._tools_changed)
        self.tools_button.connect('clicked', self.edit_tools)
        self._format_changed()
        self._keep_alive_changed()
        self.provider_notice = Gtk.Label(xalign=0, wrap=True, visible=False)
        self.append(self.provider_notice)

    def set_capabilities(self, capabilities, provider=None, reason=None):
        """Disable what the selected host or model lacks, and say why.

        `provider` is the host's own record where `capabilities` is one model's.
        `reason` replaces the explanation, for a panel that serves several targets.
        """
        self.capabilities = capabilities
        self._provider, self._reason = provider, reason
        self.output_dropdown.set_sensitive(capabilities.structured_output)
        self.output_dropdown.set_tooltip_text(None if capabilities.structured_output else self._why('structured_output'))
        self.memory_group.set_sensitive(capabilities.keep_alive)
        for name in {*OPTION_FIELDS.values(), *FIELD_FLAGS}:
            label, hint = self.field_hints[name]
            offered = self._offered(name)
            label.set_text(hint if offered else self._why(FIELD_FLAGS.get(name)))
            if name != 'top_logprobs_entry':
                getattr(self, name).get_parent().set_sensitive(offered)
        self.logprobs_check.set_sensitive(capabilities.logprobs)
        self._logprobs_changed()
        for widget in (self.tools_check, self.tools_button):
            widget.set_sensitive(capabilities.tools)
        self.update_tools_notice()
        self.provider_notice.set_text(_(capabilities.options_notice) if capabilities.options_notice else '')
        self.provider_notice.set_visible(bool(capabilities.options_notice))
        self._format_changed()

    def _offered(self, name):
        """Whether the value of the field `name` can be sent to the selected host and model."""
        if name in FIELD_FLAGS:
            return getattr(self.capabilities, FIELD_FLAGS[name])
        return all(OPTION_FIELDS[key] != name for key in self.capabilities.left_out)

    def _why(self, flag):
        if self._reason:
            return self._reason
        # The host's type takes it, so it is this model that does not.
        if self._provider is not None and getattr(self._provider, flag or '', True):
            return _('Not available for this model.')
        return _('Not available on this server.')

    def _logprobs_changed(self, *args):
        self.top_logprobs_entry.get_parent().set_sensitive(self.logprobs_enabled())

    def logprobs_enabled(self):
        return self.capabilities.logprobs and self.logprobs_check.get_active()

    def output_mode(self):
        """The output mode a request is sent with."""
        return self.output_modes[self.output_dropdown.get_selected()] if self.capabilities.structured_output else 'text'

    def get_unsent(self):
        """What the panel holds for options the host or model lacks, as typed.

        Stored with the chat and never sent or checked; `load_options` puts it back.
        """
        limits = self.capabilities
        held = {key: getattr(self, name).get_text().strip() for key, name in OPTION_FIELDS.items()
                if not self._offered(name)}
        if not limits.logprobs and self.logprobs_check.get_active():
            held.update(logprobs=True, top_logprobs=self.top_logprobs_entry.get_text().strip())
        if not limits.structured_output:
            held['output_mode'] = self.output_modes[self.output_dropdown.get_selected()]
        if not limits.keep_alive:
            value = self.keep_alive_values[self.keep_alive_dropdown.get_selected()]
            held['keep_alive'] = self.keep_alive_entry.get_text().strip() if value == 'custom' else value
        if self.tools_available and not limits.tools and self.tools_check.get_active():
            held['tools_enabled'] = True
        # Zero is a retention time; the rest are the defaults of an untouched panel.
        return {key: value for key, value in held.items() if value not in ('', None, 'text')}

    def snapshot_draft(self):
        return dict(fields={name: getattr(self, name).get_text() for name in self.field_errors},
            output=self.output_dropdown.get_selected(), retention=self.keep_alive_dropdown.get_selected(),
            stats=self.stats_check.get_active(), logprobs=self.logprobs_check.get_active(),
            tools=self.tools_check.get_active(), tools_text=self.tools_text, schema_text=self.schema_text)

    def restore_draft(self, settings):
        self._restoring_options = True
        try:
            for name, value in settings.get('fields', {}).items():
                if name in self.field_errors:
                    getattr(self, name).set_text(value)
            self.tools_text = settings.get('tools_text', '')
            self.schema_text = settings.get('schema_text', '')
            for key, widget in [('output', self.output_dropdown), ('retention', self.keep_alive_dropdown)]:
                widget.set_selected(settings.get(key, 0))
            for key, widget in [('stats', self.stats_check), ('logprobs', self.logprobs_check), ('tools', self.tools_check)]:
                widget.set_active(settings.get(key, key == 'stats'))
        finally:
            self._restoring_options = False

    def watch_draft(self, callback):
        for name in self.field_errors:
            getattr(self, name).connect('changed', callback)
        for widget in (self.output_dropdown, self.keep_alive_dropdown):
            widget.connect('notify::selected', callback)
        for widget in (self.stats_check, self.logprobs_check, self.tools_check):
            widget.connect('toggled', callback)
        self.connect('tools-options-changed', callback)

    def _build_settings(self):
        self.settings_content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24,
                                       margin_start=12, margin_end=12, margin_top=12, margin_bottom=12)
        self.field_errors = {}
        self.field_hints = {}
        self.host_dropdown = searchable(Gtk.DropDown(hexpand=True))
        factory = Gtk.SignalListItemFactory()
        factory.connect('setup', lambda f, item: item.set_child(Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, max_width_chars=20)))
        factory.connect('bind', lambda f, item: item.get_child().set_text(item.get_item().get_string()))
        self.host_dropdown.set_factory(factory)
        self.host_row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.host_row.append(Gtk.Label(label=_('Server'), xalign=0, mnemonic_widget=self.host_dropdown))
        self.host_row.append(self.host_dropdown)

        def group(title, description=''):
            value = Adw.PreferencesGroup(title=title, description=description)
            self.settings_content.append(value)
            return value

        def field(group, name, title, hint):
            entry = Composer(hexpand=True) if name == 'system_prompt_entry' else Gtk.Entry(hexpand=True)
            if isinstance(entry, Composer):
                entry.send_on_enter = False
            entry.set_placeholder_text(_('Server default'))
            entry.set_width_chars(8)
            setattr(self, name, entry)
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                          margin_top=8, margin_bottom=8)
            box.append(Gtk.Label(label=title, xalign=0, wrap=True,
                                 wrap_mode=Pango.WrapMode.WORD_CHAR, mnemonic_widget=entry))
            box.append(entry)
            help_label = Gtk.Label(label=hint, xalign=0, wrap=True)
            box.append(help_label)
            self.field_hints[name] = (help_label, hint)
            error = Gtk.Label(xalign=0, wrap=True, visible=False)
            error.add_css_class('error')
            box.append(error)
            self.field_errors[name] = error
            entry.connect('changed', lambda *args: (error.set_visible(False), entry.remove_css_class('error')))
            group.add(box)

        system = self.system_group = group(_('System Instructions'))
        field(system, 'system_prompt_entry', _('System prompt'), _('Optional instructions applied to this conversation.'))
        self.system_prompt_entry.set_placeholder_text(_('Optional instructions'))
        limits = group(_('Generation Limits'), _('Blank fields use the server’s defaults.'))
        field(limits, 'num_ctx_entry', _('Context size (tokens)'), _('Maximum context available to the model; larger values need more memory.'))
        field(limits, 'num_predict_entry', _('Maximum output tokens'), _('Use −1 for unlimited output or −2 to fill the context.'))
        field(limits, 'stop_entry', _('Stop sequences'), _('Separate sequences with commas. Generation stops when one is produced.'))
        sampling = group(_('Sampling'))
        for name, title, hint in [
            ('temperature_entry', _('Temperature'), _('Higher values increase variation; zero is more predictable.')),
            ('seed_entry', _('Seed'), _('An integer used to initialize random sampling.')),
            ('top_k_entry', _('Top K'), _('Number of candidate tokens to consider.')),
            ('top_p_entry', _('Top P'), _('Cumulative probability cutoff, between 0 and 1.')),
            ('min_p_entry', _('Min P'), _('Minimum relative token probability, between 0 and 1.'))]:
            field(sampling, name, title, hint)
        memory = self.memory_group = group(_('Model Retention'))
        self.keep_alive_dropdown = Gtk.DropDown(hexpand=True)
        self.keep_alive_dropdown.set_factory(factory)
        row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                      margin_top=8, margin_bottom=8)
        row.append(Gtk.Label(label=_('Keep model loaded'), xalign=0, wrap=True,
                             mnemonic_widget=self.keep_alive_dropdown))
        row.append(self.keep_alive_dropdown)
        memory.add(row)
        field(memory, 'keep_alive_entry', _('Custom duration (seconds)'), _('Enter a positive whole number of seconds.'))
        self.keep_alive_row = self.keep_alive_entry.get_parent()
        diagnostics = group(_('Diagnostics'))
        self.stats_check = Gtk.CheckButton(active=True)
        self.stats_check.set_child(Gtk.Label(label=_('Show response statistics'),
                                            xalign=0, wrap=True, mnemonic_widget=self.stats_check))
        self.logprobs_check = Gtk.CheckButton()
        self.logprobs_check.set_child(Gtk.Label(label=_('Return token probabilities (logprobs)'),
                                               xalign=0, wrap=True, mnemonic_widget=self.logprobs_check))
        diagnostics.add(self.stats_check)
        diagnostics.add(self.logprobs_check)
        field(diagnostics, 'top_logprobs_entry', _('Top logprobs'), _('Number of alternative token probabilities, from 0 to 20.'))
        self.logprobs_check.connect('toggled', self._logprobs_changed)
        self._logprobs_changed()

    def field_error(self, name, message):
        widget = getattr(self, name)
        self.field_errors[name].set_text(message)
        self.field_errors[name].set_visible(True)
        widget.add_css_class('error')
        self.open_settings()
        widget.grab_focus()
        raise ValueError(message)

    def open_settings(self, *args):
        if not isinstance(self.get_root(), Gtk.Window):
            return
        if self._settings_dialog is None:
            dialog = self._settings_dialog = Adw.Dialog(title=getattr(self, 'settings_title', _('Chat Settings')), content_width=600, content_height=660)
            toolbar = Adw.ToolbarView()
            header = Adw.HeaderBar()
            done = Gtk.Button(label=_('Done'), css_classes=['suggested-action'])
            done.connect('clicked', self._settings_done)
            header.pack_end(done)
            toolbar.add_top_bar(header)
            scroll = Gtk.ScrolledWindow(child=self.settings_content, hscrollbar_policy=Gtk.PolicyType.NEVER)
            toolbar.set_content(scroll)
            dialog.set_child(toolbar)
            def closed(*args):
                scroll.set_child(None)
                self._settings_dialog = None
            dialog.connect('closed', closed)
        self._settings_dialog.present(self)

    def _settings_done(self, *args):
        try:
            self.get_options_from_ui()
            self.get_keep_alive()
            self.get_logprobs()
        except ValueError:
            return
        self._settings_dialog.close()

    def close_dialogs(self):
        for dialog in (self._tools_dialog, self._schema_dialog, self._settings_dialog):
            if dialog is not None:
                dialog.close()

    def get_keep_alive(self):
        if not self.capabilities.keep_alive:
            return None
        value = self.keep_alive_values[self.keep_alive_dropdown.get_selected()]
        if value == 'custom':
            try:
                value = int(self.keep_alive_entry.get_text())
                if value <= 0:
                    raise ValueError()
            except ValueError:
                self.field_error('keep_alive_entry', _('Enter a positive whole number of seconds.'))
        return value

    def get_logprobs(self):
        text = self.top_logprobs_entry.get_text().strip()
        if not self.logprobs_enabled() or not text:
            return None
        try:
            value = int(text)
            if not 0 <= value <= 20:
                raise ValueError()
        except ValueError:
            self.field_error('top_logprobs_entry', _('Top logprobs must be between 0 and 20.'))
        return value

    def _tools_changed(self, *args):
        self.update_tools_notice()
        if not self._restoring_options:
            self.emit('tools-options-changed')
            if self.tools_check.get_active() and not self.tools_text.strip() and self.get_mapped():
                GLib.idle_add(self._open_missing_tools)

    def _open_missing_tools(self):
        if (self.get_tools_options()['tools_enabled'] and self.get_mapped()
                and not self.tools_text.strip() and self._tools_dialog is None):
            self.edit_tools()
        return False

    def update_tools_notice(self):
        try:
            count = len(parse_tools(self.tools_text)) if self.tools_text.strip() else 0
        except InvalidTools:
            count = 0
        enabled = self.tools_check.get_active()
        notice = _('Enabled tools: {0}. Results are supplied manually.').format(count) if enabled else _('Tool calling is off.')
        if not self.capabilities.tools:
            # Shown whether or not the box is ticked: it is why the box cannot be.
            notice, enabled = self._why('tools'), True
        elif enabled:
            if self.tools_loading:
                notice += ' ' + _('Checking tool support…')
            elif self.tool_support is False:
                notice += ' ' + _('This model reports no tool support; you can still test the API.')
            elif self.tool_support is None:
                notice += ' ' + _('Tool support is unknown.')
        self.tools_notice.set_text(notice)
        self.tools_notice.set_visible(enabled)

    def edit_tools(self, *args, error=None):
        if not self.tools_available or not isinstance(self.get_root(), Gtk.Window):
            return
        if self._tools_dialog is None:
            def apply(text):
                self.tools_text = text
                self._tools_changed()
            self._tools_dialog = ToolsEditor(self.tools_text, apply)
            self._tools_dialog.connect('closed', lambda *args: setattr(self, '_tools_dialog', None))
        if error:
            self._tools_dialog.show_error(error)
        self._tools_dialog.present(self)

    def get_tools_options(self):
        enabled = self.tools_available and self.capabilities.tools and self.tools_check.get_active()
        return {'tools_enabled': enabled, 'tools_text': self.tools_text}

    def _format_changed(self, *args):
        schema_mode = self.capabilities.structured_output and self.output_dropdown.get_selected() == 2
        self.schema_button.set_visible(schema_mode)
        if (args and schema_mode and not self.schema_text.strip()
                and not self._restoring_options and self.get_mapped()):
            # Let the dropdown finish handling selection before presenting a dialog.
            GLib.idle_add(self._open_missing_schema)

    def _open_missing_schema(self):
        if (self.capabilities.structured_output and self.get_mapped() and self.output_dropdown.get_selected() == 2
                and not self.schema_text.strip() and self._schema_dialog is None):
            self.edit_schema()
        return False

    def _keep_alive_changed(self, *args):
        self.keep_alive_row.set_visible(self.keep_alive_dropdown.get_selected() == 5)

    def edit_schema(self, *args, error=None):
        if not isinstance(self.get_root(), Gtk.Window):
            return
        if self._schema_dialog is None:
            def apply(text):
                self.schema_text = text
            self._schema_dialog = SchemaEditor(self.schema_text, apply)
            self._schema_dialog.connect('closed', lambda *args: setattr(self, '_schema_dialog', None))
        if error:
            self._schema_dialog.show_error(error)
        self._schema_dialog.present(self)

    def get_request_settings(self):
        mode = self.output_mode()
        output_format = request_format(mode, self.schema_text)
        keep_alive = self.get_keep_alive()
        tools_options = self.get_tools_options()
        tools = parse_tools(self.tools_text) if tools_options['tools_enabled'] else None
        return dict(output_mode=mode, schema_text=self.schema_text, format=output_format,
                    keep_alive=keep_alive, tools=tools, **tools_options)

    def update_hosts(self) -> None:
        """Reloads the host list from storage and updates the dropdown."""
        if not self.storage:
            return
        selected = self.get_selected_host()
        selected_id = selected['id'] if selected else None
        hosts = self.storage.get_all_hosts()
        self.host_list = hosts
        
        host_names = [h['name'] for h in hosts]
        if not host_names:
            host_names = [_("No hosts configured")]
        
        string_list = Gtk.StringList.new(host_names)
        self.host_dropdown.set_model(string_list)
        
        target_idx = 0
        for i, h in enumerate(hosts):
            if h.get('default', False):
                target_idx = i
                break
                
        for i, host in enumerate(hosts):
            if host['id'] == selected_id:
                target_idx = i
                break
        if hosts:
            self.host_dropdown.set_selected(target_idx)

    def get_selected_host(self) -> Optional[Dict[str, Any]]:
        """Returns the currently selected host configuration."""
        if not self.host_list:
            return None
        idx = self.host_dropdown.get_selected()
        if idx != Gtk.INVALID_LIST_POSITION and idx < len(self.host_list):
            return self.host_list[idx]
        return None
        
    def get_options_from_ui(self) -> Dict[str, Any]:
        """Extracts Ollama generation options from the UI input fields."""
        options = {}
        key_names = OPTION_FIELDS
        def add_option(entry: Gtk.Entry, key: str, type_func: Callable[[str], Any]) -> None:
            # What the host or model lacks stays in its field, unchecked, and is left out of the request.
            text = entry.get_text().strip() if self._offered(key_names[key]) else ''
            if text:
                try:
                    val = type_func(text)
                    options[key] = val
                except ValueError:
                    self.field_error(key_names[key], _("Enter a valid number."))
                if isinstance(val, float) and not math.isfinite(val):
                    self.field_error(key_names[key], _("Enter a valid number."))
        
        add_option(self.seed_entry, 'seed', int)
        add_option(self.temperature_entry, 'temperature', float)
        add_option(self.top_k_entry, 'top_k', int)
        add_option(self.min_p_entry, 'min_p', float)
        add_option(self.top_p_entry, 'top_p', float)
        add_option(self.num_ctx_entry, 'num_ctx', int)
        add_option(self.num_predict_entry, 'num_predict', int)
        
        stop_text = self.stop_entry.get_text().strip() if self._offered('stop_entry') else ''
        if stop_text:
            stops = [s.strip() for s in stop_text.split(',') if s.strip()]
            if stops:
                options['stop'] = stops
        
        for key in ('temperature', 'top_k'):
            if options.get(key, 0) < 0:
                self.field_error(key_names[key], _("Value must not be negative."))
        for key in ('top_p', 'min_p'):
            if key in options and not 0 <= options[key] <= 1:
                self.field_error(key_names[key], _("Value must be between 0 and 1."))
        if 'num_ctx' in options and options['num_ctx'] <= 0:
            self.field_error('num_ctx_entry', _('Context size must be positive.'))
        if 'num_predict' in options and options['num_predict'] < -2:
            self.field_error('num_predict_entry', _('Max tokens must be -2, -1, or a nonnegative integer.'))
        return options

    def load_options(self, wire, view) -> None:
        """Populates the panel from stored WireOptions and ViewState.

        What was held and not sent goes back in its field, whatever the host takes now.
        """
        held = view.unsent
        mode = held.get('output_mode', view.output_mode)
        options = {**wire.options, **{key: held[key] for key in OPTION_FIELDS if key in held}}
        self._restoring_options = True
        self.tools_text = view.tools_text
        self.tools_check.set_active(view.tools_enabled or held.get('tools_enabled') is True)
        self.update_tools_notice()
        self.schema_text = view.schema_text
        self.output_dropdown.set_selected(self.output_modes.index(mode) if mode in self.output_modes else 0)
        self._restoring_options = False
        keep_alive = held.get('keep_alive', wire.keep_alive)
        if keep_alive in self.keep_alive_values:
            self.keep_alive_dropdown.set_selected(self.keep_alive_values.index(keep_alive))
        else:
            self.keep_alive_dropdown.set_selected(5)
            self.keep_alive_entry.set_text(str(keep_alive))
        self.stats_check.set_active(view.show_stats)
        self.seed_entry.set_text(str(options.get('seed', '')))
        self.temperature_entry.set_text(str(options.get('temperature', '')))
        self.top_k_entry.set_text(str(options.get('top_k', '')))
        self.top_p_entry.set_text(str(options.get('top_p', '')))
        self.min_p_entry.set_text(str(options.get('min_p', '')))
        self.num_ctx_entry.set_text(str(options.get('num_ctx', '')))
        self.num_predict_entry.set_text(str(options.get('num_predict', '')))
        
        stops = options.get('stop')
        if isinstance(stops, list):
            self.stop_entry.set_text(", ".join(stops))
        elif isinstance(stops, str):
            self.stop_entry.set_text(stops)
        else:
            self.stop_entry.set_text("")
            
        self.logprobs_check.set_active(wire.logprobs or held.get('logprobs') is True)
        top_logprobs = held.get('top_logprobs', wire.top_logprobs)
        if top_logprobs is not None:
            self.top_logprobs_entry.set_text(str(top_logprobs))
