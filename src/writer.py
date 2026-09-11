"""Ordered database jobs that remain retryable after a write failure."""
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from threading import Lock


class OrderedWriter:
    def __init__(self, on_error=None):
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='GnollamaStorage')
        self._lock = Lock()
        self._jobs = deque()
        self._running = False
        self._closed = False
        self.error = None
        self.on_error = on_error

    def submit(self, fn, *args, **kwargs):
        future = Future()
        with self._lock:
            if self._closed:
                raise RuntimeError('Storage is closed')
            self._jobs.append([fn, args, kwargs, future])
            self._start()
        return future

    def _start(self):
        # Called with the lock held.
        if self._jobs and not self._running and self.error is None:
            self._running = True
            self._executor.submit(self._drain)

    def _drain(self):
        while True:
            with self._lock:
                if not self._jobs:
                    self._running = False
                    return
                fn, args, kwargs, future = self._jobs[0]
            try:
                result = fn(*args, **kwargs)
            except Exception as exc:
                with self._lock:
                    self.error = exc
                    self._running = False
                future.set_exception(exc)
                if self.on_error:
                    self.on_error(exc)
                return
            with self._lock:
                self._jobs.popleft()
            future.set_result(result)

    @property
    def closed(self):
        with self._lock:
            return self._closed

    @property
    def idle(self):
        with self._lock:
            return not self._jobs and not self._running

    def retry(self):
        with self._lock:
            if self.error is not None:
                self.error = None
                self._jobs[0][3] = Future()
                self._start()

    def flush(self):
        return self.submit(lambda: None)

    def shutdown(self):
        with self._lock:
            if self._jobs or self._running:
                raise RuntimeError('Storage still has unsaved changes')
            self._closed = True
        self._executor.shutdown(wait=False)
