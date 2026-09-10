"""Collection import journeys, settings dialogs, and source-selection behavior."""
import copy
from contextlib import ExitStack
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from gi.repository import Adw, Gdk, Gio, Gtk
from src import ollama, session
from src.knowledge import DEFAULT_RAG, make_document, new_config
from src.storage import ChatStorage
from src.widgets.knowledge_import import FileImportDialog, TextImportDialog
from src.widgets.knowledge_view import CollectionDestination, CollectionDialog, KnowledgeView, SourcePicker, WorkDialog
from src.widgets.url_import import URLImportDialog
from src.widgets.json_view import buffer_text
from src.host_manager import HostEditDialog
from src.tab import GenerationTab
from test_collections import collection
from test_knowledge import TAG
from test_ui import pump_until


@unittest.skipUnless(Gdk.Display.get_default(), 'GTK requires a display')
class UsabilityTests(unittest.TestCase):
    def setUp(self):
        ollama.resume()
        self.temp = tempfile.TemporaryDirectory()
        self.storage = ChatStorage(self.temp.name)
        self.storage.add_host('Embeddings', 'http://embed:11434')
        self.config = new_config(TAG['name'], TAG['digest'])
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(ollama, 'fetch_model_details', return_value=[TAG]))
        self.stack.enter_context(patch.object(ollama, 'fetch_models', return_value=['chat-test']))
        self.stack.enter_context(patch.object(ollama, 'show_model', return_value={'capabilities': ['completion', 'embedding']}))
        self.stack.enter_context(patch.object(ollama, 'embed', side_effect=lambda host, model, inputs, **kw:
            {'embeddings': [[1., 0.] for _ in ([inputs] if isinstance(inputs, str) else inputs)]}))
        self.window = Adw.Window(default_width=400, default_height=800)
        self.window.set_content(Gtk.Box())
        self.window.present()
        self.dialogs = []
        self.views = []
        self.tabs = []

    def drain(self):
        pump_until(lambda: self.storage.knowledge.idle and self.storage.writer.idle and session.worker.idle, timeout=10)

    def present(self, dialog):
        self.dialogs.append(dialog)
        dialog.present(self.window)
        return dialog

    def tearDown(self):
        for dialog in self.dialogs:
            if dialog.get_mapped() and not getattr(dialog, 'closed', False):
                dialog.close()
        for view in self.views:
            view._destroyed()
        for tab in self.tabs:
            tab.chat_input.cancel_fetches()
            tab.knowledge_control.close_dialog(dispose=True)
            if tab.options_panel._settings_dialog:
                tab.options_panel._settings_dialog.close()
        self.storage.knowledge.cancel_all()
        self.drain()
        self.window.destroy()
        self.storage.knowledge.shutdown()
        self.storage.writer.shutdown()
        self.stack.close()
        self.temp.cleanup()

    def test_text_import_requires_explicit_destination_and_preserves_draft(self):
        collection(self.storage.db, 'Manuals', self.config)
        dialog = self.present(TextImportDialog(self.storage))
        dialog.title_entry.set_text('Getting started')
        dialog.editor.get_buffer().set_text('Install Gnollama using GNOME Builder.')
        dialog.apply_text()
        self.assertIn('destination collection', dialog.error_label.get_text())
        self.assertEqual(self.storage.db.knowledge_documents(), [])
        self.assertIn('GNOME Builder', buffer_text(dialog.editor))
        dialog.destination.refresh('Manuals')
        dialog.apply_text()
        pump_until(lambda: dialog.closed)
        self.drain()
        group = next(c for c in self.storage.db.knowledge_collections() if c['id'] == 'Manuals')
        self.assertEqual((group['documents'], group['ready']), (1, 1))
        self.assertEqual(self.storage.db.collection_documents('Manuals')[0]['title'], 'Getting started')

    def test_destination_creation_and_all_imports_share_selected_collection(self):
        view = KnowledgeView(self.storage)
        self.views.append(view)
        self.assertEqual(view.library_scope, 'collections')
        dialog = self.present(TextImportDialog(self.storage))
        with patch.object(CollectionDialog, 'present') as presented:
            dialog.destination.new_collection()
        self.assertTrue(presented.called)
        # Exercise the creation callback without depending on a dialog animation.
        done = []
        self.storage.knowledge.create_collection('Created while importing', 'http://embed:11434', TAG['name'], self.config,
            callback=lambda id, error: (done.append((id, error)), dialog.destination.refresh(id)))
        pump_until(lambda: bool(done))
        self.assertIsNone(done[0][1])
        id = done[0][0]
        self.assertEqual(dialog.destination.require()['id'], id)
        files = self.present(FileImportDialog(self.storage, id))
        urls = self.present(URLImportDialog(self.storage, id))
        self.assertEqual(files.destination.require()['id'], id)
        self.assertEqual(urls.destination_picker.require()['id'], id)
        self.assertFalse(urls.selector_expander.get_expanded())
        dialog.close()
        files.close()
        urls.close()

    def test_file_queue_reviews_one_at_a_time_and_recovers_from_bad_file(self):
        collection(self.storage.db, 'Files', self.config)
        files = []
        for name, content in [('guide.md', b'# Guide\nFirst useful document.'), ('bad.txt', b'\xff\xfe\xff'), ('notes.py', b'print("Keep this source code")')]:
            path = Path(self.temp.name) / name
            path.write_bytes(content)
            files.append(Gio.File.new_for_path(str(path)))
        dialog = self.present(FileImportDialog(self.storage, 'Files'))
        dialog.start_files(files)
        pump_until(lambda: dialog.items[0]['status'] == 'ready')
        self.assertEqual([i['status'] for i in dialog.items], ['ready', 'queued', 'queued'])
        self.assertEqual(self.storage.db.knowledge_documents(), [])
        dialog.title_entry.set_text('Renamed guide')
        dialog.save()
        pump_until(lambda: dialog.items[1]['status'] == 'failed')
        self.assertEqual(len(self.storage.db.knowledge_documents()), 1)
        dialog.skip()
        pump_until(lambda: dialog.items[2]['status'] == 'ready')
        dialog.save()
        pump_until(lambda: dialog.current is None)
        self.drain()
        dialog.update_saved()
        self.assertEqual([i['status'] for i in dialog.items], ['saved', 'skipped', 'saved'])
        self.assertEqual(next(c for c in self.storage.db.knowledge_collections() if c['id'] == 'Files')['ready'], 2)
        self.assertIn('Ready to Search', dialog.items[0]['row'].get_text())
        self.assertIsNone(dialog.items[0]['document'])

    def test_closing_file_review_cancels_extraction_without_saving(self):
        collection(self.storage.db, 'Files', self.config)
        path = Path(self.temp.name) / 'draft.txt'
        path.write_text('Unsaved document')
        entered, release = threading.Event(), threading.Event()
        from src.widgets import knowledge_import
        real = knowledge_import.extract_document
        def slow(filename, raw, cancel):
            entered.set()
            release.wait(5)
            return real(filename, raw, cancel)
        with patch.object(knowledge_import, 'extract_document', side_effect=slow):
            dialog = self.present(FileImportDialog(self.storage, 'Files'))
            dialog.start_files([Gio.File.new_for_path(str(path))])
            pump_until(entered.is_set)
            dialog.close()
            pump_until(lambda: dialog.closed)
            self.assertTrue(dialog.job['cancel'].is_cancelled())
            release.set()
            self.drain()
        self.assertEqual(self.storage.db.knowledge_documents(), [])

    def test_import_reuses_vectors_and_deleted_destination_does_not_save(self):
        collection(self.storage.db, 'A', self.config)
        collection(self.storage.db, 'B', self.config)
        doc = make_document('Original', 'Content shared by two collections')
        results = []
        self.storage.knowledge.import_document(doc, 'A', lambda r, e: results.append((r, e)))
        self.drain()
        indexes = self.storage.db.knowledge_indexes()
        self.storage.knowledge.import_document(make_document('Duplicate', doc['text']), 'B')
        self.drain()
        self.assertEqual(len(self.storage.db.knowledge_documents()), 1)
        self.assertEqual(self.storage.db.knowledge_indexes(), indexes)
        self.assertEqual(next(c for c in self.storage.db.knowledge_collections() if c['id'] == 'B')['ready'], 1)
        self.storage.db.delete_knowledge_collection('B')
        job = self.storage.knowledge.import_document(make_document('New', 'Different unsaved content'), 'B')
        self.drain()
        self.assertIn('deleted', job['error'])
        self.assertEqual(len(self.storage.db.knowledge_documents()), 1)
        self.assertIsNone(self.storage.writer.error)

    def test_failed_preparation_preserves_source_and_membership(self):
        collection(self.storage.db, 'Failures', self.config)
        with patch.object(ollama, 'embed', side_effect=ollama.OllamaError('Embedding server unavailable')):
            self.storage.knowledge.import_document(make_document('Saved source', 'Retry this document after reconnecting.'), 'Failures')
            self.drain()
        members = self.storage.db.collection_documents('Failures')
        self.assertEqual(len(members), 1)
        self.assertEqual(members[0]['status'], 'failed')
        self.storage.knowledge.build_collection('Failures')
        self.drain()
        self.assertEqual(next(c for c in self.storage.db.knowledge_collections() if c['id'] == 'Failures')['ready'], 1)

    def test_first_collection_selects_configuration_and_keeps_focused_rows(self):
        collection(self.storage.db, 'A', self.config)
        other = new_config(TAG['name'], TAG['digest'], 'custom', query_prefix='Find: ')
        collection(self.storage.db, 'B', other)
        picker = self.present(SourcePicker(self.storage, {}, lambda r: None))
        a = picker.collection_checks['A'][0]
        b = picker.collection_checks['B'][0]
        self.assertTrue(a.get_sensitive())
        self.assertTrue(b.get_sensitive())
        b.set_active(True)
        self.assertEqual(picker.config()['id'], other['id'])
        self.assertIs(picker.collection_checks['B'][0], b)
        self.assertFalse(a.get_sensitive())
        self.assertIn('different embedding', a.get_child().get_text())
        b.set_active(False)
        self.assertTrue(a.get_sensitive())
        a.set_active(True)
        pump_until(lambda: picker.host_models.model() is not None)
        self.assertEqual(picker.current_options()['collection_ids'], ['A'])
        self.assertFalse(picker.settings_expander.get_expanded())

    def test_chat_settings_keep_layout_and_validate_fields_inline(self):
        tab = GenerationTab(mode='chat', storage=self.storage)
        self.tabs.append(tab)
        self.window.set_content(tab)
        panel = tab.options_panel
        pump_until(lambda: panel.get_mapped() and session.worker.idle and not tab.chat_input.capabilities_loading)
        end = time.monotonic() + .15
        pump_until(lambda: time.monotonic() >= end)
        height = tab.message_list.get_height()
        panel.open_settings()
        pump_until(lambda: panel._settings_dialog.get_mapped())
        self.assertEqual(tab.message_list.get_height(), height)
        panel.temperature_entry.set_text('wrong')
        panel._settings_done()
        self.assertIsNotNone(panel._settings_dialog)
        self.assertTrue(panel.field_errors['temperature_entry'].get_visible())
        panel.temperature_entry.set_text('0.4')
        panel.keep_alive_dropdown.set_selected(5)
        panel.keep_alive_entry.set_text('42')
        panel._settings_done()
        pump_until(lambda: panel._settings_dialog is None)
        panel.open_settings()
        self.assertEqual(panel.get_options_from_ui()['temperature'], .4)
        self.assertEqual(panel.get_keep_alive(), 42)
        self.assertEqual(tab.chat_input.send_button.get_icon_name(), 'mail-send-symbolic')

    def test_host_editor_has_persistent_labels_and_disables_invalid_save(self):
        dialog = self.present(HostEditDialog())
        self.assertFalse(dialog.get_response_enabled('save'))
        dialog.name_entry.set_text('Local')
        dialog.hostname_entry.set_text('ftp://localhost')
        self.assertFalse(dialog.get_response_enabled('save'))
        self.assertTrue(dialog.validation_error.get_visible())
        dialog.hostname_entry.set_text('http://localhost:11434')
        self.assertTrue(dialog.get_response_enabled('save'))
