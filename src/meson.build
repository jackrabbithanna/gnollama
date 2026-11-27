pkgdatadir = get_option('prefix') / get_option('datadir') / meson.project_name()
moduledir = pkgdatadir / 'tureen'
gnome = import('gnome')

tureen_resources = gnome.compile_resources('tureen',
  'tureen.gresource.xml',
  gresource_bundle: true,
  install: true,
  install_dir: pkgdatadir,
)


python = import('python')
py3 = python.find_installation('python3')
run_command(py3, '-c', 'import sys, sqlite3; assert sys.version_info >= (3, 11), "Python 3.11+ is required"; assert sqlite3.sqlite_version_info >= (3, 41, 0), "SQLite 3.41+ is required"; db=sqlite3.connect(":memory:"); assert hasattr(db, "blobopen"); db.execute("CREATE VIRTUAL TABLE test USING fts5(text)"); db.close()', check: true)
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

# Check for ziamath (optional, for rendering equations)
ziamath_check = run_command(py3, '-c', 'import ziamath', check: false)
if ziamath_check.returncode() != 0
  warning('ziamath not found. Display equations will be shown as TeX source.')
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
  input: 'tureen.in',
  output: 'tureen',
  configuration: conf,
  install: true,
  install_dir: get_option('bindir'),
  install_mode: 'rwxr-xr-x'
)

tureen_sources = [
  '__init__.py',
  'main.py',
  'startup.py',
  'vectors.py',
  'window.py',
  'tab.py',
  'session.py',
  'records.py',
  'services.py',
  'workspace_store.py',
  'history.py',
  'request_runner.py',
  'drafts.py',
  'context.py',
  'app_context.py',
  'chat_controller.py',
  'export.py',
  'comparison.py',
  'model_conversation.py',
  'model_conversation_store.py',
  'library_repository.py',
  'structured.py',
  'tool_calling.py',
  'decision.py',
  'decision_store.py',
  'decision_run.py',
  'image_run.py',
  'image_files.py',
  'image_generation.py',
  'image_store.py',
  'knowledge.py',
  'knowledge_store.py',
  'collections_store.py',
  'web_store.py',
  'web_import.py',
  'credentials.py',
  'storage.py',
  'writer.py',
  'database.py',
  'connections.py',
  'chat_store.py',
  'store_common.py',
  'markdown_view.py',
  'math_render.py',
  'bubbles.py',
  'host_manager.py',
  'model_manager.py',
  'model_summary.py',
]

install_data(tureen_sources, install_dir: moduledir)

tureen_widget_sources = [
  'widgets/__init__.py',
  'widgets/feedback.py',
  'widgets/composer.py',
  'widgets/comparison_view.py',
  'widgets/decision_answers.py',
  'widgets/decision_form.py',
  'widgets/decision_images.py',
  'widgets/decision_view.py',
  'widgets/image_results.py',
  'widgets/image_settings.py',
  'widgets/image_view.py',
  'widgets/history_sidebar.py',
  'widgets/workspace_section.py',
  'widgets/model_target.py',
  'widgets/model_conversation_view.py',
  'widgets/chat_input.py',
  'widgets/options_panel.py',
  'widgets/json_view.py',
  'widgets/tool_view.py',
  'widgets/knowledge_view.py',
  'widgets/knowledge_common.py',
  'widgets/collection_views.py',
  'widgets/source_views.py',
  'widgets/url_import.py',
  'widgets/knowledge_import.py',
  'widgets/message_list.py',
  'widgets/search.py',
]

install_data(tureen_widget_sources, install_dir: moduledir / 'widgets')

tureen_provider_sources = [
  'providers/__init__.py',
  'providers/base.py',
  'providers/imaging.py',
  'providers/inference_manager.py',
  'providers/lmstudio.py',
  'providers/ollama.py',
  'providers/openai.py',
  'providers/openai_platform.py',
  'providers/openrouter.py',
  'providers/registry.py',
  'providers/systemone.py',
  'providers/transport.py',
  'providers/vercel.py',
]

install_data(tureen_provider_sources, install_dir: moduledir / 'providers')
