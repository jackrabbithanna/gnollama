"""Application-owned scheduling, model reservations, and discovery caches."""
import copy
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from gettext import gettext as _

from gi.repository import Gio
from .providers import RequestCancelled, validate_url


def host_key(host):
    """Identify a server by provider type and address, from a host record or a connection.

    Records of one type at one address are one server. Another type there is
    not: it lists other models and describes them differently.
    """
    kind, url = (host.get('provider'), host['hostname']) if isinstance(host, dict) else (host.kind, host.url)
    return (kind or 'ollama', validate_url(url))


def model_key(host, model):
    return (host_key(host), model.removesuffix(':latest'))


class WorkerPool:
    def __init__(self, count=2, name='Control'):
        self.executor = ThreadPoolExecutor(max_workers=count, thread_name_prefix='Tureen' + name)
        self._lock = threading.Lock()
        self._futures = set()
        self.closed = False

    def submit(self, fn, *args, **kwargs):
        with self._lock:
            future = self.executor.submit(fn, *args, **kwargs)
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
        self.closed = True
        self.executor.shutdown(wait=wait)


class ModelUseRegistry:
    def __init__(self):
        self.lock = threading.RLock()
        self.reserved = set()
        self.active = {}

    def busy(self, host, model):
        with self.lock:
            return bool(self.active.get(model_key(host, model)))

    def reserve(self, host, model, busy=lambda *args: False):
        key = model_key(host, model)
        with self.lock:
            if key in self.reserved or self.active.get(key) or busy(host, model):
                return False
            self.reserved.add(key)
            return True

    def release(self, host, model):
        with self.lock:
            self.reserved.discard(model_key(host, model))

    @contextmanager
    def using(self, host, model):
        key = model_key(host, model)
        with self.lock:
            if key in self.reserved:
                raise ValueError(_('Wait for the model operation to finish.'))
            self.active[key] = self.active.get(key, 0) + 1
        try:
            yield
        finally:
            with self.lock:
                self.active[key] -= 1
                if not self.active[key]:
                    del self.active[key]


@dataclass(frozen=True)
class ModelIdentity:
    host: str
    name: str
    digest: str = ''


# The kinds of model list a catalog shares, and the provider operation that reads each.
LISTS = {'models': 'list_models', 'decision_models': 'list_decision_models', 'image_models': 'list_image_models'}


class ModelCatalog:
    """Share discovery requests without occupying workers for each subscriber."""
    def __init__(self, discovery, control):
        self.discovery, self.control = discovery, control
        self._condition = threading.RLock()
        self._cache = {}
        self._pending = {}
        self._epoch = 0
        self._versions = {}
        self._digests = {}

    @staticmethod
    def connection_key(connection):
        # Hosts of one type at one address still differ in the key they send.
        return (*host_key(connection), connection.host_id, connection.credential_id)

    def _generation(self, key):
        return self._epoch, self._versions.get(key, 0)

    def invalidate(self, connection=None):
        with self._condition:
            if connection is None:
                self._epoch += 1
                self._cache.clear()
                self._digests.clear()
            else:
                key = self.connection_key(connection)
                self._versions[key] = self._versions.get(key, 0) + 1
                self._cache = {k: v for k, v in self._cache.items() if k[0] != key}
                self._digests = {k: v for k, v in self._digests.items() if k[0] != key}

    def _request(self, connection, kind, cancel, name=None, refresh=False):
        key = self.connection_key(connection)
        future: Future = Future()
        with self._condition:
            if cancel is not None and cancel.is_cancelled():
                future.set_exception(RequestCancelled())
                return future
            if refresh:
                current = self._pending.get(((key, kind, name, ''), self._generation(key)))
                if current is None or current['cancel'].is_cancelled():
                    self.invalidate(connection)
            digest = self._digests.get((key, name), '')
            cache_key = (key, kind, name, digest)
            cached = self._cache.get(cache_key)
            if cached and time.monotonic() - cached[0] < 60:
                future.set_result(copy.deepcopy(cached[1]))
                return future
            generation = self._generation(key)
            pending_key = (cache_key, generation)
            job: Any = self._pending.get(pending_key)
            leader = job is None or job['cancel'].is_cancelled()
            if leader:
                job = dict(cancel=Gio.Cancellable(), members=[], done=False)
                self._pending[pending_key] = job
            member = dict(future=future, cancel=cancel, handler=None)
            job['members'].append(member)

        def cancelled(*args):
            with self._condition:
                if not future.done():
                    future.set_exception(RequestCancelled())
                if all(m['future'].done() for m in job['members']):
                    job['cancel'].cancel()
        if cancel is not None:
            with self._condition:
                if not job['done']:
                    member['handler'] = cancel.connect(cancelled)

        def fetch():
            result, error = None, None
            try:
                if job['cancel'].is_cancelled():
                    raise RequestCancelled()
                if kind in LISTS:
                    tags = getattr(connection, LISTS[kind])(cancellable=job['cancel'])
                    result = []
                    for tag in tags:
                        tag = dict(tag) if isinstance(tag, dict) else dict(name=tag)
                        if isinstance(tag.get('name'), str) and tag['name']:
                            result.append(tag)
                else:
                    result = connection.show_model(name, cancellable=job['cancel'])
            except Exception as exc:
                error = exc
            with self._condition:
                job['done'] = True
                if self._pending.get(pending_key) is job:
                    self._pending.pop(pending_key)
                if error is None and not job['cancel'].is_cancelled() and generation == self._generation(key):
                    # An empty list must be retryable immediately after a model is pulled.
                    if result or kind not in LISTS:
                        self._cache[cache_key] = (time.monotonic(), copy.deepcopy(result))
                    if kind in LISTS:
                        for tag in result:
                            self._digests[(key, tag['name'])] = tag.get('digest', '')
                members = list(job['members'])
            for subscriber in members:
                signal = subscriber['handler']
                if signal:
                    subscriber['cancel'].disconnect(signal)
                with self._condition:
                    target = subscriber['future']
                    if not target.done():
                        if subscriber['cancel'] is not None and subscriber['cancel'].is_cancelled():
                            target.set_exception(RequestCancelled())
                        elif error is not None:
                            target.set_exception(error)
                        else:
                            target.set_result(copy.deepcopy(result))

        if leader:
            (self.discovery if kind in LISTS else self.control).submit(fetch)
        return future

    def request_models(self, connection, cancel=None, refresh=False):
        return self._request(connection, 'models', cancel, refresh=refresh)

    def request_decision_models(self, connection, cancel=None, refresh=False):
        """The decision models of a host, each entry saying whether it takes images; shared and cached as the chat list is."""
        return self._request(connection, 'decision_models', cancel, refresh=refresh)

    def request_image_models(self, connection, cancel=None, refresh=False):
        """The image models of a host, each entry with the settings it takes; shared and cached as the chat list is."""
        return self._request(connection, 'image_models', cancel, refresh=refresh)

    def cached_details(self, connection, name):
        key = self.connection_key(connection)
        with self._condition:
            cache_key = (key, 'details', name, self._digests.get((key, name), ''))
            cached = self._cache.get(cache_key)
            return copy.deepcopy(cached[1]) if cached and time.monotonic() - cached[0] < 60 else None

    def models(self, connection, cancel=None, refresh=False):
        tags = self.request_models(connection, cancel, refresh).result()
        return [(tag['name'], self.cached_details(connection, tag['name'])) for tag in tags]

    def request_details(self, connection, name, cancel=None):
        return self._request(connection, 'details', cancel, name)

    def details(self, connection, name, cancel=None):
        return self.request_details(connection, name, cancel).result()

    def cancel_all(self):
        with self._condition:
            jobs = list(self._pending.values())
        for job in jobs:
            job['cancel'].cancel()


class Services:

    def __init__(self):
        self.inference = WorkerPool(4, 'Inference')
        self.control = WorkerPool(2, 'Control')
        self.transfer = WorkerPool(2, 'Transfer')
        self.discovery = WorkerPool(2, 'Discovery')
        self.models = ModelUseRegistry()
        self.catalog = ModelCatalog(self.discovery, self.control)

    @property
    def idle(self):
        return all(pool.idle for pool in (self.inference, self.control, self.transfer, self.discovery))

    def shutdown(self, wait=True):
        self.catalog.cancel_all()
        for pool in (self.inference, self.control, self.transfer, self.discovery):
            pool.shutdown(wait)

