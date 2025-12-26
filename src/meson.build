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

conf = configuration_data()
conf.set('PYTHON', python.find_installation('python3').full_path())
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
]

install_data(gnollama_sources, install_dir: moduledir)
