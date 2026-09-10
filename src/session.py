"""Per-request generation state and conversation persistence."""
import copy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from threading import Lock

from gi.repository import Gio
from . import ollama
from .structured import validate_response
from .tool_calling import inspect_calls, wire_calls, result_messages
from .knowledge import augmented_messages


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
    tool_calls: list = field(default_factory=list)
    continuation: bool = False
    saved_message: dict = None
    retrieval: dict = None

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
        calls = message.get('tool_calls')
        if calls is not None and calls != []:
            self.tool_calls.extend(copy.deepcopy(calls if isinstance(calls, list) else [calls]))
        if chunk.get('done'):
            self.metadata.update(status='complete',
                                 metrics={k: chunk[k] for k in METRIC_KEYS if k in chunk})
        return content, thinking, chunk.get('logprobs') or message.get('logprobs')

    def finish(self, status, error=None):
        if self.finalized:
            return False
        self.finalized = True
        self.metadata['status'] = status
        if error:
            self.metadata['error'] = error
        validation = None if self.tool_calls else (self.metadata.get('validation') or validate_response(self.content, self.settings.get('format'), status))
        if validation is not None:
            self.metadata['validation'] = validation
        if self.tool_calls:
            self.metadata.pop('validation', None)
            if 'tool_round' not in self.metadata:
                self.metadata['tool_round'] = inspect_calls(self.tool_calls, self.settings.get('tools'), status)
        if self.settings.get('history_images_omitted'):
            self.metadata['history_images_omitted'] = True
        return True

    def api_details(self):
        return {k: v for k, v in self.settings.items()
                if k not in ('host_id', 'show_stats', 'output_mode', 'schema_text', 'history_images_omitted',
                             'tools_enabled', 'tools_text', 'knowledge', 'query_override')}


def api_messages(history, include_images=True):
    """Local display metadata must never become part of the model's prompt."""
    messages = []
    for msg in history:
        calls = wire_calls(msg) if msg['role'] == 'assistant' else []
        if msg['role'] == 'assistant' and not msg.get('content') and not calls:
            continue
        keys = ('role', 'content', 'images') if include_images else ('role', 'content')
        item = {k: copy.deepcopy(msg[k]) for k in keys if k in msg}
        if msg['role'] == 'assistant':
            if msg.get('thinking_content'):
                item['thinking'] = msg['thinking_content']
            if calls:
                item['tool_calls'] = calls
        elif msg['role'] == 'tool':
            for key in ('tool_name', 'tool_call_id'):
                if key in msg:
                    item[key] = msg[key]
        messages.append(item)
    return messages


class GenerationStrategy:
    def begin(self, state, on_done=None):
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

    @property
    def pending_round(self):
        for msg in reversed(self.history):
            if msg['role'] == 'assistant':
                if (msg.get('response_metadata') or {}).get('tool_round', {}).get('state') == 'pending':
                    return msg
                return None
        return None

    def commit_results(self, cancel=False):
        message = self.pending_round
        if message is None:
            raise ValueError(_('There are no pending tool calls.'))
        results = result_messages(message, cancel)
        message['response_metadata']['tool_round']['state'] = 'cancelled' if cancel else 'submitted'
        message['response_metadata']['tool_round']['results'] = [r['content'] for r in results]
        self.history.extend(results)

    def begin(self, state, on_done=None):
        if not state.continuation:
            msg = {'role': 'user', 'content': state.prompt}
            if state.retrieval:
                msg['response_metadata'] = {'retrieval': copy.deepcopy(state.retrieval)}
            if state.images:
                msg['images'] = state.images
            self.history.append(msg)
        state.messages = api_messages(self.history, include_images=not state.settings.get('history_images_omitted'))
        if state.settings['system']:
            state.messages.insert(0, {'role': 'system', 'content': state.settings['system']})
        if state.continuation:
            latest = next((m for m in reversed(self.history) if m['role'] == 'user'), {})
            state.retrieval = copy.deepcopy((latest.get('response_metadata') or {}).get('retrieval'))
        state.messages = augmented_messages(state.messages, state.retrieval)
        if state.retrieval:
            state.metadata['retrieval'] = copy.deepcopy(state.retrieval)
        return self.save(state, on_done=on_done)

    def process(self, state):
        args = {k: state.settings[k] for k in
                ('host', 'model', 'options', 'thinking', 'logprobs', 'top_logprobs')}
        args.update(format=state.settings.get('format'), keep_alive=state.settings.get('keep_alive'))
        return ollama.chat(**args, messages=state.messages, cancellable=state.cancellable,
                           tools=state.settings.get('tools'))

    def save(self, state, on_done=None):
        if self.deleted or not self.chat_id:
            return
        if state.finalized and state.saved_message is None:
            state.saved_message = {'role': 'assistant', 'content': state.content,
                                 'thinking_content': state.thinking, 'model': state.settings['model'],
                                 'api_details': state.api_details(),
                                 'response_metadata': copy.deepcopy(state.metadata)}
            if state.tool_calls:
                state.saved_message['tool_calls'] = copy.deepcopy(state.tool_calls)
            self.history.append(state.saved_message)
        options = dict(state.settings['options'])
        options.update(thinking_val=state.settings['thinking'],
                       logprobs=state.settings['logprobs'],
                       top_logprobs=state.settings['top_logprobs'],
                       show_stats=state.settings['show_stats'])
        options.update(output_mode=state.settings.get('output_mode', 'text'),
                       schema_text=state.settings.get('schema_text', ''),
                       keep_alive=state.settings.get('keep_alive'),
                       tools_enabled=state.settings.get('tools_enabled', False),
                       tools_text=state.settings.get('tools_text', ''))
        options['knowledge'] = copy.deepcopy(state.settings.get('knowledge', {}))
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
