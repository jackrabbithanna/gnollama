"""Generation lifecycle and coalesced delivery, independent of GTK widgets."""
import threading
import time
from gi.repository import GLib
from . import ollama
from .structured import validate_response
from .tool_calling import inspect_calls


class StreamDelivery:
    def __init__(self, callback):
        self.callback = callback
        self.lock = threading.Lock()
        self.content, self.thinking, self.logprobs = [], [], []
        self.source = None

    def append(self, content, thinking, logprobs):
        with self.lock:
            self.content.append(content)
            self.thinking.append(thinking)
            if logprobs:
                self.logprobs.extend(logprobs if isinstance(logprobs, list) else [logprobs])
            if self.source is None:
                self.source = GLib.timeout_add(50, self.flush)

    def flush(self):
        with self.lock:
            source, self.source = self.source, None
            content, thinking, logprobs = ''.join(self.content), ''.join(self.thinking), self.logprobs
            self.content, self.thinking, self.logprobs = [], [], []
        if source is not None:
            GLib.source_remove(source)
        if content or thinking or logprobs:
            self.callback(content, thinking, logprobs)
        return False


class RequestRunner:
    def __init__(self, services):
        self.services = services

    def run(self, strategy, state, on_chunk, on_done):
        started = time.monotonic()
        status, error = 'failed', None
        delivery = StreamDelivery(on_chunk)
        try:
            if state.cancellable.is_cancelled():
                raise ollama.RequestCancelled()
            with self.services.models.using(state.settings['host'], state.settings['model']):
                for chunk in strategy.process(state):
                    if state.cancellable.is_cancelled():
                        raise ollama.RequestCancelled()
                    delivery.append(*state.consume(chunk))
                    if chunk.get('done'):
                        status = 'complete'
                        break
                else:
                    raise ollama.OllamaError(_('Response ended before completion.'))
        except ollama.RequestCancelled:
            status = 'stopped'
        except Exception as exc:
            error = str(exc)
        try:
            if state.tool_calls:
                state.metadata['tool_round'] = inspect_calls(state.tool_calls, state.settings.get('tools'), status)
            elif state.settings.get('format') is not None:
                state.metadata['validation'] = validate_response(state.content, state.settings['format'], status)
        except Exception as exc:
            status, error = 'failed', str(exc)
            state.metadata['validation'] = {'status': 'incomplete', 'error': str(exc)}
        state.metadata['elapsed_seconds'] = time.monotonic() - started
        def finish():
            delivery.flush()
            on_done(status, error)
            return False
        GLib.idle_add(finish)
