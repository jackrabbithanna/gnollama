"""Per-request generation state and conversation persistence."""
import copy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from threading import Lock

from gi.repository import Gio
from . import ollama
from .structured import validate_response


class NetworkWorker:
    def __init__(self, max_workers=4):
        self.executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix='GnollamaNetwork')
        self._futures = set()
        self._lock = Lock()

    def submit(self, fn, *args, **kwargs):
        future = self.executor.submit(fn, *args, **kwargs)
        with self._lock:
            self._futures.add(future)
        future.add_done_callback(self._finished)
        return future

    def _finished(self, future):
        with self._lock:
            self._futures.discard(future)

    @property
    def idle(self):
        with self._lock:
            return not self._futures

    def shutdown(self, wait=True):
        self.executor.shutdown(wait=wait)


worker = NetworkWorker()
METRIC_KEYS = ('total_duration', 'load_duration', 'prompt_eval_count',
               'prompt_eval_cached_count', 'prompt_eval_duration', 'eval_count',
               'eval_duration', 'done_reason')


@dataclass
class RequestState:
    settings: dict
    prompt: str
    images: list = field(default_factory=list)
    messages: list = field(default_factory=list)
    cancellable: Gio.Cancellable = field(default_factory=Gio.Cancellable)
    content: str = ''
    thinking: str = ''
    metadata: dict = field(default_factory=lambda: {'status': 'running'})
    finalized: bool = False

    def __post_init__(self):
        self.settings = copy.deepcopy(self.settings)
        self.images = list(self.images)

    def consume(self, chunk):
        if 'error' in chunk:
            raise ollama.OllamaError(str(chunk['error']))
        message = chunk.get('message') or {}
        content = message.get('content') or chunk.get('response', '')
        thinking = message.get('thinking') or chunk.get('thinking', '')
        self.content += content
        self.thinking += thinking
        if chunk.get('done'):
            self.metadata = {'status': 'complete',
                             'metrics': {k: chunk[k] for k in METRIC_KEYS if k in chunk}}
        return content, thinking, chunk.get('logprobs') or message.get('logprobs')

    def finish(self, status, error=None):
        if self.finalized:
            return False
        self.finalized = True
        self.metadata['status'] = status
        if error:
            self.metadata['error'] = error
        validation = self.metadata.get('validation') or validate_response(self.content, self.settings.get('format'), status)
        if validation is not None:
            self.metadata['validation'] = validation
        if self.settings.get('history_images_omitted'):
            self.metadata['history_images_omitted'] = True
        return True

    def api_details(self):
        return {k: v for k, v in self.settings.items()
                if k not in ('host_id', 'show_stats', 'output_mode', 'schema_text', 'history_images_omitted')}


def api_messages(history, include_images=True):
    """Local display metadata must never become part of the model's prompt."""
    messages = []
    for msg in history:
        if msg['role'] == 'assistant' and not msg.get('content'):
            continue
        keys = ('role', 'content', 'images') if include_images else ('role', 'content')
        messages.append({k: copy.deepcopy(msg[k]) for k in keys if k in msg})
    return messages


class GenerationStrategy:
    def begin(self, state):
        pass

    def process(self, state):
        args = {k: state.settings[k] for k in
                ('host', 'model', 'options', 'thinking', 'logprobs', 'top_logprobs')}
        args.update(format=state.settings.get('format'), keep_alive=state.settings.get('keep_alive'))
        return ollama.generate(**args, prompt=state.prompt, system=state.settings['system'],
                               images=state.images, cancellable=state.cancellable)

    def save(self, state, on_done=None):
        pass


class ChatStrategy(GenerationStrategy):
    def __init__(self, storage, chat_id=None, initial_history=None):
        self.storage = storage
        self.chat_id = chat_id
        self.history = copy.deepcopy(initial_history or [])
        self.deleted = False

    def begin(self, state):
        msg = {'role': 'user', 'content': state.prompt}
        if state.images:
            msg['images'] = state.images
        self.history.append(msg)
        state.messages = api_messages(self.history, include_images=not state.settings.get('history_images_omitted'))
        if state.settings['system']:
            state.messages.insert(0, {'role': 'system', 'content': state.settings['system']})
        self.save(state)

    def process(self, state):
        args = {k: state.settings[k] for k in
                ('host', 'model', 'options', 'thinking', 'logprobs', 'top_logprobs')}
        args.update(format=state.settings.get('format'), keep_alive=state.settings.get('keep_alive'))
        return ollama.chat(**args, messages=state.messages, cancellable=state.cancellable)

    def save(self, state, on_done=None):
        if self.deleted or not self.chat_id:
            return
        if state.finalized:
            self.history.append({'role': 'assistant', 'content': state.content,
                                 'thinking_content': state.thinking, 'model': state.settings['model'],
                                 'api_details': state.api_details(),
                                 'response_metadata': copy.deepcopy(state.metadata)})
        options = dict(state.settings['options'])
        options.update(thinking_val=state.settings['thinking'],
                       logprobs=state.settings['logprobs'],
                       top_logprobs=state.settings['top_logprobs'],
                       show_stats=state.settings['show_stats'])
        options.update(output_mode=state.settings.get('output_mode', 'text'),
                       schema_text=state.settings.get('schema_text', ''),
                       keep_alive=state.settings.get('keep_alive'))
        return self.storage.save_chat(self.chat_id, self.history, model=state.settings['model'],
                                      options=options, system=state.settings['system'],
                                      host=state.settings['host_id'], on_done=on_done)


class ViewRequests:
    """Cancel a window's jobs and suppress deliveries after it closes."""
    def __init__(self, window):
        import weakref
        self.closed = False
        self._cancellables = weakref.WeakSet()
        window.connect('close-request', self.close)
        window.connect('destroy', self.close)

    def new_cancel(self):
        cancel = Gio.Cancellable()
        self._cancellables.add(cancel)
        if self.closed:
            cancel.cancel()
        return cancel

    def close(self, *args):
        self.closed = True
        for cancel in list(self._cancellables):
            cancel.cancel()
        return False

    def deliver(self, fn, *args, cancellable=None):
        from gi.repository import GLib
        def dispatch():
            if not self.closed and (cancellable is None or not cancellable.is_cancelled()):
                fn(*args)
            return False
        GLib.idle_add(dispatch)
