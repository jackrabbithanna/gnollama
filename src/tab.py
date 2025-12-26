from gi.repository import Adw, Gtk, Gio, GLib, Gdk
import threading
import json
import os
from . import ollama
from .utils import parse_markdown

@Gtk.Template(resource_path='/com/github/jackrabbithanna/Gnollama/tab.ui')
class GenerationTab(Gtk.Box):
    __gtype_name__ = 'GenerationTab'

    entry = Gtk.Template.Child()
    chat_box = Gtk.Template.Child()
    send_button = Gtk.Template.Child()
    model_dropdown = Gtk.Template.Child()
    thinking_dropdown = Gtk.Template.Child()
    system_prompt_entry = Gtk.Template.Child()
    stats_check = Gtk.Template.Child()
    logprobs_check = Gtk.Template.Child()
    top_logprobs_entry = Gtk.Template.Child()
    host_entry = Gtk.Template.Child()
    
    # Advanced Options
    seed_entry = Gtk.Template.Child()
    temperature_entry = Gtk.Template.Child()
    top_k_entry = Gtk.Template.Child()
    top_p_entry = Gtk.Template.Child()
    min_p_entry = Gtk.Template.Child()
    num_ctx_entry = Gtk.Template.Child()
    num_predict_entry = Gtk.Template.Child()
    stop_entry = Gtk.Template.Child()

    def __init__(self, tab_label=None, **kwargs):
        super().__init__(**kwargs)
        self.tab_label = tab_label
        
        self.send_button.connect('clicked', self.on_send_clicked)
        self.entry.connect('activate', self.on_send_clicked)
        self.system_prompt_entry.connect('activate', self.on_send_clicked)
        self.top_logprobs_entry.connect('activate', self.on_send_clicked)
        
        # Connect advanced options to send
        self.seed_entry.connect('activate', self.on_send_clicked)
        self.temperature_entry.connect('activate', self.on_send_clicked)
        self.top_k_entry.connect('activate', self.on_send_clicked)
        self.top_p_entry.connect('activate', self.on_send_clicked)
        self.min_p_entry.connect('activate', self.on_send_clicked)
        self.num_ctx_entry.connect('activate', self.on_send_clicked)
        self.num_predict_entry.connect('activate', self.on_send_clicked)
        self.stop_entry.connect('activate', self.on_send_clicked)
        
        # Initialize host entry
        default_host = "http://localhost:11434"
        self.host_entry.set_text(default_host)
        
        # Connect host entry changed signal
        self.host_entry.connect('changed', self.on_host_changed)
        self.host_update_source_id = None
        
        # Refresh models on dropdown click
        click_controller = Gtk.GestureClick()
        click_controller.connect("pressed", self.on_dropdown_clicked)
        self.model_dropdown.add_controller(click_controller)
        
        self.model_dropdown.connect('notify::selected-item', self.on_model_changed)
        
        self.active_thinking_label = None
        self.active_logprobs_label = None
        self.current_response_label = None
        self.current_response_raw_text = ""
        
        # Initial model fetch
        self.start_model_fetch_thread()

    def on_host_changed(self, widget):
        # Debounce
        if self.host_update_source_id:
            GLib.source_remove(self.host_update_source_id)
        self.host_update_source_id = GLib.timeout_add(500, self.on_host_update_timeout)

    def on_host_update_timeout(self):
        self.host_update_source_id = None
        self.start_model_fetch_thread()
        return False

    def on_dropdown_clicked(self, gesture, n_press, x, y):
        self.start_model_fetch_thread()
        
    def on_model_changed(self, *args):
        selected_item = self.model_dropdown.get_selected_item()
        if selected_item:
            model_name = selected_item.get_string()
            self.update_thinking_options(model_name)

    def update_thinking_options(self, model_name):
        if model_name.startswith("gpt-oss"):
            options = ["None", "Low", "Medium", "High"]
        else:
            options = ["Thinking", "No thinking"]
            
        string_list = Gtk.StringList.new(options)
        self.thinking_dropdown.set_model(string_list)
        
        # Set default
        if model_name.startswith("gpt-oss"):
             self.thinking_dropdown.set_selected(0) # None
        else:
             self.thinking_dropdown.set_selected(1) # No thinking (default false)

    def start_model_fetch_thread(self):
        thread = threading.Thread(target=self.fetch_models)
        thread.daemon = True
        thread.start()

    def fetch_models(self):
        host = self.host_entry.get_text()
        if not host:
            return

        models = ollama.fetch_models(host)
        if models:
            GLib.idle_add(self.update_model_dropdown, models)

    def update_model_dropdown(self, models):
        # Check if models have changed to avoid unnecessary updates
        current_model = self.model_dropdown.get_model()
        if current_model:
            current_items = [current_model.get_string(i) for i in range(current_model.get_n_items())]
            if current_items == models:
                return

        string_list = Gtk.StringList.new(models)
        self.model_dropdown.set_model(string_list)
        # Select first model by default if available and nothing selected
        if models and self.model_dropdown.get_selected() == Gtk.INVALID_LIST_POSITION:
            self.model_dropdown.set_selected(0)
            # Trigger thinking update for initial selection
            self.update_thinking_options(models[0])

    def on_send_clicked(self, widget):
        prompt = self.entry.get_text()
        if not prompt:
            return

        # Update tab label if available
        if self.tab_label:
            truncated = prompt[:20] + "..." if len(prompt) > 20 else prompt
            self.tab_label.set_label(truncated)

        self.add_message(prompt, sender="You")
        self.entry.set_text("")
        
        # Get selected model
        selected_item = self.model_dropdown.get_selected_item()
        model_name = "llama3" # Fallback
        if selected_item:
            model_name = selected_item.get_string()

        thread = threading.Thread(target=self.send_prompt_to_ollama, args=(prompt, model_name))
        thread.daemon = True
        thread.start()



    def add_message(self, text, sender="System"):
        # Container box for alignment and styling
        container = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        
        # Bubble box for background
        bubble = Gtk.Box()
        
        label = Gtk.Label()
        label.set_wrap(True)
        label.set_max_width_chars(50) # Limit width for better readability
        label.set_xalign(0)
        label.set_selectable(True)
        
        # Parse markdown for display
        markup = parse_markdown(text)
        label.set_markup(markup)
        
        bubble.append(label)
        container.append(bubble)

        if sender == "You":
            container.set_halign(Gtk.Align.END)
            bubble.add_css_class("user-bubble")
        else:
            container.set_halign(Gtk.Align.START)
            bubble.add_css_class("bot-bubble")
            
        self.chat_box.append(container)

    def start_new_response_block(self, model_name):
        self.current_response_raw_text = ""
        
        # Add header
        header = Gtk.Label(label=f"Ollama ({model_name}):")
        header.set_xalign(0)
        header.set_halign(Gtk.Align.START)
        header.add_css_class("dim-label")
        self.chat_box.append(header)
        
        # Container for response bubble
        container = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        container.set_halign(Gtk.Align.START)
        container.set_hexpand(True)
        
        # Bubble box
        bubble = Gtk.Box()
        bubble.add_css_class("bot-bubble")
        bubble.set_hexpand(True) # Allow bubble to expand to fill width if needed, or just wrap
        
        # Create label for response content
        self.current_response_label = Gtk.Label()
        self.current_response_label.set_wrap(True)
        self.current_response_label.set_xalign(0)
        self.current_response_label.set_selectable(True)
        self.current_response_label.set_halign(Gtk.Align.FILL)
        self.current_response_label.set_hexpand(True)
        
        bubble.append(self.current_response_label)
        container.append(bubble)
        self.chat_box.append(container)

    def append_response_chunk(self, text):
        if self.current_response_label:
            self.current_response_raw_text += text
            markup = parse_markdown(self.current_response_raw_text)
            self.current_response_label.set_markup(markup)

    def reset_thinking_state(self):
        self.active_thinking_label = None
        self.active_logprobs_label = None
        self.current_response_label = None
        self.current_response_raw_text = ""

    def append_thinking(self, text):
        if self.active_thinking_label is None:
            # Create expander and inner label
            expander = Gtk.Expander(label="See thinking")
            expander.set_hexpand(True)
            expander.set_halign(Gtk.Align.FILL)
            
            inner_label = Gtk.Label()
            inner_label.set_wrap(True)
            inner_label.set_xalign(0)
            inner_label.set_selectable(True)
            inner_label.set_hexpand(True)
            inner_label.set_halign(Gtk.Align.FILL)
            inner_label.add_css_class("thinking-text")
            
            expander.set_child(inner_label)
            
            last_child = self.chat_box.get_last_child()
            if last_child and self.current_response_label:
                 self.chat_box.insert_child_after(expander, last_child.get_prev_sibling())
            else:
                 self.chat_box.append(expander)
            
            self.active_thinking_label = inner_label

        current_text = self.active_thinking_label.get_label()
        self.active_thinking_label.set_label(current_text + text)

    def append_logprobs(self, logprobs_data):
        if self.active_logprobs_label is None:
            # Create expander for logprobs
            expander = Gtk.Expander(label="Logprobs")
            expander.set_hexpand(True)
            expander.set_halign(Gtk.Align.FILL)
            
            # Use ScrolledWindow + TextView for performance with large data
            scrolled = Gtk.ScrolledWindow()
            scrolled.set_min_content_height(150)
            scrolled.set_propagate_natural_height(True)
            
            text_view = Gtk.TextView()
            text_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
            text_view.set_editable(False)
            text_view.set_monospace(True)
            text_view.set_bottom_margin(6)
            text_view.set_top_margin(6)
            text_view.set_left_margin(6)
            text_view.set_right_margin(6)
            
            scrolled.set_child(text_view)
            expander.set_child(scrolled)
            self.chat_box.append(expander)
            
            self.active_logprobs_label = text_view # Reusing variable name for TextView

        buffer = self.active_logprobs_label.get_buffer()
        end_iter = buffer.get_end_iter()
        
        # Format logprobs data compactly
        text_chunk = ""
        if isinstance(logprobs_data, list):
            for item in logprobs_data:
                if isinstance(item, dict):
                    token = item.get('token', '')
                    logprob = item.get('logprob', 0.0)
                    text_chunk += f"Token: {repr(token):<15} Logprob: {logprob:.4f}\n"
                else:
                    text_chunk += str(item) + "\n"
        else:
             text_chunk = json.dumps(logprobs_data) + "\n"
             
        buffer.insert(end_iter, text_chunk)

    def show_stats(self, stats):
        # Format stats
        total_duration = stats.get('total_duration', 0) / 1e9
        load_duration = stats.get('load_duration', 0) / 1e9
        prompt_eval_count = stats.get('prompt_eval_count', 0)
        prompt_eval_duration = stats.get('prompt_eval_duration', 0) / 1e9
        eval_count = stats.get('eval_count', 0)
        eval_duration = stats.get('eval_duration', 0) / 1e9
        
        stats_text = (
            f"Total: {total_duration:.2f}s | Load: {load_duration:.2f}s | "
            f"Prompt: {prompt_eval_count} tokens ({prompt_eval_duration:.2f}s) | "
            f"Eval: {eval_count} tokens ({eval_duration:.2f}s)"
        )
        
        label = Gtk.Label(label=stats_text)
        label.set_xalign(0)
        label.set_halign(Gtk.Align.START)
        label.add_css_class("dim-label")
        self.chat_box.append(label)

    def send_prompt_to_ollama(self, prompt, model_name):
        host = self.host_entry.get_text()
        
        # Get thinking parameter
        thinking_item = self.thinking_dropdown.get_selected_item()
        thinking_val = None 
        if thinking_item:
            thinking_str = thinking_item.get_string()
            if thinking_str == "Thinking":
                thinking_val = True
            elif thinking_str == "No thinking":
                thinking_val = False
            elif thinking_str == "Low":
                thinking_val = "low"
            elif thinking_str == "Medium":
                thinking_val = "medium"
            elif thinking_str == "High":
                thinking_val = "high"
            elif thinking_str == "None":
                thinking_val = None

        system_prompt = self.system_prompt_entry.get_text().strip()
        
        logprobs = False
        top_logprobs = None
        if self.logprobs_check.get_active():
            logprobs = True
            top_logprobs_text = self.top_logprobs_entry.get_text().strip()
            if top_logprobs_text.isdigit():
                top_logprobs = int(top_logprobs_text)

        options = {}
        def add_option(entry, key, type_func):
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

        GLib.idle_add(self.reset_thinking_state)
        GLib.idle_add(self.start_new_response_block, model_name)
        
        try:
            for json_obj in ollama.generate(
                host=host,
                model=model_name,
                prompt=prompt,
                system=system_prompt if system_prompt else None,
                options=options if options else None,
                thinking=thinking_val,
                logprobs=logprobs,
                top_logprobs=top_logprobs
            ):
                if "error" in json_obj:
                    raise Exception(json_obj["error"])

                # Handle thinking
                thinking_fragment = json_obj.get('thinking', '')
                if thinking_fragment and thinking_val is not False and thinking_val is not None:
                     GLib.idle_add(self.append_thinking, thinking_fragment)
                     
                # Handle response
                response_fragment = json_obj.get('response', '')
                if response_fragment:
                    GLib.idle_add(self.append_response_chunk, response_fragment)
                    
                # Handle logprobs
                if "logprobs" in json_obj and json_obj["logprobs"]:
                     GLib.idle_add(self.append_logprobs, json_obj["logprobs"])

                if json_obj.get('done'):
                    if self.stats_check.get_active():
                        GLib.idle_add(self.show_stats, json_obj)
                        
        except Exception as e:
            GLib.idle_add(self.add_message, f"\nError: {str(e)}\n", "System")
