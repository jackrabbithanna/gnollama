pkgdatadir = get_option('prefix') / get_option('datadir') / meson.project_name()
moduledir = pkgdatadir / 'gnollama'
gnome = import('gnome')

gnollama_resources = gnome.compile_resources('gnollama',
  'gnollama.gresource.xml',
  gresource_bundle: true,
  install: true,
  install_dir: pkgdatadir,
)


python = import('python')
py3 = python.find_installation('python3')
run_command(py3, '-c', 'from jsonschema import Draft202012Validator; from referencing import Registry, Resource; from importlib.metadata import version; assert tuple(map(int, version("jsonschema").split(".")[:2])) >= (4, 26), "jsonschema 4.26+ is required"', check: true)
run_command(py3, '-c', 'import gi; gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1"); gi.require_version("Soup", "3.0"); from gi.repository import Gtk, Adw, Soup', check: true)
run_command(py3, '-c', 'import gi; gi.require_version("Secret", "1"); from gi.repository import Secret', check: true)
run_command(py3, '-c', 'import sqlite3, sqlite_vec; from pypdf import PdfReader; db=sqlite3.connect(":memory:"); db.enable_load_extension(True); sqlite_vec.load(db); db.enable_load_extension(False); assert db.execute("SELECT vec_version()").fetchone()[0] == "v0.1.9"', check: true)
run_command(py3, '-c', 'import trafilatura, markdownify, cssselect; from lxml import html; from importlib.metadata import version; assert version("trafilatura") == "2.2.0"; assert version("markdownify") == "1.2.3"; html.fromstring("<main>test</main>").cssselect("main")', check: true)

# Check for python-markdown
markdown_check = run_command(py3, '-c', 'import markdown', check: false)
if markdown_check.returncode() != 0
  warning('python3-markdown not found. Markdown rendering will be limited.')
endif

# Check for GtkSourceView (optional, for code highlighting)
dependency('gtksourceview-5', required: false)

# Check for GtkSourceView python bindings explicitly
gtksource_check = run_command(py3, '-c', 'import gi; gi.require_version("GtkSource", "5"); from gi.repository import GtkSource', check: false)
if gtksource_check.returncode() != 0
  warning('GtkSourceView 5 python bindings not found. Code highlighting will be disabled.')
endif


conf = configuration_data()
conf.set('PYTHON', py3.full_path())
conf.set('VERSION', meson.project_version())
conf.set('localedir', get_option('prefix') / get_option('localedir'))
conf.set('pkgdatadir', pkgdatadir)

configure_file(
  input: 'gnollama.in',
  output: 'gnollama',
  configuration: conf,
  install: true,
  install_dir: get_option('bindir'),
  install_mode: 'rwxr-xr-x'
)

gnollama_sources = [
  '__init__.py',
  'main.py',
  'startup.py',
  'vectors.py',
  'window.py',
  'tab.py',
  'session.py',
  'structured.py',
  'tool_calling.py',
  'knowledge.py',
  'knowledge_store.py',
  'collections_store.py',
  'web_store.py',
  'web_import.py',
  'ollama.py',
  'credentials.py',
  'storage.py',
  'writer.py',
  'database.py',
  'markdown_view.py',
  'bubbles.py',
  'host_manager.py',
  'model_manager.py',
]

install_data(gnollama_sources, install_dir: moduledir)

gnollama_widget_sources = [
  'widgets/__init__.py',
  'widgets/chat_input.py',
  'widgets/options_panel.py',
  'widgets/json_view.py',
  'widgets/tool_view.py',
  'widgets/knowledge_view.py',
  'widgets/url_import.py',
  'widgets/knowledge_import.py',
  'widgets/message_list.py',
]

install_data(gnollama_widget_sources, install_dir: moduledir / 'widgets')
