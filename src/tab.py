from gi.repository import Adw, Gtk, Gio, GLib, Gdk, GObject
import threading
import json
import base64
import os
from . import ollama
from .storage import ChatStorage
from .storage import ChatStorage
from .bubbles import UserBubble, AiBubble

class GenerationStrategy:
    def process(self, tab, **kwargs):
        return ollama.generate(
            host=kwargs['host'],
            model=kwargs['model'],
            prompt=kwargs['prompt'],
            system=kwargs['system'],
            options=kwargs['options'],
            thinking=kwargs['thinking'],
            logprobs=kwargs['logprobs'],
            top_logprobs=kwargs['top_logprobs'],
            images=kwargs['images']
        )
    
    def on_response_complete(self, tab, model_name):
        pass

class ChatStrategy:
    def __init__(self, storage, chat_id=None, initial_history=None):
        self.history = initial_history if initial_history else []
        self.current_response_full_text = ""
        self.chat_id = chat_id
        self.storage = storage
        self.current_thinking_full_text = ""

    def append_thinking(self, text):
        self.current_thinking_full_text += text

    def append_response_chunk(self, text):
        self.current_response_full_text += text

    def on_response_complete(self, tab, model_name):
        msg = {
            "role": "assistant", 
            "content": self.current_response_full_text,
            "model": model_name
        }
        if self.current_thinking_full_text:
            msg["thinking_content"] = self.current_thinking_full_text
            
        self.history.append(msg)
        
        if self.chat_id:
            # We need to capture the current state.
            # Ideally this is passed in, but we can rely on what was passed to process() 
            # if we stored it, OR validly assumed the tab is largely unchanged.
            # But process() stores state on self now.
            
            options = getattr(self, 'current_options', None) or {}
            system = getattr(self, 'current_system', None)
            
            # Save "thinking" setting if present
            thinking_val = getattr(self, 'current_thinking_val', None)
            if thinking_val is not None:
                options['thinking_val'] = thinking_val
                
            # Save "logprobs" setting if present
            logprobs_val = getattr(self, 'current_logprobs', None)
            if logprobs_val is not None:
                options['logprobs'] = logprobs_val
                
            top_logprobs_val = getattr(self, 'current_top_logprobs', None)
            if top_logprobs_val is not None:
                options['top_logprobs'] = top_logprobs_val
            
            self.storage.save_chat(self.chat_id, self.history, model=model_name, options=options, system=system)
            
            def update_ui():
                # Update tab title if still generic
                chat_data = self.storage.get_chat(self.chat_id)
                if chat_data and tab.tab_label:
                    new_title = chat_data.get('title', 'Chat')
                    tab.tab_label.set_label(new_title)
                    tab.emit("chat-updated", self.chat_id, new_title)
                return False
                
            GLib.idle_add(update_ui)
            
    def process(self, tab, **kwargs):
        prompt = kwargs['prompt']
        system = kwargs['system']
        
        # Store for saving later
        self.current_options = kwargs.get('options')
        self.current_system = system
        self.current_thinking_val = kwargs.get('thinking')
        self.current_logprobs = kwargs.get('logprobs')
        self.current_top_logprobs = kwargs.get('top_logprobs')
        
        # Build messages
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
            
        messages.extend(self.history)
        messages.append({"role": "user", "content": prompt})
        
        # Update our history with the user's message now
        msg = {"role": "user", "content": prompt}
        if kwargs.get('images'):
             msg['images'] = kwargs['images']
        self.history.append(msg)
        
        # Reset current response accumulator
        self.current_response_full_text = ""
        self.current_thinking_full_text = ""

        return ollama.chat(
            host=kwargs['host'],
            model=kwargs['model'],
            messages=messages,
            options=kwargs['options'],
            thinking=kwargs['thinking'],
            logprobs=kwargs['logprobs'],
            top_logprobs=kwargs['top_logprobs'],
            images=kwargs['images']
        )


@Gtk.Template(resource_path='/io/github/jackrabbithanna/Gnollama/tab.ui')
class GenerationTab(Gtk.Box):
    __gtype_name__ = 'GenerationTab'
    
    __gsignals__ = {
        'chat-updated': (GObject.SignalFlags.RUN_FIRST, None, (str, str)),
    }

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
    
    # Image Attachment
    attach_button = Gtk.Template.Child()
    image_label = Gtk.Template.Child()
    clear_image_button = Gtk.Template.Child()
    
    # Advanced Options
    seed_entry = Gtk.Template.Child()
    temperature_entry = Gtk.Template.Child()
    top_k_entry = Gtk.Template.Child()
    top_p_entry = Gtk.Template.Child()
    min_p_entry = Gtk.Template.Child()
    num_ctx_entry = Gtk.Template.Child()
    num_predict_entry = Gtk.Template.Child()
    stop_entry = Gtk.Template.Child()

    def __init__(self, tab_label=None, mode='generate', chat_id=None, initial_history=None, storage=None, **kwargs):
        super().__init__(**kwargs)
        self.tab_label = tab_label
        self.settings = Gio.Settings.new('io.github.jackrabbithanna.Gnollama')
        
        if mode == 'chat':
            # Ensure storage is provided for chat mode
            if not storage:
                storage = ChatStorage()
            self.strategy = ChatStrategy(storage, chat_id=chat_id, initial_history=initial_history)
        else:
            self.strategy = GenerationStrategy()
        
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
        
        # Connect image attachment
        self.attach_button.connect('clicked', self.on_attach_clicked)
        self.clear_image_button.connect('clicked', self.on_clear_image_clicked)
        
        self.selected_image_paths = []
        
        # Initialize host entry
        default_host = self.settings.get_string("ollama-host")
        if not default_host:
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
        
        self.active_logprobs_label = None
        self.current_ai_bubble = None
        
        # Initial model fetch
        self.start_model_fetch_thread()
        
        # Load initial history logic
        # Load initial history logic
        if mode == 'chat':
            if chat_id:
                # Load settings from storage
                chat_data = storage.get_chat(chat_id)
                if chat_data:
                    self.load_chat_settings(chat_data)
            
            if initial_history:
                 self.load_initial_history(initial_history)

    def on_attach_clicked(self, widget):
        file_chooser = Gtk.FileChooserNative.new(
            _("Open Image"),
            self.get_native(),
            Gtk.FileChooserAction.OPEN,
            _("Open"),
            _("Cancel")
        )
        
        filter_image = Gtk.FileFilter()
        filter_image.set_name("Images")
        filter_image.add_mime_type("image/png")
        filter_image.add_mime_type("image/jpeg")
        filter_image.add_mime_type("image/webp")
        file_chooser.add_filter(filter_image)
        
        file_chooser.set_select_multiple(True)
        file_chooser.connect("response", self.on_file_chooser_response)
        file_chooser.show()

    def on_file_chooser_response(self, dialog, response):
        if response == Gtk.ResponseType.ACCEPT:
            files = dialog.get_files()
            self.selected_image_paths = [f.get_path() for f in files]
            
            count = len(self.selected_image_paths)
            if count == 1:
                label_text = os.path.basename(self.selected_image_paths[0])
            else:
                label_text = _("{count} images selected").format(count=count)
            
            self.image_label.set_text(label_text)
            self.image_label.remove_css_class("dim-label")
            self.clear_image_button.set_visible(True)
        dialog.destroy()

    def on_clear_image_clicked(self, widget):
        self.selected_image_paths = []
        self.image_label.set_text(_("No image selected"))
        self.image_label.add_css_class("dim-label")
        self.clear_image_button.set_visible(False)

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
            options = [_("None"), _("Low"), _("Medium"), _("High")]
        else:
            options = [_("Thinking"), _("No thinking")]
            
        string_list = Gtk.StringList.new(options)
        self.thinking_dropdown.set_model(string_list)
        
        # Set default
        if model_name.startswith("gpt-oss"):
             self.thinking_dropdown.set_selected(0) # None
        else:
             self.thinking_dropdown.set_selected(1) # No thinking (default false)
             
        # Restore pending if any
        if hasattr(self, 'pending_thinking_val'):
            self.apply_pending_thinking()
             
    def apply_pending_thinking(self):
        if not hasattr(self, 'pending_thinking_val'):
            return
            
        # Check if we are waiting for a specific model
        if hasattr(self, 'pending_model_selection') and self.pending_model_selection:
             current_model_name = ""
             item = self.model_dropdown.get_selected_item()
             if item:
                  current_model_name = item.get_string()
             
             # If current model is not the one we want, and the one we want IS in the list (or we haven't checked yet), 
             # we should wait. 
             # Simpler: If pending_model_selection is set, ONLY apply if current matches.
             if current_model_name != self.pending_model_selection:
                  return
            
        val = self.pending_thinking_val
        dropdown_model = self.thinking_dropdown.get_model()
        if dropdown_model:
             n_items = dropdown_model.get_n_items()
             for i in range(n_items):
                 item_str = dropdown_model.get_string(i)
                 match = False
                 if val is True and item_str == _("Thinking"): match = True
                 elif val is False and item_str == _("No thinking"): match = True
                 elif val == "low" and item_str == _("Low"): match = True
                 elif val == "medium" and item_str == _("Medium"): match = True
                 elif val == "high" and item_str == _("High"): match = True
                 elif val is None and item_str == _("None"): match = True
                 
                 if match:
                     self.thinking_dropdown.set_selected(i)
                     del self.pending_thinking_val
                     break

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
            
        # Apply pending model selection if any
        if hasattr(self, 'pending_model_selection') and self.pending_model_selection:
            for i, m in enumerate(models):
                if m == self.pending_model_selection:
                    self.model_dropdown.set_selected(i)
                    self.pending_model_selection = None
                    break

    def on_send_clicked(self, widget):
        prompt = self.entry.get_text()
        if not prompt:
            return

        # Update tab label if available
        if self.tab_label:
            truncated = prompt[:20] + "..." if len(prompt) > 20 else prompt
            self.tab_label.set_label(truncated)

        # Get selected model
        selected_item = self.model_dropdown.get_selected_item()
        model_name = "llama3" # Fallback
        if selected_item:
            model_name = selected_item.get_string()

        # Handle image
        images = []
        if self.selected_image_paths:
            try:
                for path in self.selected_image_paths:
                    with open(path, "rb") as image_file:
                        encoded_string = base64.b64encode(image_file.read()).decode('utf-8')
                        images.append(encoded_string)
            except Exception as e:
                self.add_message(_("Error loading images: {e}").format(e=e), "System")
                return
            
            # Clear image after reading
            self.on_clear_image_clicked(None)

        self.add_message(prompt, sender=_("You"), images=images)
        self.entry.set_text("")
        
        thread = threading.Thread(target=self.process_request, args=(prompt, model_name, images))
        thread.daemon = True
        thread.start()

    def add_message(self, text, sender="System", images=None):
        if sender == _("You"):
            bubble = UserBubble(text, images=images)
            self.chat_box.append(bubble)
        else:
            # System message or error
            # For now use a simple label row or AiBubble with no model
            # Let's use a persistent Label for system messages
            row = Gtk.ListBoxRow()
            row.set_selectable(False)
            row.set_activatable(False)
            label = Gtk.Label(label=text)
            label.set_wrap(True)
            label.set_xalign(0)
            label.set_margin_top(6)
            label.set_margin_bottom(6)
            label.set_margin_start(12)
            label.set_margin_end(12)
            label.add_css_class("dim-label")
            row.set_child(label)
            self.chat_box.append(row)

    def start_new_response_block(self, model_name):
        self.current_ai_bubble = AiBubble(model_name=model_name)
        self.chat_box.append(self.current_ai_bubble)

    def append_response_chunk(self, text):
        if self.current_ai_bubble:
            self.current_ai_bubble.append_text(text)
            
            # Notify strategy for accumulation if needed
            if hasattr(self.strategy, 'append_response_chunk'):
                self.strategy.append_response_chunk(text)

    def reset_thinking_state(self):
        self.active_logprobs_label = None
        self.current_ai_bubble = None

    def append_thinking(self, text):
        if self.current_ai_bubble:
            self.current_ai_bubble.append_thinking(text)
        
        if hasattr(self.strategy, 'append_thinking'):
            self.strategy.append_thinking(text)

    def append_logprobs(self, logprobs_data):
        if self.active_logprobs_label is None:
            # Create expander for logprobs
            expander = Gtk.Expander(label=_("Logprobs"))
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

        label.add_css_class("dim-label")
        self.chat_box.append(label)

    def load_initial_history(self, history):
        for msg in history:
            role = msg.get('role')
            content = msg.get('content')
            thinking_content = msg.get('thinking_content')
            
            if role == 'user':
                images = msg.get('images')
                self.add_message(content, sender=_("You"), images=images)
            elif role == 'assistant':
                # Reconstruct with AiBubble
                model_name = msg.get('model', 'Assistant')
                bubble = AiBubble(model_name=model_name)
                if thinking_content:
                    bubble.append_thinking(thinking_content)
                bubble.append_text(content)
                self.chat_box.append(bubble)



    def get_options_from_ui(self):
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
                
        # Helper options
        options['stats'] = self.stats_check.get_active()
        
        return options

    def load_chat_settings(self, chat_data):
        # Model
        model = chat_data.get('model')
        if model:
            # We can't easily select it if the dropdown isn't populated yet,
            # but we can try setting the active item by string if available, 
            # or just rely on the fetch thread to eventually sync.
            # Actually, `fetch_models` updates the dropdown. We can hook into that or just set a "pending" selection.
            # For now, let's just wait for the thread. But maybe we can bias the selection.
            # Simplest hack: Store it and apply it when model list loads
            self.pending_model_selection = model

        # System Prompt
        system = chat_data.get('system')
        if system is not None:
            self.system_prompt_entry.set_text(system)

        # Options
        options = chat_data.get('options', {})
        if options:
            if 'thinking_val' in options:
                # Restore thinking selection
                val = options['thinking_val']
                self.pending_thinking_val = val
                
                # Try to apply immediately if model populated
                self.apply_pending_thinking()
            
            if 'seed' in options: self.seed_entry.set_text(str(options['seed']))
            if 'temperature' in options: self.temperature_entry.set_text(str(options['temperature']))
            if 'top_k' in options: self.top_k_entry.set_text(str(options['top_k']))
            if 'top_p' in options: self.top_p_entry.set_text(str(options['top_p']))
            if 'min_p' in options: self.min_p_entry.set_text(str(options['min_p']))
            if 'num_ctx' in options: self.num_ctx_entry.set_text(str(options['num_ctx']))
            if 'num_predict' in options: self.num_predict_entry.set_text(str(options['num_predict']))
            if 'stop' in options: self.stop_entry.set_text(", ".join(options['stop']))
            
            # Stats check
            if 'stats' in options:
                self.stats_check.set_active(options['stats'])

            # Logprobs
            if 'logprobs' in options:
                self.logprobs_check.set_active(options['logprobs'])
            if 'top_logprobs' in options and options['top_logprobs'] is not None:
                self.top_logprobs_entry.set_text(str(options['top_logprobs']))

    def process_request(self, prompt, model_name, images=None):
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

        options = self.get_options_from_ui()

        GLib.idle_add(self.start_new_response_block, model_name)

        try:
            for json_obj in self.strategy.process(
                self,
                host=host,
                model=model_name,
                prompt=prompt,
                system=system_prompt if system_prompt else None,
                options=options if options else None,
                thinking=thinking_val,
                logprobs=logprobs,
                top_logprobs=top_logprobs,
                images=images
            ):
                if "error" in json_obj:
                    raise Exception(json_obj["error"])
                
                # Handle message response (for chat endpoint)
                if 'message' in json_obj:
                    content = json_obj['message'].get('content', '')
                    if content:
                         GLib.idle_add(self.append_response_chunk, content)
                # Handle standard response (for generate endpoint)
                elif 'response' in json_obj:
                    content = json_obj.get('response', '')
                    if content:
                        GLib.idle_add(self.append_response_chunk, content)

                # Handle thinking
                thinking_fragment = json_obj.get('thinking', '')
                 # Check nested thinking in message (common in chat endpoint)
                if not thinking_fragment and 'message' in json_obj:
                    thinking_fragment = json_obj['message'].get('thinking', '')

                if thinking_fragment:
                     GLib.idle_add(self.append_thinking, thinking_fragment)
                        
                # Handle logprobs
                if "logprobs" in json_obj and json_obj["logprobs"]:
                     GLib.idle_add(self.append_logprobs, json_obj["logprobs"])

                if json_obj.get('done'):
                    if self.stats_check.get_active():
                        GLib.idle_add(self.show_stats, json_obj)
                    
                    if hasattr(self.strategy, 'on_response_complete'):
                         self.strategy.on_response_complete(self, model_name)
                        
        except Exception as e:
            GLib.idle_add(self.add_message, f"\nError: {str(e)}\n", "System")
