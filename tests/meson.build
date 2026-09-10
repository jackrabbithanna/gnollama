test('Regression tests', py3,
  args: [files('run.py'), meson.project_source_root(), meson.project_build_root() / 'src' / 'gnollama.gresource'],
  depends: gnollama_resources,
  timeout: 90,
)
