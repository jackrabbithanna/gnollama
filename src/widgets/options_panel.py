import math
from typing import Dict, Any, Callable, List, Optional
from gi.repository import Gtk, GObject, GLib
from ..storage import ChatStorage
from ..structured import request_format
from .json_view import SchemaEditor
from .tool_view import ToolsEditor
from ..tool_calling import parse_tools, InvalidTools

@Gtk.Template(resource_path='/io/github/jackrabbithanna/Gnollama/widgets/options_panel.ui')
class OptionsPanel(Gtk.Box):
    """Common tool/output controls followed by expandable generation settings."""
    __gtype_name__ = 'OptionsPanel'
    __gsignals__ = {'tools-options-changed': (GObject.SignalFlags.RUN_FIRST, None, ())}

    host_dropdown: Gtk.DropDown = Gtk.Template.Child()
    system_prompt_entry: Gtk.Entry = Gtk.Template.Child()
    stats_check: Gtk.CheckButton = Gtk.Template.Child()
    logprobs_check: Gtk.CheckButton = Gtk.Template.Child()
    top_logprobs_entry: Gtk.Entry = Gtk.Template.Child()
    
    seed_entry: Gtk.Entry = Gtk.Template.Child()
    temperature_entry: Gtk.Entry = Gtk.Template.Child()
    top_k_entry: Gtk.Entry = Gtk.Template.Child()
    top_p_entry: Gtk.Entry = Gtk.Template.Child()
    min_p_entry: Gtk.Entry = Gtk.Template.Child()
    num_ctx_entry: Gtk.Entry = Gtk.Template.Child()
    num_predict_entry: Gtk.Entry = Gtk.Template.Child()
    stop_entry: Gtk.Entry = Gtk.Template.Child()
    output_dropdown = Gtk.Template.Child()
    schema_button = Gtk.Template.Child()
    keep_alive_dropdown = Gtk.Template.Child()
    keep_alive_entry = Gtk.Template.Child()
    tools_box = Gtk.Template.Child()
    tools_check = Gtk.Template.Child()
    tools_button = Gtk.Template.Child()
    tools_notice = Gtk.Template.Child()
    advanced_expander = Gtk.Template.Child()

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
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

    def _tools_changed(self, *args):
        self.update_tools_notice()
        if not self._restoring_options:
            self.emit('tools-options-changed')
            if self.tools_check.get_active() and not self.tools_text.strip() and self.get_mapped():
                GLib.idle_add(self._open_missing_tools)

    def _open_missing_tools(self):
        if (self.tools_available and self.get_mapped() and self.tools_check.get_active()
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
        if enabled:
            if self.tools_loading:
                notice += ' ' + _('Checking tool support…')
            elif self.tool_support is False:
                notice += ' ' + _('This model reports no tool support; you can still test the API.')
            elif self.tool_support is None:
                notice += ' ' + _('Tool support is unknown.')
        self.tools_notice.set_text(notice)

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
        return {'tools_enabled': self.tools_available and self.tools_check.get_active(), 'tools_text': self.tools_text}

    def _format_changed(self, *args):
        schema_mode = self.output_dropdown.get_selected() == 2
        self.schema_button.set_visible(schema_mode)
        if (args and schema_mode and not self.schema_text.strip()
                and not self._restoring_options and self.get_mapped()):
            # Let the dropdown finish handling selection before presenting a dialog.
            GLib.idle_add(self._open_missing_schema)

    def _open_missing_schema(self):
        if (self.get_mapped() and self.output_dropdown.get_selected() == 2
                and not self.schema_text.strip() and self._schema_dialog is None):
            self.edit_schema()
        return False

    def _keep_alive_changed(self, *args):
        self.keep_alive_entry.set_visible(self.keep_alive_dropdown.get_selected() == 5)

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
        mode = self.output_modes[self.output_dropdown.get_selected()]
        output_format = request_format(mode, self.schema_text)
        keep_alive = self.keep_alive_values[self.keep_alive_dropdown.get_selected()]
        if keep_alive == 'custom':
            try:
                keep_alive = int(self.keep_alive_entry.get_text())
            except ValueError as exc:
                raise ValueError(_('Keep-alive must be a positive number of seconds.')) from exc
            if keep_alive <= 0:
                raise ValueError(_('Keep-alive must be a positive number of seconds.'))
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
        def add_option(entry: Gtk.Entry, key: str, type_func: Callable[[str], Any]) -> None:
            text = entry.get_text().strip()
            if text:
                try:
                    val = type_func(text)
                    options[key] = val
                except ValueError:
                    raise ValueError(_("Invalid value for {0}.").format(key))
                if isinstance(val, float) and not math.isfinite(val):
                    raise ValueError(_("Invalid value for {0}.").format(key))
        
        add_option(self.seed_entry, 'seed', int)
        add_option(self.temperature_entry, 'temperature', float)
        add_option(self.top_k_entry, 'top_k', int)
        add_option(self.top_p_entry, 'top_p', float)
        add_option(self.min_p_entry, 'min_p', float)
        add_option(self.num_ctx_entry, 'num_ctx', int)
        add_option(self.num_predict_entry, 'num_predict', int)
        
        stop_text = self.stop_entry.get_text().strip()
        if stop_text:
            stops = [s.strip() for s in stop_text.split(',') if s.strip()]
            if stops:
                options['stop'] = stops
        
        for key in ('temperature', 'top_k'):
            if options.get(key, 0) < 0:
                raise ValueError(_("{0} must not be negative.").format(key))
        for key in ('top_p', 'min_p'):
            if key in options and not 0 <= options[key] <= 1:
                raise ValueError(_("{0} must be between 0 and 1.").format(key))
        if 'num_ctx' in options and options['num_ctx'] <= 0:
            raise ValueError(_("Context size must be positive."))
        if 'num_predict' in options and options['num_predict'] < -2:
            raise ValueError(_("Max tokens must be -2, -1, or a nonnegative integer."))
        return options

    def load_options(self, options: Dict[str, Any]) -> None:
        """Populates UI options from a dict."""
        mode = options.get('output_mode', 'text')
        self._restoring_options = True
        self.tools_text = options.get('tools_text', '')
        self.tools_check.set_active(options.get('tools_enabled', False))
        self.update_tools_notice()
        self.schema_text = options.get('schema_text', '')
        self.output_dropdown.set_selected(self.output_modes.index(mode) if mode in self.output_modes else 0)
        self._restoring_options = False
        keep_alive = options.get('keep_alive')
        if keep_alive in self.keep_alive_values:
            self.keep_alive_dropdown.set_selected(self.keep_alive_values.index(keep_alive))
        else:
            self.keep_alive_dropdown.set_selected(5)
            self.keep_alive_entry.set_text(str(keep_alive))
        self.stats_check.set_active(options.get('show_stats', True))
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
            
        if 'logprobs' in options:
            self.logprobs_check.set_active(options['logprobs'])
            
        if 'top_logprobs' in options and options['top_logprobs'] is not None:
            self.top_logprobs_entry.set_text(str(options['top_logprobs']))
