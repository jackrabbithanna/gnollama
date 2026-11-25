test('Regression tests', py3,
  args: [files('run.py'), meson.project_source_root(), meson.project_build_root() / 'src' / 'tureen.gresource'],
  depends: tureen_resources,
  timeout: 600,
)
