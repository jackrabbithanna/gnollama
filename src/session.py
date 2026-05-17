from typing import List, Optional, Any, Dict, Callable
from gi.repository import GLib
import concurrent.futures
from . import ollama
from .storage import ChatStorage

class NetworkWorker:
    """Manages background network tasks cleanly."""
    def __init__(self, max_workers: int = 4) -> None:
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="GnollamaNetworkWorker")

    def submit(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> concurrent.futures.Future:
        return self.executor.submit(fn, *args, **kwargs)
        
    def shutdown(self, wait: bool = True) -> None:
        self.executor.shutdown(wait=wait, cancel_futures=True)

worker = NetworkWorker()

class GenerationStrategy:
    """Strategy for single-turn text generation."""
    def process(self, tab: Any, **kwargs: Any) -> Any:
        """Executes the generation process via Ollama API."""
        return ollama.generate(
            host=kwargs['host'],
            model=kwargs['model'],
            prompt=kwargs['prompt'],
            system=kwargs.get('system'),
            options=kwargs.get('options'),
            thinking=kwargs.get('thinking'),
            logprobs=kwargs.get('logprobs', False),
            top_logprobs=kwargs.get('top_logprobs'),
            images=kwargs.get('images')
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
        system = kwargs.get('system')
        
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
            options=kwargs.get('options'),
            thinking=kwargs.get('thinking'),
            logprobs=kwargs.get('logprobs', False),
            top_logprobs=kwargs.get('top_logprobs'),
            images=kwargs.get('images')
        )
