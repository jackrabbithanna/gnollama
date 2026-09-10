"""Run each locale in its own process because C gettext and GTK cache state."""
import builtins
import gettext
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
directory = Path(sys.argv[1])
launcher = (root / 'src/gnollama.in').read_text().replace('@localedir@', str(directory))
exec(compile(launcher, str(root / 'src/gnollama.in'), 'exec'), {'__name__': 'locale_probe'})

import gi
gi.require_version('GLib', '2.0')
from gi.repository import GLib
if sys.argv[2] == 'gettext':
    print(json.dumps([builtins._('Cancel'), gettext.gettext('Cancel'), GLib.dgettext('gnollama', 'Cancel')]))
    sys.exit()

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gdk, Gio, Gtk
Gtk.init()
if Gdk.Display.get_default() is None:
    sys.exit(77)
Adw.init()
os.environ['GSETTINGS_BACKEND'] = 'memory'
os.environ['GSETTINGS_SCHEMA_DIR'] = str(directory)
subprocess.run(['glib-compile-schemas', '--targetdir=' + str(directory), str(root / 'data')], check=True)
resource = directory / 'gnollama.gresource'
subprocess.run(['glib-compile-resources', str(root / 'src/gnollama.gresource.xml'),
                '--sourcedir=' + str(root / 'src'), '--target=' + str(resource)], check=True)
Gio.Resource.load(str(resource))._register()

from src.main import GnollamaApplication
from src.host_manager import HostEditDialog
from src.widgets.chat_input import ChatInput
from src.widgets.options_panel import OptionsPanel
from src.widgets.json_view import code_view
from src.session import display_chat_title

app = GnollamaApplication()
app.set_application_id('io.github.jackrabbithanna.Gnollama.Test' + uuid.uuid4().hex)
app.set_flags(Gio.ApplicationFlags.NON_UNIQUE)
app.register()
widget = ChatInput()
options = OptionsPanel()
options.tools_box.set_visible(True)
options.output_dropdown.set_selected(2)
options.keep_alive_dropdown.set_selected(5)
texture = Gdk.MemoryTexture.new(1, 1, Gdk.MemoryFormat.R8G8B8A8,
                                GLib.Bytes.new(bytes([255, 255, 255, 255])), 4)
image_path = directory / 'pixel.png'
texture.save_to_png(str(image_path))
widget.selected_image_paths = [str(image_path), str(image_path)]
widget.update_image_preview()
builder = Gtk.Builder.new_from_file(str(root / 'src/shortcuts-dialog.ui'))
shortcut = next(item for item in builder.get_objects()
                if isinstance(item, Adw.ShortcutsItem) and item.get_action_name() == 'app.shortcuts')
def direction(widget):
    return 'rtl' if widget.get_direction() == Gtk.TextDirection.RTL else 'ltr'
print(json.dumps({
    'send': widget.send_button.get_tooltip_text(),
    'shortcut': shortcut.get_title(),
    'direction': direction(widget),
    'code_direction': direction(code_view()),
    'text_direction': direction(code_view(language=None)),
    'host_direction': direction(HostEditDialog().hostname_entry),
    'image_label': widget.image_label.get_text(),
    'default_title': display_chat_title('New Chat'),
    'custom_title': display_chat_title('My custom title'),
    'options_min_width': options.measure(Gtk.Orientation.HORIZONTAL, -1).minimum,
    'settings_min_width': options.settings_content.measure(Gtk.Orientation.HORIZONTAL, -1).minimum,
}))
from src.session import worker
worker.shutdown()
