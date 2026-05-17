from typing import List, Optional, Any, Dict, Union
from gi.repository import Gtk, Gio, GLib, GObject
import threading
from . import ollama
from .storage import ChatStorage
from .session import GenerationStrategy, ChatStrategy

from .widgets.message_list import MessageList
from .widgets.chat_input import ChatInput
from .widgets.options_panel import OptionsPanel

@Gtk.Template(resource_path='/io/github/jackrabbithanna/Gnollama/tab.ui')
class GenerationTab(Gtk.Box):
    """The main widget for a chat or generation session."""
    __gtype_name__ = 'GenerationTab'
    
    __gsignals__ = {
        'chat-updated': (GObject.SignalFlags.RUN_FIRST, None, (str, str)),
    }

    message_list: MessageList = Gtk.Template.Child()
    chat_input: ChatInput = Gtk.Template.Child()
    options_panel: OptionsPanel = Gtk.Template.Child()

    def __init__(self, tab_label: Optional[Gtk.Label] = None, mode: str = 'generate', chat_id: Optional[str] = None, 
                 initial_history: Optional[List[Dict[str, Any]]] = None, storage: Optional[ChatStorage] = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.init_template()
        self.tab_label = tab_label
        
        if not storage:
            storage = ChatStorage()
        self.storage = storage

        if mode == 'chat':
            self.strategy = ChatStrategy(storage, chat_id=chat_id, initial_history=initial_history)
        else:
            self.strategy = GenerationStrategy()
            
        self.mode = mode
        
        self.options_panel.storage = self.storage
        self.options_panel.update_hosts()
        
        self.chat_input.send_button.connect('clicked', self.on_send_clicked)
        self.chat_input.entry.connect('activate', self.on_send_clicked)
        self.options_panel.system_prompt_entry.connect('activate', self.on_send_clicked)
        
        self.options_panel.host_dropdown.connect('notify::selected-item', self.on_host_changed)
        
        self.on_host_changed()

        if mode == 'chat':
            if chat_id:
                chat_data = storage.get_chat(chat_id)
                if chat_data:
                    self.load_chat_settings(chat_data)
            
            if initial_history:
                 self.load_initial_history(initial_history)

    def load_chat_settings(self, chat_data: Dict[str, Any]) -> None:
        if 'options' in chat_data:
            options = chat_data['options']
            self.options_panel.load_options(options)
            
            if 'thinking_val' in options:
                self.chat_input.load_thinking_val(options['thinking_val'])
        
        if 'system' in chat_data and chat_data['system']:
            self.options_panel.system_prompt_entry.set_text(chat_data['system'])
            
        if 'host' in chat_data:
            host_id = chat_data['host']
            for i, h in enumerate(self.options_panel.host_list):
                if h['id'] == host_id:
                    self.options_panel.host_dropdown.set_selected(i)
                    break

        if 'model' in chat_data:
            self.chat_input.pending_model_selection = chat_data['model']
            GLib.idle_add(self.chat_input.select_model, chat_data['model'])

    def load_initial_history(self, history: List[Dict[str, Any]]) -> None:
        for msg in history:
            role = msg.get('role')
            content = msg.get('content', '')
            if role == 'user':
                images = msg.get('images')
                self.message_list.add_user_message(content, images=images)
            elif role == 'assistant':
                from .bubbles import AiBubble
                bubble = AiBubble(model_name=msg.get('model', ''))
                if 'thinking_content' in msg:
                    bubble.append_thinking(msg['thinking_content'])
                bubble.append_text(content)
                if 'api_details' in msg:
                    bubble.set_api_details(msg['api_details'])
                self.message_list.add_ai_bubble(bubble)
            elif role == 'system':
                self.message_list.add_system_message(content)

    def on_host_changed(self, *args: Any) -> None:
        host = self.options_panel.get_selected_host()
        if host:
            self.chat_input.fetch_models(host['hostname'])

    def on_send_clicked(self, *args: Any) -> None:
        prompt = self.chat_input.entry.get_text().strip()
        if not prompt: return
        
        if self.tab_label:
            truncated = prompt[:20] + "..." if len(prompt) > 20 else prompt
            self.tab_label.set_label(truncated)
            
        self.chat_input.entry.set_text("")
        images = []
        if self.chat_input.selected_image_paths:
            import base64
            for path in self.chat_input.selected_image_paths:
                with open(path, "rb") as image_file:
                    encoded = base64.b64encode(image_file.read()).decode('utf-8')
                    images.append(encoded)
            self.chat_input.on_clear_image_clicked(None)

        self.message_list.add_user_message(prompt, images=images)
        
        # Extract all UI state on the main thread before launching the background request
        host = self.options_panel.get_selected_host()
        model = self.chat_input.get_selected_model()
        thinking = self.chat_input.get_thinking_value()
        
        options = self.options_panel.get_options_from_ui()
        system = self.options_panel.system_prompt_entry.get_text().strip()
        logprobs = self.options_panel.logprobs_check.get_active()
        show_stats = self.options_panel.stats_check.get_active()
        
        top_logprobs = None
        if logprobs:
            try:
                top_logprobs = int(self.options_panel.top_logprobs_entry.get_text().strip())
            except ValueError:
                pass
                
        req_data = {
            'host': host,
            'model': model,
            'thinking': thinking,
            'options': options,
            'system': system,
            'logprobs': logprobs,
            'show_stats': show_stats,
            'top_logprobs': top_logprobs
        }
        
        from .session import worker
        worker.submit(self.process_request, prompt, images, req_data)

    def process_request(self, prompt: str, images: Optional[List[str]], req_data: Dict[str, Any]) -> None:
        host = req_data.get('host')
        if not host:
            GLib.idle_add(self.message_list.add_system_message, _("Error: No host configured."))
            return
            
        model = req_data.get('model')
        thinking = req_data.get('thinking')
        options = req_data.get('options')
        system = req_data.get('system')
        logprobs = req_data.get('logprobs')
        show_stats = req_data.get('show_stats', False)
        top_logprobs = req_data.get('top_logprobs')
                
        api_params = {
            "endpoint": "chat" if isinstance(self.strategy, ChatStrategy) else "generate",
            "host": host['hostname'],
            "model": model,
            "options": options if options else None,
            "thinking": thinking,
            "logprobs": logprobs,
            "top_logprobs": top_logprobs,
        }
        
        if not isinstance(self.strategy, ChatStrategy) and system:
            api_params["system"] = system
            
        self.strategy.current_api_params = api_params
        
        ai_bubble = None
        def create_bubble():
            nonlocal ai_bubble
            from .bubbles import AiBubble
            ai_bubble = AiBubble(model_name=model)
            ai_bubble.set_api_details(api_params)
            self.message_list.add_ai_bubble(ai_bubble)
            
        GLib.idle_add(create_bubble)
        
        import time
        while ai_bubble is None:
            time.sleep(0.01)

        if hasattr(self.strategy, 'current_response_full_text'):
            self.strategy.current_response_full_text = ""
        if hasattr(self.strategy, 'current_thinking_full_text'):
            self.strategy.current_thinking_full_text = ""

        try:
            for chunk in self.strategy.process(
                self,
                host=host['hostname'],
                host_id=host['id'],
                model=model,
                prompt=prompt,
                system=system if system else None,
                options=options if options else None,
                thinking=thinking,
                logprobs=logprobs,
                top_logprobs=top_logprobs,
                images=images
            ):
                if 'error' in chunk:
                    error_header = _("Error")
                    GLib.idle_add(ai_bubble.append_text, f"\n\n### {error_header}\n\n{chunk['error']}")
                    break
                    
                native_thinking = chunk.get('thinking', chunk.get('thought', ''))
                if not native_thinking and 'message' in chunk:
                    native_thinking = chunk['message'].get('thinking', '')
                    
                
                # Tag logic to support <think> fallback
                content = chunk.get('message', {}).get('content') or chunk.get('response', '')
                
                if content and not native_thinking:
                    # Simple fallback logic since we don't track full state across chunks here
                    # Actually, we should just let the user see <think> for now or keep it simple.
                    pass

                if native_thinking:
                    GLib.idle_add(ai_bubble.append_thinking, native_thinking)
                    if hasattr(self.strategy, 'append_thinking'):
                        self.strategy.append_thinking(native_thinking)
                        
                if content:
                    GLib.idle_add(ai_bubble.append_text, content)
                    if hasattr(self.strategy, 'append_response_chunk'):
                        self.strategy.append_response_chunk(content)
                        
                logprobs_data = chunk.get('logprobs')
                if not logprobs_data and 'message' in chunk:
                    logprobs_data = chunk['message'].get('logprobs')
                if logprobs_data:
                    GLib.idle_add(ai_bubble.append_logprobs, logprobs_data)
                        
                if chunk.get('done', False):
                    metrics = {
                        k: chunk[k] for k in [
                            'total_duration', 'load_duration', 'prompt_eval_count', 
                            'prompt_eval_duration', 'eval_count', 'eval_duration'
                        ] if k in chunk
                    }
                    if show_stats and metrics:
                        GLib.idle_add(ai_bubble.show_stats, metrics)
                        
                    if hasattr(self.strategy, 'on_response_complete'):
                        self.strategy.on_response_complete(self, model)
                        
        except Exception as e:
            conn_err = _("Connection Error")
            GLib.idle_add(ai_bubble.append_text, f"\n\n### {conn_err}\n\n{str(e)}")
