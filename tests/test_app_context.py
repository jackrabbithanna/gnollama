"""AppContext owns the workers, the transport, and the order of shutdown."""
import tempfile
import threading
import time
import unittest

from gi.repository import GLib

from src.app_context import AppContext
from src.providers import RequestCancelled
from test_transport import Server


def pump_until(predicate, timeout=4):
    deadline = time.monotonic() + timeout
    context = GLib.MainContext.default()
    while time.monotonic() < deadline:
        while context.pending():
            context.iteration(False)
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError('Timed out waiting for background work')


class AppContextTests(unittest.TestCase):
    def open(self):
        temp = tempfile.TemporaryDirectory(prefix='gnollama-context-')
        self.addCleanup(temp.cleanup)
        context = AppContext(temp.name)

        def close():
            if not context.storage.writer.closed:
                pump_until(lambda: context.idle and context.storage.writer.idle)
                context.shutdown(wait=True)
        self.addCleanup(close)
        return context

    def server(self, mode='stream'):
        fixture = Server(mode)
        self.addCleanup(fixture.close)
        return fixture

    def stalled(self, context, fixture):
        """Start a streamed request that stays open; return its outcome list."""
        outcome = []
        provider = context.providers.local(fixture.host)

        def read():
            try:
                outcome.append(list(provider.generate('test', 'hello')))
            except Exception as exc:
                outcome.append(exc)
        context.services.inference.submit(read)
        self.assertTrue(fixture.server.started.wait(3))
        return outcome

    def test_contexts_share_no_workers_and_cancel_separately(self):
        first, second = self.open(), self.open()
        for name in ('storage', 'services', 'transport', 'providers', 'knowledge', 'credentials'):
            self.assertIsNot(getattr(first, name), getattr(second, name), name)
        self.assertIsNot(first.services.inference, second.services.inference)
        self.assertIs(first.connection(first.storage.get_all_hosts()[0]).transport, first.transport)

        slow, other = self.server('stall'), self.server('stall')
        stopped, running = self.stalled(first, slow), self.stalled(second, other)
        first.cancel_requests()
        pump_until(lambda: bool(stopped))
        self.assertIsInstance(stopped[0], RequestCancelled)
        self.assertFalse(running, 'the other context keeps its request')
        with self.assertRaises(RequestCancelled):
            first.providers.local(slow.host).version()
        self.assertTrue(first.knowledge.closed)
        self.assertFalse(second.knowledge.closed)

        other.server.release.set()
        pump_until(lambda: bool(running))
        self.assertEqual(running[0], [{'response': 'partial'}])

        first.resume_requests()
        self.assertFalse(first.knowledge.closed)
        ready = self.server('json')
        ready.server.payload = {'version': '1.0'}
        self.assertEqual(first.providers.local(ready.host).version(), '1.0')

    def test_shutdown_stops_active_inference(self):
        context = self.open()
        outcome = self.stalled(context, self.server('stall'))
        self.assertFalse(context.idle)
        context.cancel_requests()
        pump_until(lambda: context.idle)
        self.assertIsInstance(outcome[0], RequestCancelled)
        context.shutdown(wait=True)
        self.assertTrue(context.storage.writer.closed)

    def test_shutdown_refuses_while_writes_are_pending(self):
        context = self.open()
        release, called = threading.Event(), []
        context.storage.submit(release.wait, 5)
        context.storage.after_pending_writes(lambda: called.append(True))
        with self.assertRaisesRegex(RuntimeError, 'unsaved'):
            context.shutdown()
        self.assertFalse(context.storage.writer.closed)
        release.set()
        pump_until(lambda: bool(called) and context.idle and context.storage.writer.idle)
        context.shutdown(wait=True)

    def test_failed_write_stays_queued_until_retried(self):
        context = self.open()
        errors, attempts, saved = [], [], []
        context.storage.on_error = errors.append

        def write():
            attempts.append(True)
            if len(attempts) == 1:
                raise OSError('disk full')
        context.storage.submit(write, on_done=lambda: saved.append(True))
        chat = context.storage.create_chat('queued behind the failure')
        pump_until(lambda: bool(errors))
        self.assertEqual(str(errors[0]), 'disk full')
        with self.assertRaisesRegex(RuntimeError, 'unsaved'):
            context.shutdown()
        self.assertIsNone(context.storage.get_chat(chat['id']))
        context.storage.writer.retry()
        pump_until(lambda: bool(saved) and context.storage.writer.idle and context.idle)
        self.assertEqual(len(attempts), 2)
        self.assertIsNotNone(context.storage.get_chat(chat['id']))
        context.shutdown(wait=True)


if __name__ == '__main__':
    unittest.main()
