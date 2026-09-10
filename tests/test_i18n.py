"""Catalog and installed-launcher regressions; no translation library required."""
import ast
import gettext
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(all(shutil.which(tool) for tool in ('msgfmt', 'msgcmp', 'xgettext')),
                     'Translation checks require GNU gettext tools')
class TranslationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='gnollama-i18n-tests-')
        cls.addClassCleanup(cls.temp.cleanup)
        cls.directory = Path(cls.temp.name)
        # GLib intentionally disables translations for C and C.UTF-8 locales.
        available = subprocess.run(['locale', '-a'], check=True, capture_output=True,
                                   text=True).stdout.splitlines()
        cls.message_locale = next((name for name in available
                                   if name.lower().replace('-', '').endswith('.utf8')
                                   and not name.startswith('C.')), None)
        cls.languages = [
            line for line in (ROOT / 'po/LINGUAS').read_text().splitlines()
            if line and not line.startswith('#')
        ]
        cls.catalogs = {}
        for language in cls.languages:
            output = cls.directory / language / 'LC_MESSAGES/gnollama.mo'
            output.parent.mkdir(parents=True)
            subprocess.run(['msgfmt', '--check', '--check-accelerators=_',
                            '-o', str(output), str(ROOT / f'po/{language}.po')],
                           check=True, capture_output=True, text=True)
            with output.open('rb') as stream:
                cls.catalogs[language] = gettext.GNUTranslations(stream)
        cls.template = cls.directory / 'current.pot'
        subprocess.run(['xgettext', '--from-code=UTF-8', '--keyword=_',
                        '--keyword=ngettext:1,2', '--keyword=pgettext:1c,2',
                        '-f', 'po/POTFILES.in', '-o', str(cls.template)],
                       cwd=ROOT, check=True, capture_output=True, text=True)

    def compare(self, catalog, reference, *, require_translated=False):
        args = ['msgcmp', '--no-fuzzy-matching']
        if not require_translated:
            args.extend(['--use-untranslated', '--use-fuzzy'])
        result = subprocess.run(args + [str(catalog), str(reference)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_catalogs_and_template_match_current_sources(self):
        self.assertEqual(self.languages, sorted(set(self.languages)))
        self.compare(ROOT / 'po/gnollama.pot', self.template)
        self.compare(self.template, ROOT / 'po/gnollama.pot')
        for language in self.languages:
            with self.subTest(language=language):
                catalog = ROOT / f'po/{language}.po'
                self.compare(catalog, self.template, require_translated=True)
                self.compare(self.template, catalog)
                self.assertEqual(self.catalogs[language].info()['language'], language)

    def test_extraction_includes_all_marked_sources(self):
        listed = set((ROOT / 'po/POTFILES.in').read_text().splitlines())
        for path in (ROOT / 'src').rglob('*.py'):
            calls = [node for node in ast.walk(ast.parse(path.read_text()))
                     if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                     and node.func.id in ('_', 'ngettext', 'pgettext')]
            if calls:
                self.assertIn(str(path.relative_to(ROOT)), listed)
        for path in (ROOT / 'src').rglob('*.ui'):
            tree = ET.parse(path)
            self.assertEqual(tree.getroot().get('domain'), 'gnollama', str(path))
            if tree.findall('.//*[@translatable="yes"]'):
                self.assertIn(str(path.relative_to(ROOT)), listed)

    def test_plural_rules_and_mnemonics(self):
        counts = (0, 1, 2, 3, 11, 99, 100, 102, 103)
        expected = {
            'ar': (0, 1, 2, 3, 4, 4, 5, 5, 3),
            'de': (1, 0, 1, 1, 1, 1, 1, 1, 1),
            'es': (1, 0, 1, 1, 1, 1, 1, 1, 1),
            'fr': (0, 0, 1, 1, 1, 1, 1, 1, 1),
            'hi': (0, 0, 1, 1, 1, 1, 1, 1, 1),
            'pt': (1, 0, 1, 1, 1, 1, 1, 1, 1),
            'zh_CN': (0,) * len(counts),
        }
        for language, categories in expected.items():
            catalog = self.catalogs[language]
            for count, category in zip(counts, categories):
                with self.subTest(language=language, count=count):
                    self.assertEqual(catalog.plural(count), category)
                    text = catalog.ngettext('{0} image selected', '{0} images selected', count)
                    self.assertIn(str(count), text.format(count))
                    self.assertNotEqual(text, '{0} image selected' if count == 1
                                        else '{0} images selected')
        menu = ('_New Chat', '_New Response', '_Manage hosts', '_Manage models',
                '_Keyboard Shortcuts', '_About Gnollama')
        for language, catalog in self.catalogs.items():
            with self.subTest(language=language):
                mnemonics = []
                for key in menu:
                    translated = catalog.gettext(key)
                    self.assertEqual(translated.count('_'), 1)
                    mnemonics.append(translated.split('_')[1][0].casefold())
                self.assertEqual(len(set(mnemonics)), len(menu))

    def probe(self, language, gtk=False):
        if self.message_locale is None:
            self.skipTest('Runtime translation checks require a non-C UTF-8 locale')
        result = subprocess.run(
            [sys.executable, str(ROOT / 'tests/i18n_probe.py'), str(self.directory),
             'gtk' if gtk else 'gettext'],
            env=dict(os.environ, LANGUAGE=language, LC_ALL=self.message_locale),
            capture_output=True, text=True, timeout=20)
        if result.returncode == 77:
            self.skipTest('GTK language smoke tests require a display')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        return json.loads(result.stdout)

    def test_launcher_configures_python_and_c_gettext(self):
        for language, catalog in self.catalogs.items():
            with self.subTest(language=language):
                result = self.probe(language)
                translated = catalog.gettext('Cancel')
                self.assertNotEqual(translated, 'Cancel')
                self.assertEqual(result, [translated] * 3)
        self.assertEqual(self.probe('zz'), ['Cancel'] * 3)
        self.assertEqual(self.probe('zz:hi'), ['रद्द करें'] * 3)

    def test_gtk_templates_context_and_arabic_direction(self):
        for language in self.languages:
            with self.subTest(language=language):
                result = self.probe(language, gtk=True)
                catalog = self.catalogs[language]
                self.assertEqual(result['send'], catalog.gettext('Send Message'))
                self.assertEqual(result['shortcut'],
                                 catalog.pgettext('shortcut window', 'Show Shortcuts'))
                self.assertEqual(result['direction'], 'rtl' if language == 'ar' else 'ltr')
                self.assertEqual(result['code_direction'], 'ltr')
                self.assertEqual(result['text_direction'], result['direction'])
                self.assertEqual(result['host_direction'], 'ltr')
                self.assertEqual(result['default_title'], catalog.gettext('New Chat'))
                self.assertEqual(result['custom_title'], 'My custom title')
                self.assertLessEqual(result['options_min_width'], 360)
                self.assertLessEqual(result['settings_min_width'], 360)
                self.assertEqual(result['image_label'],
                                 catalog.ngettext('{0} image selected', '{0} images selected', 2).format(2))


if __name__ == '__main__':
    unittest.main()
