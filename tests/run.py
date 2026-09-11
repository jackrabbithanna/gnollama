import gettext
import os
from pathlib import Path
import sys
import tempfile
import subprocess
import unittest

sys.dont_write_bytecode = True
root = Path(sys.argv[1])
sys.path.insert(0, str(root))
sys.path.insert(0, str(root / 'tests'))
with tempfile.TemporaryDirectory(prefix='gnollama-tests-') as temp:
    os.environ['XDG_DATA_HOME'] = temp
    os.environ['XDG_CONFIG_HOME'] = temp
    os.environ['GSETTINGS_BACKEND'] = 'memory'
    os.environ['GSETTINGS_SCHEMA_DIR'] = temp
    subprocess.run(['glib-compile-schemas', '--targetdir='+temp, str(root/'data')], check=True)
    gettext.install('gnollama')
    import gi
    gi.require_version('Gtk', '4.0')
    gi.require_version('Adw', '1')
    from gi.repository import Gio, Gtk, Adw
    resource = Gio.Resource.load(sys.argv[2])
    resource._register()
    Gtk.init()
    Adw.init()
    if os.environ.get('GNOLLAMA_REQUIRE_DISPLAY') == '1':
        from gi.repository import Gdk
        if Gdk.Display.get_default() is None:
            raise RuntimeError('Required GTK display is unavailable')
    suite = unittest.defaultTestLoader.discover(str(root / 'tests'), pattern=os.environ.get('GNOLLAMA_TEST_PATTERN', 'test*.py'))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    from src.session import worker
    worker.shutdown()
    required_skips = os.environ.get('GNOLLAMA_REQUIRE_DISPLAY') == '1' and any(
        'display' in reason.lower() for test, reason in result.skipped)
    sys.exit(not result.wasSuccessful() or required_skips)
