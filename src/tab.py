from typing import List, Optional, Any, Dict, Union, Callable
from gi.repository import Adw, Gtk, Gio, GLib, Gdk, GObject
import threading
import json
import base64
import os
import time
from . import ollama
from .storage import ChatStorage
from .bubbles import UserBubble, AiBubble

class GenerationStrategy:
    """Strategy for single-turn text generation."""
    def process(self, tab: Any, **kwargs: Any) -> Any:
        """Executes the generation process via Ollama API."""
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
    
    def on_response_complete(self, tab: Any, model_name: str) -> None:
        """Callback when generation is complete."""
        pass

class ChatStrategy:
    """Strategy for multi-turn chat sessions with history persistence."""
    def __init__(self, storage: ChatStorage, chat_id: Optional[str] = None, initial_history: Optional[List[Dict[str, Any]]] = None) -> None:
        self.history: List[Dict[str, Any]] = initial_history if initial_history else []
        self.current_response_full_text: str = ""
        self.chat_id: Optional[str] = chat_id
        self.storage: ChatStorage = storage
        self.current_thinking_full_text: str = ""

    def append_thinking(self, text: str) -> None:
        """Accumulates thinking content for the current turn."""
        self.current_thinking_full_text += text

    def append_response_chunk(self, text: str) -> None:
        """Accumulates response content for the current turn."""
        self.current_response_full_text += text

    def on_response_complete(self, tab: Any, model_name: str) -> None:
        """Saves the completed turn to storage and updates UI."""
        msg = {
            "role": "assistant", 
            "content": self.current_response_full_text,
            "model": model_name
        }
        if self.current_thinking_full_text:
            msg["thinking_content"] = self.current_thinking_full_text
            
        if hasattr(self, 'current_api_params'):
            msg["api_details"] = getattr(self, 'current_api_params')
            
        self.history.append(msg)
        
        if self.chat_id:
            options = getattr(self, 'current_options', None) or {}
            system = getattr(self, 'current_system', None)
            
            thinking_val = getattr(self, 'current_thinking_val', None)
            if thinking_val is not None:
                options['thinking_val'] = thinking_val
                
            logprobs_val = getattr(self, 'current_logprobs', None)
            if logprobs_val is not None:
                options['logprobs'] = logprobs_val
                
            top_logprobs_val = getattr(self, 'current_top_logprobs', None)
            if top_logprobs_val is not None:
                options['top_logprobs'] = top_logprobs_val
            
            host = getattr(self, 'current_host', None)
            
            self.storage.save_chat(self.chat_id, self.history, model=model_name, options=options, system=system, host=host)
            
            def update_ui() -> bool:
                chat_data = self.storage.get_chat(self.chat_id)
                if chat_data and tab.tab_label:
                    new_title = chat_data.get('title', 'Chat')
                    tab.tab_label.set_label(new_title)
                    tab.emit("chat-updated", self.chat_id, new_title)
                return False
                
            GLib.idle_add(update_ui)
            
    def process(self, tab: Any, **kwargs: Any) -> Any:
        """Executes the chat process via Ollama API."""
        prompt = kwargs['prompt']
        system = kwargs['system']
        
        self.current_options = kwargs.get('options')
        self.current_system = system
        self.current_thinking_val = kwargs.get('thinking')
        self.current_logprobs = kwargs.get('logprobs')
        self.current_top_logprobs = kwargs.get('top_logprobs')
        self.current_host = kwargs.get('host_id')
        
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
            
        messages.extend(self.history)
        messages.append({"role": "user", "content": prompt})
        
        msg = {"role": "user", "content": prompt}
        if kwargs.get('images'):
             msg['images'] = kwargs['images']
        self.history.append(msg)
        
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
    """The main widget for a chat or generation session."""
    __gtype_name__ = 'GenerationTab'
    
    __gsignals__ = {
        'chat-updated': (GObject.SignalFlags.RUN_FIRST, None, (str, str)),
    }

    entry: Gtk.Entry = Gtk.Template.Child()
    chat_box: Gtk.ListBox = Gtk.Template.Child()
    send_button: Gtk.Button = Gtk.Template.Child()
    model_dropdown: Gtk.DropDown = Gtk.Template.Child()
    thinking_dropdown: Gtk.DropDown = Gtk.Template.Child()
    system_prompt_entry: Gtk.Entry = Gtk.Template.Child()
    stats_check: Gtk.CheckButton = Gtk.Template.Child()
    logprobs_check: Gtk.CheckButton = Gtk.Template.Child()
    top_logprobs_entry: Gtk.Entry = Gtk.Template.Child()
    host_dropdown: Gtk.DropDown = Gtk.Template.Child()
    
    attach_button: Gtk.Button = Gtk.Template.Child()
    image_label: Gtk.Label = Gtk.Template.Child()
    clear_image_button: Gtk.Button = Gtk.Template.Child()
    
    seed_entry: Gtk.Entry = Gtk.Template.Child()
    temperature_entry: Gtk.Entry = Gtk.Template.Child()
    top_k_entry: Gtk.Entry = Gtk.Template.Child()
    top_p_entry: Gtk.Entry = Gtk.Template.Child()
    min_p_entry: Gtk.Entry = Gtk.Template.Child()
    num_ctx_entry: Gtk.Entry = Gtk.Template.Child()
    num_predict_entry: Gtk.Entry = Gtk.Template.Child()
    stop_entry: Gtk.Entry = Gtk.Template.Child()

    def __init__(self, tab_label: Optional[Gtk.Label] = None, mode: str = 'generate', chat_id: Optional[str] = None, 
                 initial_history: Optional[List[Dict[str, Any]]] = None, storage: Optional[ChatStorage] = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.init_template()
        self.tab_label = tab_label
        self.settings = Gio.Settings.new('io.github.jackrabbithanna.Gnollama')
        
        if not storage:
            storage = ChatStorage()
        self.storage = storage

        if mode == 'chat':
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
        
        # Initialize host dropdown
        self.host_list = []
        self.pending_host_id = None
        self.host_update_source_id = None
        self.host_dropdown.connect('notify::selected-item', self.on_host_changed)
        
        self.update_hosts()
        
        # Refresh models on dropdown click
        click_controller = Gtk.GestureClick()
        click_controller.connect("pressed", self.on_dropdown_clicked)
        self.model_dropdown.add_controller(click_controller)
        
        self.model_dropdown.connect('notify::selected-item', self.on_model_changed)
        
        self.active_logprobs_label = None
        self.current_ai_bubble = None
        
        # Initial model fetch
        self.start_model_fetch_thread()
        
        self.mode: str = mode
        
        # Load initial history logic
        if mode == 'chat':
            if chat_id:
                # Load settings from storage
                chat_data = storage.get_chat(chat_id)
                if chat_data:
                    self.load_chat_settings(chat_data)
            
            if initial_history:
                 self.load_initial_history(initial_history)

    def update_hosts(self) -> None:
        """Reloads the list of Ollama hosts from storage and updates the dropdown."""
        hosts = self.storage.get_all_hosts()
        
        current_host_id = None
        if hasattr(self, 'pending_host_id') and self.pending_host_id:
            current_host_id = self.pending_host_id
            self.pending_host_id = None
        elif hasattr(self, 'host_list') and self.host_list:
            selected_idx = self.host_dropdown.get_selected()
            if selected_idx != Gtk.INVALID_LIST_POSITION and selected_idx < len(self.host_list):
                current_host_id = self.host_list[selected_idx]['id']
                
        self.host_list = hosts
        
        host_names = [h['name'] for h in hosts]
        string_list = Gtk.StringList.new(host_names)
        self.host_dropdown.set_model(string_list)
        
        target_idx = 0
        found = False
        if current_host_id:
            for i, h in enumerate(hosts):
                if h['id'] == current_host_id:
                    target_idx = i
                    found = True
                    break
                    
        if not found:
            for i, h in enumerate(hosts):
                if h.get('default', False):
                    target_idx = i
                    break
                    
        self.host_dropdown.set_selected(target_idx)
        self.on_host_changed(None)

    def get_current_host(self) -> Optional[Dict[str, Any]]:
        """Returns the currently selected host dictionary."""
        selected_idx = self.host_dropdown.get_selected()
        if selected_idx != Gtk.INVALID_LIST_POSITION and hasattr(self, 'host_list') and selected_idx < len(self.host_list):
            return self.host_list[selected_idx]
        return None

    def on_attach_clicked(self, widget: Gtk.Button) -> None:
        """Opens a file chooser to attach images."""
        file_chooser = Gtk.FileChooserNative.new(
            _("Open Image"),
            self.get_native(),
            Gtk.FileChooserAction.OPEN,
            _("Open"),
            _("Cancel")
        )
        
        filter_image = Gtk.FileFilter()
        filter_image.set_name(_("Images"))
        filter_image.add_mime_type("image/png")
        filter_image.add_mime_type("image/jpeg")
        filter_image.add_mime_type("image/webp")
        file_chooser.add_filter(filter_image)
        
        file_chooser.set_select_multiple(True)
        file_chooser.connect("response", self.on_file_chooser_response)
        file_chooser.show()

    def on_file_chooser_response(self, dialog: Gtk.FileChooserNative, response: Gtk.ResponseType) -> None:
        """Handles the response from the image file chooser."""
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

    def on_clear_image_clicked(self, widget: Gtk.Button) -> None:
        """Clears the current image selection."""
        self.selected_image_paths = []
        self.image_label.set_text(_("No image selected"))
        self.image_label.add_css_class("dim-label")
        self.clear_image_button.set_visible(False)

    def on_host_changed(self, widget: Optional[GObject.Object], *args: Any) -> None:
        """Debounced callback for host selection changes."""
        if self.host_update_source_id:
            GLib.source_remove(self.host_update_source_id)
        self.host_update_source_id = GLib.timeout_add(500, self.on_host_update_timeout)

    def on_host_update_timeout(self) -> bool:
        """Helper for debounced host updates."""
        self.host_update_source_id = None
        self.start_model_fetch_thread()
        return False

    def on_dropdown_clicked(self, gesture: Gtk.GestureClick, n_press: int, x: float, y: float) -> None:
        """Triggers a model fetch when the dropdown is interacted with."""
        self.start_model_fetch_thread()
        
    def on_model_changed(self, *args: Any) -> None:
        """Callback when the selected model changes."""
        selected_item = self.model_dropdown.get_selected_item()
        if selected_item:
            model_name = selected_item.get_string()
            self.update_thinking_options(model_name)

    def update_thinking_options(self, model_name: str) -> None:
        """Updates the thinking dropdown options based on the model's capabilities."""
        if model_name.startswith("gpt-oss"):
            options = [_("None"), _("Low"), _("Medium"), _("High"), _("Max")]
        else:
            options = [_("Thinking"), _("No thinking")]
            
        string_list = Gtk.StringList.new(options)
        self.thinking_dropdown.set_model(string_list)
        
        if model_name.startswith("gpt-oss"):
             self.thinking_dropdown.set_selected(0) # None
        else:
             self.thinking_dropdown.set_selected(1) # No thinking (default false)
             
        if hasattr(self, 'pending_thinking_val'):
            self.apply_pending_thinking()
             
    def apply_pending_thinking(self) -> None:
        """Applies a previously saved thinking preference to the UI."""
        if not hasattr(self, 'pending_thinking_val'):
            return
            
        if hasattr(self, 'pending_model_selection') and self.pending_model_selection:
             current_model_name = ""
             item = self.model_dropdown.get_selected_item()
             if item:
                  current_model_name = item.get_string()
             
             if current_model_name != self.pending_model_selection:
                  return
            
        val = getattr(self, 'pending_thinking_val')
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
                 elif val == "max" and item_str == _("Max"): match = True
                 elif val is None and item_str == _("None"): match = True
                 
                 if match:
                     self.thinking_dropdown.set_selected(i)
                     delattr(self, 'pending_thinking_val')
                     break

    def start_model_fetch_thread(self) -> None:
        """Starts a background thread to fetch available models."""
        thread = threading.Thread(target=self.fetch_models, daemon=True)
        thread.start()

    def fetch_models(self) -> None:
        """Fetches models from the current host and updates the UI."""
        host = self.get_current_host()
        if not host:
            return

        models = ollama.fetch_models(host['hostname'])
        if models:
            GLib.idle_add(self.update_model_dropdown, models)

    def update_model_dropdown(self, models: List[str]) -> None:
        """Updates the model dropdown with a new list of names."""
        current_model_list = self.model_dropdown.get_model()
        if current_model_list:
            current_items = [current_model_list.get_string(i) for i in range(current_model_list.get_n_items())]
            if current_items == models:
                return

        string_list = Gtk.StringList.new(models)
        self.model_dropdown.set_model(string_list)
        if models and self.model_dropdown.get_selected() == Gtk.INVALID_LIST_POSITION:
            self.model_dropdown.set_selected(0)
            self.update_thinking_options(models[0])
            
        if hasattr(self, 'pending_model_selection') and self.pending_model_selection:
            target_model = getattr(self, 'pending_model_selection')
            for i, m in enumerate(models):
                if m == target_model:
                    self.model_dropdown.set_selected(i)
                    setattr(self, 'pending_model_selection', None)
                    break

    def on_send_clicked(self, widget: Union[Gtk.Button, Gtk.Entry]) -> None:
        """Handles the send action from either button or entry."""
        prompt = self.entry.get_text()
        if not prompt:
            return

        if self.tab_label:
            truncated = prompt[:20] + "..." if len(prompt) > 20 else prompt
            self.tab_label.set_label(truncated)

        selected_item = self.model_dropdown.get_selected_item()
        model_name = "llama3"
        if selected_item:
            model_name = selected_item.get_string()

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
            
            self.on_clear_image_clicked(None)

        self.add_message(prompt, sender=_("You"), images=images)
        self.entry.set_text("")
        
        thread = threading.Thread(target=self.process_request, args=(prompt, model_name, images), daemon=True)
        thread.start()

    def add_message(self, text: str, sender: str = "System", images: Optional[List[str]] = None) -> None:
        """Adds a message bubble to the chat box."""
        if sender == _("You"):
            bubble = UserBubble(text, images=images)
            self.chat_box.append(bubble)
        else:
            row = Gtk.ListBoxRow()
            row.set_selectable(False)
            row.set_activatable(False)
            label = Gtk.Label(label=text)
            label.set_wrap(True)
            label.set_xalign(0)
            label.add_css_class("system-message")
            row.set_child(label)
            self.chat_box.append(row)

    def start_new_response_block(self, model_name: str, api_details: Optional[Dict[str, Any]] = None) -> None:
        """Initializes a new AI response bubble."""
        self.current_ai_bubble = AiBubble(model_name=model_name)
        if api_details:
            self.current_ai_bubble.set_api_details(api_details)
        self.chat_box.append(self.current_ai_bubble)

    def append_response_chunk(self, text: str) -> None:
        """Appends a text chunk to the current AI bubble."""
        if self.current_ai_bubble:
            self.current_ai_bubble.append_text(text)
            if hasattr(self.strategy, 'append_response_chunk'):
                self.strategy.append_response_chunk(text)

    def reset_thinking_state(self) -> None:
        """Resets the state tracking for AI response generation."""
        self.active_logprobs_label = None
        self.current_ai_bubble = None

    def append_thinking(self, text: str) -> None:
        """Appends a thinking chunk to the current AI bubble."""
        if self.current_ai_bubble:
            self.current_ai_bubble.append_thinking(text)

    def process_request(self, prompt: str, model_name: str, images: Optional[List[str]] = None) -> None:
        """Main thread worker to process an Ollama request and stream the response."""
        import time
        host_record = self.get_current_host()
        if not host_record:
             GLib.idle_add(self.add_message, _("Error: No host configured."), "System")
             return
        host_str = host_record['hostname']
        host_id = host_record['id']
        
        thinking_item = self.thinking_dropdown.get_selected_item()
        thinking_val = None 
        if thinking_item:
            thinking_str = thinking_item.get_string()
            if thinking_str == _("Thinking"):
                thinking_val = True
            elif thinking_str == _("No thinking"):
                thinking_val = False
            elif thinking_str == _("Low"):
                thinking_val = "low"
            elif thinking_str == _("Medium"):
                thinking_val = "medium"
            elif thinking_str == _("High"):
                thinking_val = "high"
            elif thinking_str == _("Max"):
                thinking_val = "max"    
            elif thinking_str == _("None"):
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

        api_params = {
            "endpoint": "chat" if isinstance(self.strategy, ChatStrategy) else "generate",
            "host": host_str,
            "model": model_name,
            "options": options if options else None,
            "thinking": thinking_val,
            "logprobs": logprobs,
            "top_logprobs": top_logprobs,
        }
        
        if not isinstance(self.strategy, ChatStrategy):
            api_params["system"] = system_prompt if system_prompt else None

        if images:
             api_params["images"] = f"[{len(images)} attached]"
             
        if isinstance(self.strategy, ChatStrategy):
             messages_to_send = []
             if system_prompt:
                 messages_to_send.append({"role": "system", "content": system_prompt})
             
             for h in self.strategy.history:
                 clean_h = dict(h)
                 if "images" in clean_h and clean_h["images"]:
                     clean_h["images"] = f"[{len(clean_h['images'])} attached]"
                 messages_to_send.append(clean_h)
                 
             msg = {"role": "user", "content": prompt}
             if images:
                 msg["images"] = f"[{len(images)} attached]"
             messages_to_send.append(msg)
             api_params["messages"] = messages_to_send
        else:
             api_params["prompt"] = prompt

        self.strategy.current_api_params = api_params
        # Reset strategy buffers for the new response
        if hasattr(self.strategy, 'current_response_full_text'):
            self.strategy.current_response_full_text = ""
        if hasattr(self.strategy, 'current_thinking_full_text'):
            self.strategy.current_thinking_full_text = ""
            
        GLib.idle_add(self.start_new_response_block, model_name, api_params)

        try:
            full_response = ""
            thinking_content = ""
            is_thinking = False
            last_scroll_time = 0.0
            
            ai_bubble = None
            def get_bubble():
                nonlocal ai_bubble
                ai_bubble = self.current_ai_bubble
            GLib.idle_add(get_bubble)
            
            while ai_bubble is None:
                time.sleep(0.01)
            
            # Show API details if available
            if hasattr(self.strategy, 'current_api_params'):
                GLib.idle_add(ai_bubble.set_api_details, self.strategy.current_api_params)

            for json_obj in self.strategy.process(
                self,
                host=host_str,
                host_id=host_id,
                model=model_name,
                prompt=prompt,
                system=system_prompt if system_prompt else None,
                options=options if options else None,
                thinking=thinking_val,
                logprobs=logprobs,
                top_logprobs=top_logprobs,
                images=images
            ):
                chunk = json_obj
                
                if 'error' in chunk:
                    error_msg = chunk['error']
                    GLib.idle_add(ai_bubble.append_text, f"\n\n### Error\n\n{error_msg}")
                    break
                
                # 1. Handle native thinking field from Ollama
                native_thinking = chunk.get('thinking', chunk.get('thought', ''))
                if not native_thinking and 'message' in chunk:
                    native_thinking = chunk['message'].get('thinking', '')
                
                if native_thinking:
                    thinking_content += native_thinking
                    GLib.idle_add(ai_bubble.append_thinking, native_thinking)
                    if hasattr(self.strategy, 'append_thinking'):
                        self.strategy.append_thinking(native_thinking)
                    continue

                # 2. Handle content and potential <think> tags
                content = None
                if 'message' in chunk:
                    content = chunk['message'].get('content')
                elif 'response' in chunk:
                    content = chunk['response']
                
                if content:
                    # Robust tag parsing (handles common thinking tags)
                    think_tags = [
                        ("<think>", "</think>"),
                        ("<|channel>thought\n", "<channel|>"),
                        ("<thought>", "</thought>")
                    ]
                    
                    processed_content = content
                    for start_tag, end_tag in think_tags:
                        if start_tag in processed_content:
                            parts = processed_content.split(start_tag, 1)
                            # Text before the tag is normal content
                            if parts[0]:
                                GLib.idle_add(ai_bubble.append_text, parts[0])
                                full_response += parts[0]
                                if hasattr(self.strategy, 'append_response_chunk'):
                                    self.strategy.append_response_chunk(parts[0])
                            
                            is_thinking = True
                            processed_content = parts[1]
                            
                        if end_tag in processed_content and is_thinking:
                            parts = processed_content.split(end_tag, 1)
                            # Text before the tag is thinking content
                            if parts[0]:
                                GLib.idle_add(ai_bubble.append_thinking, parts[0])
                                thinking_content += parts[0]
                                if hasattr(self.strategy, 'append_thinking'):
                                    self.strategy.append_thinking(parts[0])
                            
                            is_thinking = False
                            processed_content = parts[1]
                    
                    # Distribute remaining content based on state
                    if processed_content:
                        if is_thinking:
                            thinking_content += processed_content
                            GLib.idle_add(ai_bubble.append_thinking, processed_content)
                            if hasattr(self.strategy, 'append_thinking'):
                                self.strategy.append_thinking(processed_content)
                        else:
                            full_response += processed_content
                            GLib.idle_add(ai_bubble.append_text, processed_content)
                            if hasattr(self.strategy, 'append_response_chunk'):
                                self.strategy.append_response_chunk(processed_content)

                if logprobs and 'logprobs' in chunk:
                    GLib.idle_add(self._add_logprobs_to_bubble, ai_bubble, chunk['logprobs'])

                if chunk.get('done') and self.stats_check.get_active():
                    stats_text = self._format_stats(chunk)
                    GLib.idle_add(self._add_stats_to_bubble, ai_bubble, stats_text)

                now = time.time()
                if now - last_scroll_time > 0.1:
                    GLib.idle_add(self._scroll_to_bottom)
                    last_scroll_time = now

            GLib.idle_add(self.strategy.on_response_complete, self, model_name)

        except Exception as e:
            GLib.idle_add(ai_bubble.append_text, f"\n\n### Connection Error\n\n{str(e)}")
        finally:
            GLib.idle_add(self.send_button.set_sensitive, True)
            GLib.idle_add(self._scroll_to_bottom)

    def _scroll_to_bottom(self) -> None:
        """Scrolls the chat scrolled window to the bottom."""
        adj = self.chat_box.get_parent().get_vadjustment()
        adj.set_value(adj.get_upper() - adj.get_page_size())

    def _add_logprobs_to_bubble(self, bubble: AiBubble, logprobs_data: List[Dict[str, Any]]) -> None:
        """Adds logprobs UI to an AI bubble."""
        if not hasattr(bubble, 'logprobs_container'):
            expander = Gtk.Expander(label=_("Logprobs"))
            expander.set_hexpand(True)
            expander.set_halign(Gtk.Align.FILL)
            
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
            bubble.bubble_box.append(expander)
            setattr(bubble, 'logprobs_container', text_view)
            
        text_view = getattr(bubble, 'logprobs_container')
        buffer = text_view.get_buffer()
        end_iter = buffer.get_end_iter()
        formatted_data = json.dumps(logprobs_data, indent=2)
        buffer.insert(end_iter, formatted_data + "\n")

    def _format_stats(self, chunk: Dict[str, Any]) -> str:
        """Formats Ollama performance statistics into a readable string."""
        total_duration = chunk.get('total_duration', 0) / 1e9
        load_duration = chunk.get('load_duration', 0) / 1e9
        prompt_eval_count = chunk.get('prompt_eval_count', 0)
        prompt_eval_duration = chunk.get('prompt_eval_duration', 1) / 1e9
        eval_count = chunk.get('eval_count', 0)
        eval_duration = chunk.get('eval_duration', 1) / 1e9
        
        stats = [
            f"{_('Total duration')}: {total_duration:.2f}s",
            f"{_('Load duration')}: {load_duration:.2f}s",
            f"{_('Prompt eval')}: {prompt_eval_count} tokens ({prompt_eval_count/prompt_eval_duration:.2f} t/s)",
            f"{_('Response')}: {eval_count} tokens ({eval_count/eval_duration:.2f} t/s)"
        ]
        return " | ".join(stats)

    def _add_stats_to_bubble(self, bubble: AiBubble, text: str) -> None:
        """Adds a statistics label to the AI bubble."""
        label = Gtk.Label(label=text)
        label.set_wrap(True)
        label.set_xalign(0)
        label.set_margin_top(6)
        label.set_margin_bottom(6)
        label.set_margin_start(12)
        label.set_margin_end(12)
        label.add_css_class("dim-label")
        bubble.bubble_box.append(label)

    def load_initial_history(self, history: List[Dict[str, Any]]) -> None:
        """Populates the chat box with an existing message history."""
        for msg in history:
            role = msg.get('role')
            content = msg.get('content')
            thinking_content = msg.get('thinking_content')
            
            if role == 'user':
                images = msg.get('images')
                self.add_message(content, sender=_("You"), images=images)
            elif role == 'assistant':
                model_name = msg.get('model', _('Assistant'))
                bubble = AiBubble(model_name=model_name)
                
                api_details = msg.get('api_details')
                if api_details:
                    bubble.set_api_details(api_details)
                    
                if thinking_content:
                    bubble.append_thinking(thinking_content)
                bubble.append_text(content)
                self.chat_box.append(bubble)

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

    def load_chat_settings(self, chat_data: Dict[str, Any]) -> None:
        """Populates the UI settings from a saved chat session."""
        model = chat_data.get('model')
        if model:
            setattr(self, 'pending_model_selection', model)

        system = chat_data.get('system')
        if system is not None:
            self.system_prompt_entry.set_text(system)

        host_id = chat_data.get('host')
        if host_id:
            setattr(self, 'pending_host_id', host_id)
            self.update_hosts()

        options = chat_data.get('options', {})
        if options:
            if 'thinking_val' in options:
                setattr(self, 'pending_thinking_val', options['thinking_val'])
                self.apply_pending_thinking()
            
            if 'seed' in options: self.seed_entry.set_text(str(options['seed']))
            if 'temperature' in options: self.temperature_entry.set_text(str(options['temperature']))
            if 'top_k' in options: self.top_k_entry.set_text(str(options['top_k']))
            if 'top_p' in options: self.top_p_entry.set_text(str(options['top_p']))
            if 'min_p' in options: self.min_p_entry.set_text(str(options['min_p']))
            if 'num_ctx' in options: self.num_ctx_entry.set_text(str(options['num_ctx']))
            if 'num_predict' in options: self.num_predict_entry.set_text(str(options['num_predict']))
            if 'stop' in options: self.stop_entry.set_text(", ".join(options['stop']))
            
            if 'stats' in options:
                self.stats_check.set_active(options['stats'])

            if 'logprobs' in options:
                self.logprobs_check.set_active(options['logprobs'])
            if 'top_logprobs' in options and options['top_logprobs'] is not None:
                self.top_logprobs_entry.set_text(str(options['top_logprobs']))

    def get_current_host(self) -> Optional[Dict[str, Any]]:
        """Returns the currently selected host from the dropdown."""
        selected_item = self.host_dropdown.get_selected_item()
        if not selected_item:
            return None
        
        name = selected_item.get_string()
        for host in self.storage.get_all_hosts():
            if host['name'] == name:
                return host
        return None
