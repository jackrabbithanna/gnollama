fs = import('fs')
msgfmt = find_program('msgfmt', version: '>= 0.19.7')
po_dir = meson.project_source_root() / 'po'
translation_files = files('../po/LINGUAS')
foreach line : fs.read('../po/LINGUAS').split('\n')
  foreach language : line.split('#')[0].split()
    translation_files += files('../po' / (language + '.po'))
  endforeach
endforeach

# i18n.merge_file does not track catalogs as dependencies. Explicit inputs to
# the merge targets keep installed metadata current after translation edits.
desktop_file = custom_target('desktop-translations',
        input: 'io.github.jackrabbithanna.Tureen.desktop.in',
       output: 'io.github.jackrabbithanna.Tureen.desktop',
      command: [msgfmt, '--desktop', '--template', '@INPUT@', '-d', po_dir, '-o', '@OUTPUT@'],
 depend_files: translation_files,
      install: true,
  install_dir: get_option('datadir') / 'applications'
)

desktop_utils = find_program('desktop-file-validate', required: false)
if desktop_utils.found()
  test('Validate desktop file', desktop_utils, args: [desktop_file])
endif

appstream_file = custom_target('appstream-translations',
        input: 'io.github.jackrabbithanna.Tureen.metainfo.xml.in',
       output: 'io.github.jackrabbithanna.Tureen.metainfo.xml',
      command: [msgfmt, '--xml', '--template', '@INPUT@', '-d', po_dir, '-o', '@OUTPUT@'],
 depend_files: translation_files,
      install: true,
  install_dir: get_option('datadir') / 'metainfo'
)

appstreamcli = find_program('appstreamcli', required: false, disabler: true)
test('Validate appstream file', appstreamcli,
     args: ['validate', '--no-net', '--explain', appstream_file])

service_conf = configuration_data()
service_conf.set('bindir', get_option('prefix') / get_option('bindir'))
configure_file(
  input: 'io.github.jackrabbithanna.Tureen.service.in',
  output: 'io.github.jackrabbithanna.Tureen.service',
  configuration: service_conf,
  install_dir: get_option('datadir') / 'dbus-1' / 'services'
)

subdir('icons')
