from typing import Dict, Any, Callable, List, Optional
from gi.repository import Gtk, GObject
from ..storage import ChatStorage

@Gtk.Template(resource_path='/io/github/jackrabbithanna/Gnollama/widgets/options_panel.ui')
class OptionsPanel(Gtk.Expander):
    """Encapsulates the advanced settings and options for the chat."""
    __gtype_name__ = 'OptionsPanel'

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

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.storage = None
        self.host_list: List[Dict[str, Any]] = []

    def update_hosts(self) -> None:
        """Reloads the host list from storage and updates the dropdown."""
        if not self.storage:
            return
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
                    pass
        
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
        
        return options

    def load_options(self, options: Dict[str, Any]) -> None:
        """Populates UI options from a dict."""
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
