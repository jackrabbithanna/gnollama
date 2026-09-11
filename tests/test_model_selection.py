"""Discovery latency, cancellation, cache freshness, and selector recovery."""
import threading
import unittest
from unittest.mock import patch
from gi.repository import Gdk, Gio, Gtk
from src import ollama, session
from src.widgets.chat_input import ChatInput
import test_ui
import test_workspace
from test_ui import pump_until


class CatalogTests(unittest.TestCase):
    setUp = test_workspace.WorkspaceTests.setUp
    tearDown = test_workspace.WorkspaceTests.tearDown

    def test_shared_lists_leave_workers_available_for_other_hosts_and_control_reads(self):
        catalog = self.storage.services.catalog
        started, release = threading.Event(), threading.Event()
        def fetch(host, **kwargs):
            if host == 'http://slow':
                started.set()
                release.wait(3)
            return ['model']
        with patch.object(ollama, 'fetch_models', side_effect=fetch) as tags:
            try:
                futures = [catalog.request_models('http://slow', refresh=True) for _ in range(4)]
                self.assertTrue(started.wait(1))
                self.assertEqual(catalog.models('http://fast')[0][0], 'model')
                self.assertEqual(self.storage.services.control.submit(lambda: 'responsive').result(1), 'responsive')
                self.assertEqual(tags.call_count, 2)
            finally:
                release.set()
            for future in futures:
                self.assertEqual(future.result(1)[0]['name'], 'model')

    def test_new_subscriber_does_not_join_a_cancelled_request(self):
        catalog = self.storage.services.catalog
        started, release = threading.Event(), threading.Event()
        cancel = Gio.Cancellable()
        calls = []
        def fetch(*args, **kwargs):
            calls.append(True)
            if len(calls) == 1:
                started.set()
                release.wait(3)
                return ['stale']
            return ['fresh']
        with patch.object(ollama, 'fetch_models', side_effect=fetch):
            old = catalog.request_models('http://models', cancel)
            try:
                self.assertTrue(started.wait(1))
                cancel.cancel()
                self.assertEqual(catalog.models('http://models')[0][0], 'fresh')
            finally:
                release.set()
            with self.assertRaises(ollama.RequestCancelled):
                old.result(1)
            self.assertEqual(catalog.models('http://models')[0][0], 'fresh')

    def test_empty_lists_are_not_cached_and_explicit_refresh_bypasses_cached_names(self):
        catalog = self.storage.services.catalog
        with patch.object(ollama, 'fetch_models', side_effect=[[], ['first'], ['second']]) as tags:
            self.assertEqual(catalog.models('http://models'), [])
            self.assertEqual(catalog.models('http://models')[0][0], 'first')
            self.assertEqual(catalog.models('http://models')[0][0], 'first')
            self.assertEqual(catalog.models('http://models', refresh=True)[0][0], 'second')
            self.assertEqual(tags.call_count, 3)

    def test_capabilities_are_shared_and_expire_by_time_or_digest(self):
        catalog = self.storage.services.catalog
        clock = [0]
        with patch('src.services.time.monotonic', side_effect=lambda: clock[0]), \
                patch.object(ollama, 'fetch_models', side_effect=[[dict(name='model', digest='one')], [dict(name='model', digest='two')]]), \
                patch.object(ollama, 'show_model', return_value={'capabilities': ['completion']}) as show:
            catalog.models('http://models')
            clock[0] = 59
            catalog.details('http://models', 'model')
            catalog.details('http://models', 'model')
            self.assertEqual(show.call_count, 1)
            clock[0] = 61
            catalog.models('http://models')
            self.assertIsNone(catalog.cached_details('http://models', 'model'))
            catalog.details('http://models', 'model')
            self.assertEqual(show.call_count, 2)
            clock[0] = 122
            catalog.details('http://models', 'model')
            self.assertEqual(show.call_count, 3)


@unittest.skipUnless(Gdk.Display.get_default(), 'GTK tests require a display')
class SelectorTests(unittest.TestCase):
    setUp = test_ui.UITests.setUp
    tearDown = test_ui.UITests.tearDown
    make_window = test_ui.UITests.make_window

    def test_names_appear_before_capabilities_and_unselected_models_are_not_probed(self):
        widget = ChatInput()
        widget.services = self.storage.services
        started, release = threading.Event(), threading.Event()
        def show(*args, **kwargs):
            started.set()
            release.wait(3)
            return {'capabilities': ['completion']}
        with patch.object(ollama, 'fetch_models', return_value=['model-' + str(i) for i in range(100)]), \
                patch.object(ollama, 'show_model', side_effect=show) as details:
            try:
                widget.fetch_models('http://models')
                pump_until(lambda: widget.model_dropdown.get_model().get_n_items() == 100)
                self.assertTrue(started.wait(1))
                self.assertTrue(widget.capabilities_loading)
                self.assertFalse(widget.send_button.get_sensitive())
                self.assertEqual(details.call_count, 1)
            finally:
                release.set()
            pump_until(lambda: not widget.capabilities_loading and session.worker.idle)
            widget.cancel_fetches()

    def test_empty_selector_retries_on_mouse_and_keyboard_and_retains_saved_selection(self):
        for interaction in ('mouse', 'keyboard'):
            with self.subTest(interaction=interaction):
                widget = ChatInput()
                widget.services = self.storage.services
                widget.pending_model_selection = 'saved'
                with patch.object(ollama, 'fetch_models', side_effect=[[], ['first', 'saved']]) as tags:
                    widget.fetch_models('http://' + interaction)
                    pump_until(lambda: not widget._models_loading)
                    controllers = widget.model_dropdown.observe_controllers()
                    for i in range(controllers.get_n_items()):
                        controller = controllers.get_item(i)
                        if interaction == 'mouse' and isinstance(controller, Gtk.GestureClick):
                            controller.emit('pressed', 1, 2., 2.)
                        elif interaction == 'keyboard' and isinstance(controller, Gtk.EventControllerKey):
                            controller.emit('key-pressed', Gdk.KEY_Return, 0, Gdk.ModifierType(0))
                    pump_until(lambda: widget.get_selected_model() == 'saved' and session.worker.idle)
                    self.assertEqual(tags.call_count, 2)
                    widget.cancel_fetches()

    def test_comparison_targets_share_discovery_and_refresh_edited_host_connections(self):
        window, original = self.make_window()
        self.storage.services.catalog.invalidate()
        with patch.object(ollama, 'fetch_models', return_value=['one', 'two']) as tags:
            tab = window.new_comparison_tab()
            self.tabs.append(tab)
            tab.add_target()
            tab.add_target()
            pump_until(lambda: all(p.input.get_selected_model() for p in tab.targets) and session.worker.idle)
            self.assertEqual(tags.call_count, 1)
            tab.targets[1].input.select_model('two')
            host = self.storage.get_all_hosts()[0]
            self.storage.update_host(host['id'], host['name'], 'http://changed:11434', True)
            tab.update_hosts()
            pump_until(lambda: all(p.input._host == 'http://changed:11434' for p in tab.targets) and session.worker.idle)
            self.assertEqual(tab.targets[1].input.get_selected_model(), 'two')
            self.assertEqual(tags.call_count, 2)
            self.assertIsNone(tab.chat_input._host)

    def test_open_selector_does_not_keep_capabilities_past_catalog_expiry(self):
        import time
        widget = ChatInput()
        widget.services = self.storage.services
        capabilities = ['completion', 'vision']
        with patch.object(ollama, 'fetch_models', return_value=['one', 'two']), \
                patch.object(ollama, 'show_model', side_effect=lambda *a, **kw: {'capabilities': list(capabilities)}) as show:
            widget.fetch_models('http://models')
            pump_until(lambda: widget.get_selected_model() == 'one' and not widget.capabilities_loading)
            self.assertTrue(widget.image_support)
            capabilities.remove('vision')
            real_clock = time.monotonic
            with patch('src.services.time.monotonic', side_effect=lambda: real_clock() + 61):
                widget.select_model('two')
                pump_until(lambda: not widget.capabilities_loading)
                widget.select_model('one')
                pump_until(lambda: not widget.capabilities_loading)
                self.assertFalse(widget.image_support)
                self.assertEqual(show.call_count, 3)
            widget.cancel_fetches()
