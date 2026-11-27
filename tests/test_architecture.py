"""Boundaries the architecture relies on, checked against the sources."""
import ast
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCES = sorted((ROOT / 'src').rglob('*.py'))
# Modules that may use the database object directly.
PERSISTENCE = {'storage.py', 'database.py', 'library_repository.py', 'history.py'}
# Modules that run without widgets, and import without the application entry point.
HEADLESS = ['app_context.py', 'chat_controller.py', 'chat_store.py', 'collections_store.py', 'comparison.py',
            'connections.py', 'context.py', 'credentials.py', 'database.py', 'decision.py', 'decision_run.py', 'decision_store.py',
            'image_generation.py', 'image_run.py', 'image_store.py',
            'drafts.py', 'export.py', 'history.py', 'knowledge.py', 'knowledge_store.py', 'library_repository.py',
            'model_conversation.py', 'model_conversation_store.py', 'model_summary.py', 'records.py', 'request_runner.py',
            'services.py', 'session.py', 'storage.py', 'store_common.py', 'structured.py', 'tool_calling.py',
            'vectors.py', 'web_import.py', 'web_store.py', 'workspace_store.py', 'writer.py',
            'providers/__init__.py', 'providers/base.py', 'providers/imaging.py', 'providers/inference_manager.py',
            'providers/lmstudio.py', 'providers/ollama.py',
            'providers/openai.py', 'providers/openai_platform.py', 'providers/openrouter.py', 'providers/registry.py', 'providers/systemone.py',
            'providers/transport.py', 'providers/vercel.py']
# These open a dialog on request, importing GTK only inside that function.
LAZY_VIEWS = {'export.py', 'session.py'}


class ArchitectureTests(unittest.TestCase):
    def test_headless_modules_do_not_import_widgets(self):
        for name in sorted(set(HEADLESS) - LAZY_VIEWS):
            with self.subTest(module=name):
                text = (ROOT / 'src' / name).read_text()
                self.assertIsNone(re.search(r'^\s*from gi\.repository import .*\b(Gtk|Adw|Gdk)\b', text, re.M))
                self.assertIsNone(re.search(r'^\s*(from|import) \S*widgets', text, re.M))

    def test_importing_the_chat_controller_does_not_load_gtk(self):
        code = ("import sys; import src.chat_controller, src.app_context; "
                "sys.exit(any(m in sys.modules for m in ('gi.repository.Gtk', 'gi.repository.Adw')))")
        result = subprocess.run([sys.executable, '-c', code], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_headless_modules_import_without_installed_builtins_or_gtk(self):
        names = ['src.' + name[:-3].replace('/', '.').removesuffix('.__init__') for name in HEADLESS]
        code = ("import builtins, importlib, sys\n"
                "for name in sys.argv[1:]:\n"
                "    importlib.import_module(name)\n"
                "assert not hasattr(builtins, '_')\n"
                "loaded = [m for m in ('gi.repository.Gtk', 'gi.repository.Adw', 'gi.repository.Gdk') if m in sys.modules]\n"
                "assert not loaded, loaded\n")
        result = subprocess.run([sys.executable, '-c', code, *names], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_every_module_imports_the_translation_function_it_calls(self):
        for path in SOURCES:
            tree = ast.parse(path.read_text())
            calls = any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == '_'
                        for node in ast.walk(tree))
            imported = any(isinstance(node, ast.ImportFrom) and node.module == 'gettext'
                           and any(alias.asname == '_' for alias in node.names) for node in tree.body)
            with self.subTest(module=str(path.relative_to(ROOT))):
                self.assertTrue(imported or not calls)

    def test_the_database_composes_its_repositories(self):
        from src.connections import ConnectionProvider
        from src.database import DatabaseManager
        self.assertEqual(DatabaseManager.__bases__, (object,))
        for name in ('database.py', 'chat_store.py', 'knowledge_store.py', 'collections_store.py',
                     'web_store.py', 'workspace_store.py', 'model_conversation_store.py', 'decision_store.py', 'image_store.py',
                     'history.py'):
            with self.subTest(module=name):
                # Connections come from the shared provider, never from a repository.
                self.assertNotIn('sqlite3.connect(', (ROOT / 'src' / name).read_text().replace(
                    "sqlite3.connect(Path(self.db_path)", '').replace('sqlite3.connect(backup)', ''))
        self.assertTrue(callable(ConnectionProvider.close))

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
