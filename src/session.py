"""Per-request generation state and conversation persistence."""
from gettext import gettext as _
import copy
from typing import Optional
from dataclasses import dataclass, field

from gi.repository import Gio
from .providers import ProviderError
from .structured import validate_response
from .tool_calling import inspect_calls, wire_calls, result_messages
from .knowledge import augmented_messages
from .records import RequestSettings


def display_chat_title(title):
    """Localize the database's automatic-title marker only when displaying it."""
    return _('New Chat') if title == 'New Chat' else title


METRIC_KEYS = ('total_duration', 'load_duration', 'prompt_eval_count',
               'prompt_eval_cached_count', 'prompt_eval_duration', 'eval_count',
               'eval_duration', 'done_reason')


@dataclass
class RequestState:
    settings: RequestSettings
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
    saved_message: Optional[dict] = None
    retrieval: Optional[dict] = None
    connection: object = field(default=None, repr=False)
    draft_id: str | None = None
    draft_revision: int | None = None
    # Whatever the view uses to show this request; opaque to controllers.
    presentation: object = field(default=None, repr=False)

    def __post_init__(self):
        self.settings = copy.deepcopy(self.settings)
        self.images = list(self.images)

    def consume(self, chunk):
        if 'error' in chunk:
            raise ProviderError(str(chunk['error']))
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
        validation = None if self.tool_calls else (self.metadata.get('validation') or validate_response(self.content, self.settings.wire.format, status))
        if validation is not None:
            self.metadata['validation'] = validation
        if self.tool_calls:
            self.metadata.pop('validation', None)
            if 'tool_round' not in self.metadata:
                self.metadata['tool_round'] = inspect_calls(self.tool_calls, self.settings.wire.tools, status)
        if self.settings.history_images_omitted:
            self.metadata['history_images_omitted'] = True
        return True

    def api_details(self):
        return self.settings.api_details()


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
        wire = state.settings.wire
        return state.connection.generate(model=state.settings.target.model, **wire.provider_args(),
                                         prompt=state.prompt, system=wire.system,
                                         images=state.images, cancellable=state.cancellable)

    def save(self, state, on_done=None):
        pass


class ChatStrategy(GenerationStrategy):
    def __init__(self, storage, chat_id=None, initial_history=None):
        self.storage = storage
        self.chat_id = chat_id
        self.history = copy.deepcopy(initial_history or [])
        self._saved_count = len(self.history)
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
        self._saved_count = min(self._saved_count, self.history.index(message))
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
        state.messages = api_messages(self.history, include_images=not state.settings.history_images_omitted)
        if state.settings.wire.system:
            state.messages.insert(0, {'role': 'system', 'content': state.settings.wire.system})
        if state.continuation:
            latest: dict = next((m for m in reversed(self.history) if m['role'] == 'user'), {})
            state.retrieval = copy.deepcopy((latest.get('response_metadata') or {}).get('retrieval'))
        state.messages = augmented_messages(state.messages, state.retrieval)
        if state.retrieval:
            state.metadata['retrieval'] = copy.deepcopy(state.retrieval)
        return self.save(state, on_done=on_done)

    def process(self, state):
        wire = state.settings.wire
        return state.connection.chat(model=state.settings.target.model, **wire.provider_args(),
                                     messages=state.messages, cancellable=state.cancellable, tools=wire.tools)

    def save(self, state, on_done=None):
        if self.deleted or not self.chat_id:
            return
        settings = state.settings
        if state.finalized and state.saved_message is None:
            state.saved_message = {'role': 'assistant', 'content': state.content,
                                 'thinking_content': state.thinking, 'model': settings.target.model,
                                 'api_details': state.api_details(),
                                 'response_metadata': copy.deepcopy(state.metadata)}
            if state.tool_calls:
                state.saved_message['tool_calls'] = copy.deepcopy(state.tool_calls)
            self.history.append(state.saved_message)
        start = self._saved_count
        self._saved_count = len(self.history)
        return self.storage.save_chat_delta(self.chat_id, self.history[start:], start,
                                      draft_id=state.draft_id if not state.finalized else None,
                                      draft_revision=state.draft_revision,
                                      model=settings.target.model,
                                      options=settings.to_chat_options(), system=settings.wire.system,
                                      host=settings.target.host_id, on_done=on_done)


class ViewRequests:
    """Cancel a window's or dialog's jobs and suppress deliveries after it closes."""
    def __init__(self, window):
        import weakref
        self.closed = False
        self._cancellables: weakref.WeakSet = weakref.WeakSet()
        from gi.repository import Adw
        window.connect('closed' if isinstance(window, Adw.Dialog) else 'close-request', self.close)
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
