import tempfile
import threading
import unittest
import uuid
from unittest.mock import patch
from gi.repository import Adw, Gdk, Gio
from src import startup
from src.database import DatabaseUpgradeError
from src.storage import ChatStorage
from test_ui import pump_until


@unittest.skipUnless(Gdk.Display.get_default(), 'GTK smoke tests require a display')
class StartupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = Adw.Application(application_id='io.github.jackrabbithanna.Gnollama.Test' + uuid.uuid4().hex,
                                   flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.app.register(None)
        self.window = None
        self.storage = None

    def tearDown(self):
        if self.window:
            self.window.destroy()
        if self.storage and not self.storage.writer._closed:
            startup.StartupWindow.dispose_storage(self.storage)
        self.temp.cleanup()

    def test_open_happens_off_main_thread_and_reports_progress(self):
        started, release = threading.Event(), threading.Event()
        main_thread = threading.get_ident()
        def opening(progress):
            self.assertNotEqual(threading.get_ident(), main_thread)
            progress('Checking saved vectors')
            started.set()
            release.wait(4)
            self.storage = ChatStorage(self.temp.name, progress=progress)
            return self.storage
        ready = []
        with patch.object(startup, 'ChatStorage', side_effect=opening):
            self.window = startup.StartupWindow(self.app, ready.append)
            self.window.present()
            try:
                pump_until(lambda: started.is_set() and self.window.page.get_description() == 'Checking saved vectors')
                self.assertTrue(self.window.busy)
                self.assertEqual(ready, [])
            finally:
                release.set()
            pump_until(lambda: not self.window.busy)
        self.assertEqual(ready, [self.storage])

    def test_error_shows_backup_and_retry_does_not_replace_database(self):
        ready = []
        def opening(progress):
            self.storage = ChatStorage(self.temp.name, progress=progress)
            return self.storage
        with patch.object(startup, 'ChatStorage', side_effect=DatabaseUpgradeError('disk full', '/tmp/recovery.bak')):
            self.window = startup.StartupWindow(self.app, ready.append)
            self.window.present()
            pump_until(lambda: not self.window.busy)
        self.assertTrue(self.window.retry.get_visible())
        self.assertIn('/tmp/recovery.bak', self.window.page.get_description())
        self.assertEqual(ready, [])
        with patch.object(startup, 'ChatStorage', side_effect=opening):
            self.window.retry.emit('clicked')
            pump_until(lambda: not self.window.busy)
        self.assertEqual(ready, [self.storage])

    def test_close_during_upgrade_waits_and_disposes_storage(self):
        started, release = threading.Event(), threading.Event()
        def opening(progress):
            started.set()
            release.wait(4)
            self.storage = ChatStorage(self.temp.name, progress=progress)
            return self.storage
        ready = []
        with patch.object(startup, 'ChatStorage', side_effect=opening):
            self.window = startup.StartupWindow(self.app, ready.append)
            self.window.present()
            try:
                pump_until(started.is_set)
                self.window.close()
                self.assertTrue(self.window.closing)
                self.assertTrue(self.window.busy)
            finally:
                release.set()
            pump_until(lambda: not self.window.busy)
        self.assertEqual(ready, [])
        self.assertTrue(self.storage.writer._closed)
        self.assertTrue(self.storage.knowledge.closed)
