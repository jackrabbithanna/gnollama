"""Boundaries the architecture relies on, checked against the sources."""
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCES = sorted((ROOT / 'src').rglob('*.py'))
# Modules that may use the database object directly.
PERSISTENCE = {'storage.py', 'database.py', 'library_repository.py', 'history.py'}
# Modules that run without widgets.
HEADLESS = ['chat_controller.py', 'app_context.py', 'request_runner.py', 'services.py', 'storage.py',
            'providers/__init__.py', 'providers/base.py', 'providers/ollama.py', 'providers/registry.py',
            'providers/transport.py']


class ArchitectureTests(unittest.TestCase):
    def test_headless_modules_do_not_import_widgets(self):
        for name in HEADLESS:
            with self.subTest(module=name):
                text = (ROOT / 'src' / name).read_text()
                self.assertIsNone(re.search(r'^\s*from gi\.repository import .*\b(Gtk|Adw|Gdk)\b', text, re.M))
                self.assertIsNone(re.search(r'^\s*(from|import) \S*widgets', text, re.M))

    def test_importing_the_chat_controller_does_not_load_gtk(self):
        code = ("import gettext, sys; gettext.install('gnollama'); import src.chat_controller, src.app_context; "
                "sys.exit(any(m in sys.modules for m in ('gi.repository.Gtk', 'gi.repository.Adw')))")
        result = subprocess.run([sys.executable, '-c', code], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_only_persistence_modules_use_the_database_directly(self):
        for path in SOURCES:
            if path.name in PERSISTENCE or path.name.endswith('_store.py'):
                continue
            with self.subTest(module=str(path.relative_to(ROOT))):
                self.assertNotIn('.db.', path.read_text())

    def test_behaviour_follows_capabilities_not_provider_names(self):
        for path in SOURCES:
            text = path.read_text()
            with self.subTest(module=str(path.relative_to(ROOT))):
                self.assertNotIn('is_cloud', text)
                self.assertNotIn('set_cloud', text)
                if 'providers' not in path.parts:
                    self.assertNotIn('OllamaCloudProvider', text)

    def test_views_do_not_reach_into_other_widgets(self):
        private = re.compile(r'\b(message_list|options_panel|panel|knowledge_control|chat_input)\._[a-z]')
        for path in SOURCES:
            with self.subTest(module=str(path.relative_to(ROOT))):
                self.assertEqual(private.findall(path.read_text()), [])

    def test_request_settings_are_typed_records_not_dictionaries(self):
        keyed = re.compile(r'\bstate\.settings(\[|\.get\(|\.items\()')
        for path in SOURCES:
            with self.subTest(module=str(path.relative_to(ROOT))):
                self.assertEqual(keyed.findall(path.read_text()), [])


if __name__ == '__main__':
    unittest.main()
