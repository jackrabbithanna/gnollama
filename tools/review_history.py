#!/usr/bin/env python3
"""Capture grouped history and autocomplete with isolated fixture records."""
import argparse
import gettext
import json
import locale
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--resource', required=True)
parser.add_argument('--output', required=True)
parser.add_argument('--language', default='en')
args = parser.parse_args()
output = Path(args.output)
output.mkdir(parents=True, exist_ok=True)
gettext.install('gnollama')
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, Gio, GLib, Gdk


def pump(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        while GLib.MainContext.default().pending():
            GLib.MainContext.default().iteration(False)
        if predicate():
            return
        time.sleep(.005)
    raise AssertionError('History view did not settle')


with tempfile.TemporaryDirectory(prefix='gnollama-history-review-') as temporary:
    os.environ.update(XDG_DATA_HOME=temporary, XDG_CONFIG_HOME=temporary,
                      GSETTINGS_BACKEND='memory', GSETTINGS_SCHEMA_DIR=temporary)
    subprocess.run(['glib-compile-schemas', '--targetdir=' + temporary, str(ROOT / 'data')], check=True)
    if args.language != 'en':
        os.environ['LANGUAGE'] = args.language
        locale.setlocale(locale.LC_ALL, 'en_US.UTF-8')
        catalog = Path(temporary) / args.language / 'LC_MESSAGES/gnollama.mo'
        catalog.parent.mkdir(parents=True)
        subprocess.run(['msgfmt', '-o', str(catalog), str(ROOT / 'po' / (args.language + '.po'))], check=True)
        locale.bindtextdomain('gnollama', temporary)
        locale.bind_textdomain_codeset('gnollama', 'UTF-8')
        locale.textdomain('gnollama')
        gettext.bindtextdomain('gnollama', temporary)
        gettext.textdomain('gnollama')
        gettext.install('gnollama', temporary)
    Gtk.init()
    Adw.init()
    Gtk.Settings.get_default().set_property('gtk-enable-animations', False)
    Gtk.Widget.set_default_direction(Gtk.TextDirection.RTL if args.language == 'ar' else Gtk.TextDirection.LTR)
    Gio.Resource.load(args.resource)._register()
    from src.storage import ChatStorage
    from src.window import GnollamaWindow
    from src.history import CATEGORIES
    from src import ollama
    storage = ChatStorage(temporary)
    app = Adw.Application(application_id='io.github.jackrabbithanna.Gnollama.HistoryReview', flags=Gio.ApplicationFlags.NON_UNIQUE)
    app.register(None)
    with patch.object(ollama, 'fetch_models', return_value=['cedar:8b']), patch.object(ollama, 'show_model', return_value={'capabilities': ['completion']}):
        window = GnollamaWindow(application=app, storage=storage)
        window.present()
        pump(lambda: storage.services.idle and storage.writer.idle and not window.history.pending)
        titles = dict(chat='Chat', comparison='Comparison', model_conversation='Model conversation', pinned='Pinned')
        with storage.db._get_conn() as conn:
            for key in CATEGORIES[1:]:
                for index in range(2 if key == 'pinned' else 9):
                    title = ('Solar ' if index % 2 == 0 else 'Lunar ') + titles[key] + ' ' + str(index + 1)
                    conn.execute('''INSERT INTO chats(id,title,created_at,updated_at,kind,options,is_pinned)
                        VALUES (?,?,?,?,?,'{}',?)''', (f'{key}-{index}', title, index, index,
                            'chat' if key == 'pinned' else key, key == 'pinned'))
            conn.commit()
        for index in range(2):
            storage.db.save_draft(dict(id=f'draft-{index}', mode='chat', text='Solar draft ' + str(index + 1),
                                      settings={}, targets=[], revision=1))
        window.load_history_sidebar()
        pump(lambda: not window.history.pending)
        counts = {}
        for key in CATEGORIES:
            rows = window.draft_rows if key == 'drafts' else window.chat_rows
            counts[key] = sum(row.get_section() == window.history.sections[key] for row in rows.values())
        assert counts == dict(drafts=2, pinned=2, chat=5, comparison=5, model_conversation=5), counts
        captures = []

        def capture(name, width=1100, dark=False, bottom=False):
            window.set_visible(False)
            window.set_default_size(width, 1100)
            Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK if dark else Adw.ColorScheme.FORCE_LIGHT)
            window.present()
            started = time.monotonic()
            pump(lambda: window.get_mapped() and time.monotonic() - started > .25)
            # Let the narrow-window breakpoint settle before opening its overlay.
            window.split_view.set_show_sidebar(True)
            pump(lambda: window.history_search.get_mapped())
            if bottom:
                # Inspect the widget tree only in this visual test harness.
                def scrolls(widget):
                    if isinstance(widget, Gtk.ScrolledWindow):
                        yield widget
                    child = widget.get_first_child()
                    while child:
                        yield from scrolls(child)
                        child = child.get_next_sibling()
                scrolled = next(scrolls(window.history_sidebar))
                adj = scrolled.get_vadjustment()
                adj.set_value(adj.get_upper() - adj.get_page_size())
            started = time.monotonic()
            pump(lambda: time.monotonic() - started > .1)
            assert window.get_width() <= width, (name, window.get_width())
            snapshot = Gtk.Snapshot()
            snapshot.render_background(window.get_style_context(), 0, 0, window.get_width(), window.get_height())
            window.snapshot_child(window.get_child(), snapshot)
            texture = window.get_renderer().render_texture(snapshot.to_node(), None)
            texture.save_to_png(str(output / (name + '.png')))
            captures.append(dict(name=name, width=window.get_width(), height=window.get_height()))

        capture('history-collapsed-wide')
        capture('history-recent-categories', bottom=True)
        window.history_search.set_text('comparison')
        pump(lambda: not window.history.pending)
        window.history.toggle('comparison')
        pump(lambda: not window.history.pending)
        assert len(window.chat_rows) == 9
        capture('history-expanded-category')
        window.history.toggle('comparison')
        pump(lambda: not window.history.pending)
        assert len(window.chat_rows) == 5
        window.history_search.set_text('solar')
        pump(lambda: not window.history.pending)
        capture('history-filtered-categories')
        window.history_search.grab_focus()
        pump(lambda: window.history.popover.get_visible())
        suggestion_count = len(window.history._suggestions)
        assert suggestion_count == 8, suggestion_count
        window.history._key_pressed(None, Gdk.KEY_Down, 0, Gdk.ModifierType(0))
        started = time.monotonic()
        pump(lambda: time.monotonic() - started > .1)
        # Render the native popover surface separately from the parent window.
        popover = window.history.popover
        snap = Gtk.Snapshot()
        popover.snapshot_child(popover.get_first_child(), snap)
        texture = popover.get_renderer().render_texture(snap.to_node(), None)
        texture.save_to_png(str(output / 'history-autocomplete.png'))
        window.history._key_pressed(None, Gdk.KEY_Escape, 0, Gdk.ModifierType(0))
        window.history_search.set_text('')
        pump(lambda: not window.history.pending)
        capture('history-narrow-dark', 360, True, bottom=True)
        (output / 'history-report.json').write_text(json.dumps(dict(collapsed_counts=counts, expanded_comparisons=9,
            restored_comparisons=5, suggestion_count=suggestion_count, captures=captures), indent=2) + '\n')
        window.request_shutdown()
        pump(lambda: window._allow_close)
