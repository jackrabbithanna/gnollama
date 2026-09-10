"""Ollama's native HTTP API, with cancellable streaming through libsoup."""
import copy
import json
import threading
from contextlib import contextmanager
from gettext import gettext as _
from urllib.parse import urlsplit

import gi

gi.require_version('Soup', '3.0')
from gi.repository import Gio, GLib, Soup


class OllamaError(Exception):
    """A server, protocol, or connection error."""


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
    try:
        message = Soup.Message.new(method, validate_host(host) + endpoint)
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
            raise OllamaError(detail or f'HTTP Error {message.get_status()}: {message.get_reason_phrase()}')
        yield stream, cancellable
    except GLib.Error as exc:
        if exc.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
            raise RequestCancelled(_('Request stopped')) from exc
        raise OllamaError(str(exc)) from exc
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
    with _response(host, endpoint, data, 'POST', timeout, cancellable) as (stream, cancel):
        pending = b''
        while True:
            chunk = stream.read_bytes(8192, cancel).get_data()
            if not chunk:
                if pending.strip():
                    yield _json_object(pending)
                return
            pending += chunk
            while b'\n' in pending:
                line, pending = pending.split(b'\n', 1)
                if line.strip():
                    yield _json_object(line)


def fetch_models(host, timeout=10, cancellable=None):
    return [model['name'] for model in fetch_model_details(host, timeout, cancellable)]


def fetch_model_details(host, timeout=10, cancellable=None):
    return _request(host, '/api/tags', timeout=timeout, cancellable=cancellable).get('models', [])


def show_model(host, name, timeout=10, cancellable=None):
    return _request(host, '/api/show', {'model': name, 'verbose': False}, 'POST', timeout, cancellable)


def get_version(host, timeout=5, cancellable=None):
    return _request(host, '/api/version', timeout=timeout, cancellable=cancellable).get('version', 'Unknown')


def delete_model(host, model_name, timeout=10, cancellable=None):
    _request(host, '/api/delete', {'model': model_name}, 'DELETE', timeout, cancellable)
    return True


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


def _add_common_params(data, options, thinking, logprobs, top_logprobs):
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
             logprobs=False, top_logprobs=None, images=None, timeout=300, cancellable=None):
    data = {'model': model, 'prompt': prompt, 'stream': True}
    if images:
        data['images'] = images
    if system:
        data['system'] = system
    _add_common_params(data, options, thinking, logprobs, top_logprobs)
    yield from _stream_response(host, '/api/generate', data, timeout, cancellable)


def chat(host, model, messages, options=None, thinking=None, logprobs=False,
         top_logprobs=None, images=None, timeout=300, cancellable=None):
    data = {'model': model, 'messages': copy.deepcopy(messages), 'stream': True}
    if images and data['messages'] and data['messages'][-1]['role'] == 'user':
        data['messages'][-1]['images'] = images
    _add_common_params(data, options, thinking, logprobs, top_logprobs)
    yield from _stream_response(host, '/api/chat', data, timeout, cancellable)
