pkgdatadir = get_option('prefix') / get_option('datadir') / meson.project_name()
moduledir = pkgdatadir / 'gnollama'
gnome = import('gnome')

gnome.compile_resources('gnollama',
  'gnollama.gresource.xml',
  gresource_bundle: true,
  install: true,
  install_dir: pkgdatadir,
)


python = import('python')
py3 = python.find_installation('python3')

# Check for python-markdown
markdown_check = run_command(py3, '-c', 'import markdown', check: false)
if markdown_check.returncode() != 0
  warning('python3-markdown not found. Markdown rendering will be limited.')
endif

# Check for GtkSourceView (optional, for code highlighting)
dependency('gtksourceview-5', required: false)


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
  'window.py',
  'tab.py',
  'ollama.py',
  'utils.py',
  'storage.py',
  'markdown_view.py',
  'bubbles.py',
]

install_data(gnollama_sources, install_dir: moduledir)
