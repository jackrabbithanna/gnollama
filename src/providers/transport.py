"""Cancellable HTTP through libsoup, shared by every provider."""
import json
import threading
from contextlib import contextmanager
from gettext import gettext as _
from urllib.parse import urlsplit

import gi

gi.require_version('Soup', '3.0')
from gi.repository import Gio, GLib, Soup


class ProviderError(Exception):
    """A server, protocol, or connection error."""

    def __init__(self, message='', status=None):
        super().__init__(message)
        self.status = status


class RequestCancelled(ProviderError):
    """An operation was deliberately stopped."""


def validate_url(url):
    try:
        parts = urlsplit(url)
        valid = parts.scheme in ('http', 'https') and parts.hostname and not parts.query and not parts.fragment
        parts.port  # Validate the port as well.
    except (ValueError, AttributeError, TypeError):
        valid = False
    if not valid:
        raise ProviderError(_('Enter a valid http:// or https:// Ollama host URL.'))
    return url.rstrip('/')


def read_all(stream, cancellable):
    chunks: list = []
    while True:
        chunk = stream.read_bytes(8192, cancellable).get_data()
        if not chunk:
            return b''.join(chunks)
        chunks.append(chunk)


def json_object(raw):
    try:
        result = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise ProviderError(_('Invalid JSON received from Ollama')) from exc
    if not isinstance(result, dict):
        raise ProviderError(_('Expected a JSON object from Ollama'))
    return result


class Transport:
    """Sends requests and tracks them, so one application can stop all of its own."""

    def __init__(self):
        self._active = set()
        self._lock = threading.Lock()
        self._stopping = False

    def cancel_all(self):
        """Cancel active operations and prevent queued operations from starting."""
        with self._lock:
            self._stopping = True
            active = list(self._active)
        for cancellable in active:
            cancellable.cancel()

    def resume(self):
        with self._lock:
            self._stopping = False

    @contextmanager
    def response(self, url, data=None, method='POST', timeout=10, cancellable=None,
                 prepare=None, explain=None):
        """Yield (stream, cancellable) for a successful response.

        `url` and `data` may be callables, evaluated once the request is tracked.
        `prepare(message, cancellable)` may add headers and returns a secret to
        redact from errors. `explain(status, detail)` may replace an error detail.
        """
        cancellable = cancellable if cancellable is not None else Gio.Cancellable()
        with self._lock:
            if self._stopping or cancellable.is_cancelled():
                raise RequestCancelled(_('Request stopped'))
            self._active.add(cancellable)
        session = Soup.Session(timeout=timeout)
        stream = None
        secret = None
        try:
            message = Soup.Message.new(method, url() if callable(url) else url)
            if prepare is not None:
                secret = prepare(message, cancellable)
            if callable(data):
                data = data()
            if data is not None:
                body = json.dumps(data, allow_nan=False).encode('utf-8')
                message.set_request_body_from_bytes('application/json', GLib.Bytes.new(body))
            stream = session.send(message, cancellable)
            if not 200 <= message.get_status() < 300:
                body = read_all(stream, cancellable)
                detail = None
                try:
                    decoded = json.loads(body)
                    if isinstance(decoded, dict):
                        detail = decoded.get('error')
                except (ValueError, UnicodeError):
                    pass
                status = message.get_status()
                if explain is not None:
                    detail = explain(status, detail)
                raise ProviderError(str(detail or f'HTTP Error {status}: {message.get_reason_phrase()}'), status)
            yield stream, cancellable
        except RequestCancelled:
            raise
        except ProviderError as exc:
            if secret:
                raise ProviderError(str(exc).replace(secret, '[redacted]'), exc.status) from None
            raise
        except GLib.Error as exc:
            if exc.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                raise RequestCancelled(_('Request stopped')) from exc
            raise ProviderError(str(exc).replace(secret, '[redacted]') if secret else str(exc)) from None
        finally:
            if stream is not None:
                try:
                    stream.close(None)
                except GLib.Error:
                    pass
            session.abort()
            with self._lock:
                self._active.discard(cancellable)

    def request(self, url, data=None, method='GET', timeout=10, cancellable=None, **hooks):
        with self.response(url, data, method, timeout, cancellable, **hooks) as (stream, cancel):
            raw = read_all(stream, cancel)
            result = json_object(raw) if raw.strip() else {}
            if 'error' in result:
                raise ProviderError(str(result['error']))
            return result

    def stream_lines(self, url, data, timeout=300, cancellable=None, decode=None, **hooks):
        """Yield the nonblank lines of a streamed response body, decoded if asked.

        Decoding happens inside the request, so its errors are redacted too.
        """
        decode = decode or (lambda line: line)
        with self.response(url, data, 'POST', timeout, cancellable, **hooks) as (stream, cancel):
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

    def stream_json(self, url, data, timeout=300, cancellable=None, **hooks):
        """Yield the objects of a newline-delimited JSON response."""
        def decode(line):
            chunk = json_object(line)
            if 'error' in chunk:
                raise ProviderError(str(chunk['error']))
            return chunk
        return self.stream_lines(url, data, timeout, cancellable, decode, **hooks)
