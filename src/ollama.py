"""Ollama's native HTTP API, with cancellable streaming through libsoup."""
import copy
import json
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from gettext import gettext as _
from urllib.parse import urlsplit

import gi

gi.require_version('Soup', '3.0')
from gi.repository import Gio, GLib, Soup
from .credentials import CredentialError

CLOUD_URL = 'https://ollama.com'


@dataclass(frozen=True)
class Connection:
    """A request destination and credential reference, never an API key."""
    url: str
    host_id: str
    credential_id: str | None = None
    provider: str = 'ollama_cloud'
    credentials: object = field(default=None, repr=False, compare=False)


def is_cloud(host):
    if isinstance(host, Connection):
        return host.provider == 'ollama_cloud'
    return isinstance(host, dict) and host.get('provider') == 'ollama_cloud'


class OllamaError(Exception):
    """A server, protocol, or connection error."""

    def __init__(self, message='', status=None):
        super().__init__(message)
        self.status = status


class RequestCancelled(OllamaError):
    """An operation was deliberately stopped."""


_active = set()
_lock = threading.Lock()
_stopping = False


def cancel_all():
    """Cancel active operations and prevent queued operations from starting."""
    global _stopping
    with _lock:
        _stopping = True
        active = list(_active)
    for cancellable in active:
        cancellable.cancel()


def resume():
    global _stopping
    with _lock:
        _stopping = False


def validate_host(host):
    if isinstance(host, Connection):
        if host.provider != 'ollama_cloud' or host.url != CLOUD_URL:
            raise OllamaError(_('Ollama Cloud requires https://ollama.com.'))
        host = host.url
    try:
        url = urlsplit(host)
        valid = url.scheme in ('http', 'https') and url.hostname and not url.query and not url.fragment
        url.port  # Validate the port as well.
    except ValueError:
        valid = False
    if not valid:
        raise OllamaError(_('Enter a valid http:// or https:// Ollama host URL.'))
    return host.rstrip('/')


@contextmanager
def _response(host, endpoint, data=None, method='POST', timeout=10, cancellable=None):
    cancellable = cancellable if cancellable is not None else Gio.Cancellable()
    with _lock:
        if _stopping or cancellable.is_cancelled():
            raise RequestCancelled(_('Request stopped'))
        _active.add(cancellable)
    session = Soup.Session(timeout=timeout)
    stream = None
    key = None
    try:
        message = Soup.Message.new(method, validate_host(host) + endpoint)
        if is_cloud(host):
            if endpoint not in ('/api/tags', '/api/show', '/api/chat', '/api/generate'):
                raise OllamaError(_('This operation is unavailable for Ollama Cloud.'))
            message.add_flags(Soup.MessageFlags.NO_REDIRECT)
            try:
                key = host.credentials.lookup(host.host_id, host.credential_id, cancellable)
            except CredentialError as exc:
                if cancellable.is_cancelled():
                    raise RequestCancelled(_('Request stopped')) from None
                raise OllamaError(str(exc)) from None
            message.get_request_headers().replace('Authorization', 'Bearer ' + key)
            if data is not None:
                data = {k: v for k, v in data.items() if k not in ('format', 'keep_alive')}
        if data is not None:
            body = json.dumps(data, allow_nan=False).encode('utf-8')
            message.set_request_body_from_bytes('application/json', GLib.Bytes.new(body))
        stream = session.send(message, cancellable)
        if not 200 <= message.get_status() < 300:
            body = _read_all(stream, cancellable)
            detail = None
            try:
                decoded = json.loads(body)
                if isinstance(decoded, dict):
                    detail = decoded.get('error')
            except (ValueError, UnicodeError):
                pass
            status = message.get_status()
            if is_cloud(host) and status in (401, 403):
                detail = _('Ollama Cloud rejected the API key or account access. Edit this host to check its API key.')
            elif is_cloud(host) and status == 429:
                detail = _('Ollama Cloud usage or rate limit reached. Try again later.')
            raise OllamaError(str(detail or f'HTTP Error {status}: {message.get_reason_phrase()}'), status)
        yield stream, cancellable
    except RequestCancelled:
        raise
    except OllamaError as exc:
        if key:
            raise OllamaError(str(exc).replace(key, '[redacted]'), exc.status) from None
        raise
    except GLib.Error as exc:
        if exc.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
            raise RequestCancelled(_('Request stopped')) from exc
        raise OllamaError(str(exc).replace(key, '[redacted]') if key else str(exc)) from None
    finally:
        if stream is not None:
            try:
                stream.close(None)
            except GLib.Error:
                pass
        session.abort()
        with _lock:
            _active.discard(cancellable)


def _read_all(stream, cancellable):
    chunks = []
    while True:
        chunk = stream.read_bytes(8192, cancellable).get_data()
        if not chunk:
            return b''.join(chunks)
        chunks.append(chunk)


def _json_object(raw):
    try:
        result = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise OllamaError(_('Invalid JSON received from Ollama')) from exc
    if not isinstance(result, dict):
        raise OllamaError(_('Expected a JSON object from Ollama'))
    return result


def _request(host, endpoint, data=None, method='GET', timeout=10, cancellable=None):
    with _response(host, endpoint, data, method, timeout, cancellable) as (stream, cancel):
        raw = _read_all(stream, cancel)
        result = _json_object(raw) if raw.strip() else {}
        if 'error' in result:
            raise OllamaError(str(result['error']))
        return result


def _stream_response(host, endpoint, data, timeout=300, cancellable=None):
    def decode(raw):
        chunk = _json_object(raw)
        if 'error' in chunk:
            raise OllamaError(str(chunk['error']))
        return chunk

    with _response(host, endpoint, data, 'POST', timeout, cancellable) as (stream, cancel):
        pending = b''
        while True:
            chunk = stream.read_bytes(8192, cancel).get_data()
            if not chunk:
                if pending.strip():
                    yield decode(pending)
                return
            pending += chunk
            while b'\n' in pending:
                line, pending = pending.split(b'\n', 1)
                if line.strip():
                    yield decode(line)


def fetch_models(host, timeout=10, cancellable=None, *, details=False):
    models = fetch_model_details(host, timeout, cancellable)
    return models if details else [model['name'] for model in models]


def fetch_model_details(host, timeout=10, cancellable=None):
    return _request(host, '/api/tags', timeout=timeout, cancellable=cancellable).get('models', [])


def show_model(host, name, timeout=10, cancellable=None):
    return _request(host, '/api/show', {'model': name, 'verbose': False}, 'POST', timeout, cancellable)


def get_version(host, timeout=5, cancellable=None):
    return _request(host, '/api/version', timeout=timeout, cancellable=cancellable).get('version', 'Unknown')


def embed(host, model, input, dimensions=None, keep_alive=None, timeout=300, cancellable=None):
    """Return validated native embeddings without allowing silent input truncation."""
    import math
    texts = [input] if isinstance(input, str) else list(input)
    if not texts or any(not isinstance(text, str) or not text.strip() for text in texts):
        raise OllamaError(_('Enter nonempty text to embed.'))
    data = {'model': model, 'input': texts, 'truncate': False}
    if dimensions is not None:
        if type(dimensions) is not int or dimensions <= 0:
            raise OllamaError(_('Embedding dimensions must be a positive integer.'))
        data['dimensions'] = dimensions
    if keep_alive is not None:
        data['keep_alive'] = keep_alive
    result = _request(host, '/api/embed', data, 'POST', timeout, cancellable)
    vectors = result.get('embeddings') if isinstance(result, dict) else None
    if not isinstance(vectors, list) or len(vectors) != len(texts):
        raise OllamaError(_('The server returned an incorrect number of embeddings.'))
    size = dimensions
    for vector in vectors:
        if (not isinstance(vector, list) or not vector or
                any(type(n) not in (int, float) or not math.isfinite(n) for n in vector)):
            raise OllamaError(_('The server returned an invalid embedding vector.'))
        size = size or len(vector)
        if len(vector) != size or not any(vector):
            raise OllamaError(_('Embedding dimensions do not match, or a vector is zero.'))
    return result


def delete_model(host, model_name, timeout=10, cancellable=None):
    _request(host, '/api/delete', {'model': model_name}, 'DELETE', timeout, cancellable)
    return True


def fetch_running_models(host, timeout=10, cancellable=None):
    return _request(host, '/api/ps', timeout=timeout, cancellable=cancellable).get('models', [])


def unload_model(host, model, timeout=30, cancellable=None):
    return _request(host, '/api/generate', {'model': model, 'keep_alive': 0, 'stream': False},
                    'POST', timeout, cancellable)


def pull(host, model, insecure=False, timeout=300, cancellable=None):
    data = {'model': model, 'insecure': insecure, 'stream': True}
    complete = False
    for chunk in _stream_response(host, '/api/pull', data, timeout, cancellable):
        if 'error' in chunk:
            raise OllamaError(str(chunk['error']))
        yield chunk
        if chunk.get('status') == 'success':
            complete = True
            break
    if not complete:
        raise OllamaError(_('Model pull ended before completion'))


def _add_common_params(data, options, thinking, logprobs, top_logprobs, format=None, keep_alive=None):
    if format is not None:
        if format != 'json' and not isinstance(format, dict):
            raise OllamaError(_('Invalid output format'))
        data['format'] = copy.deepcopy(format)
    if keep_alive is not None:
        if isinstance(keep_alive, bool) or not isinstance(keep_alive, int) or keep_alive < -1:
            raise OllamaError(_('Keep-alive must be a duration in seconds, 0, or -1'))
        data['keep_alive'] = keep_alive
    if thinking is not None:
        if not isinstance(thinking, bool) and thinking not in ('low', 'medium', 'high', 'max'):
            raise OllamaError(_('Invalid thinking setting'))
        data['think'] = thinking
    if logprobs:
        data['logprobs'] = True
        if top_logprobs is not None:
            if not isinstance(top_logprobs, int) or not 0 <= top_logprobs <= 20:
                raise OllamaError(_('Top logprobs must be between 0 and 20'))
            data['top_logprobs'] = top_logprobs
    if options:
        data['options'] = copy.deepcopy(options)


def generate(host, model, prompt, system=None, options=None, thinking=None,
             logprobs=False, top_logprobs=None, images=None, timeout=300, cancellable=None,
             format=None, keep_alive=None):
    data = {'model': model, 'prompt': prompt, 'stream': True}
    if images:
        data['images'] = images
    if system:
        data['system'] = system
    _add_common_params(data, options, thinking, logprobs, top_logprobs, format, keep_alive)
    yield from _stream_response(host, '/api/generate', data, timeout, cancellable)


def chat(host, model, messages, options=None, thinking=None, logprobs=False,
         top_logprobs=None, images=None, timeout=300, cancellable=None, format=None, keep_alive=None, tools=None):
    data = {'model': model, 'messages': copy.deepcopy(messages), 'stream': True}
    if tools:
        data['tools'] = copy.deepcopy(tools)
    if images and data['messages'] and data['messages'][-1]['role'] == 'user':
        data['messages'][-1]['images'] = images
    _add_common_params(data, options, thinking, logprobs, top_logprobs, format, keep_alive)
    yield from _stream_response(host, '/api/chat', data, timeout, cancellable)
