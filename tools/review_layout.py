#!/usr/bin/env python3
"""Capture real GTK layouts using isolated fixture data and mocked Ollama."""
import argparse
from contextlib import ExitStack
import gettext
import json
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
parser.add_argument('--output', default='validation-screenshots')
parser.add_argument('--language', default='en')
args = parser.parse_args()
output = Path(args.output)
output.mkdir(parents=True, exist_ok=True)
gettext.install('gnollama')
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, Gio, GLib
from src.storage import ChatStorage
from src import ollama


def pump(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        while GLib.MainContext.default().pending():
            GLib.MainContext.default().iteration(False)
        if predicate():
            return
        time.sleep(.005)
    raise AssertionError('Layout did not settle')


with tempfile.TemporaryDirectory(prefix='gnollama-layout-') as temporary, ExitStack() as mocks:
    os.environ.update(XDG_DATA_HOME=temporary, XDG_CONFIG_HOME=temporary, GSETTINGS_BACKEND='memory', GSETTINGS_SCHEMA_DIR=temporary)
    subprocess.run(['glib-compile-schemas', '--targetdir=' + temporary, str(ROOT / 'data')], check=True)
    if args.language != 'en':
        import locale
        os.environ['LANGUAGE'] = args.language
        locale.setlocale(locale.LC_ALL, 'en_US.UTF-8')
        catalog = Path(temporary) / args.language / 'LC_MESSAGES' / 'gnollama.mo'
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
    Gio.Resource.load(args.resource)._register()
    from src.window import GnollamaWindow
    mocks.enter_context(patch.object(ollama, 'fetch_models', return_value=['cedar:8b', 'juniper:4b']))
    mocks.enter_context(patch.object(ollama, 'show_model', return_value={'capabilities': ['completion', 'vision', 'thinking']}))
    storage = ChatStorage(temporary)
    app = Adw.Application(application_id='io.github.jackrabbithanna.Gnollama.LayoutReview', flags=Gio.ApplicationFlags.NON_UNIQUE)
    app.register(None)
    Gtk.Widget.set_default_direction(Gtk.TextDirection.RTL if args.language == 'ar' else Gtk.TextDirection.LTR)
    window = GnollamaWindow(application=app, storage=storage)
    window.present()
    pump(lambda: storage.services.idle and storage.writer.idle)
    captures, heartbeats = [], []
    previous = [time.monotonic()]
    def beat():
        now = time.monotonic()
        heartbeats.append((now - previous[0]) * 1000)
        previous[0] = now
        return True
    heartbeat = GLib.timeout_add(10, beat)
    def capture(name, width, dark=False, rtl=False):
        rtl = rtl or args.language == 'ar'
        Gtk.Widget.set_default_direction(Gtk.TextDirection.RTL if rtl else Gtk.TextDirection.LTR)
        window.set_direction(Gtk.TextDirection.RTL if rtl else Gtk.TextDirection.LTR)
        window.set_visible(False)
        window.set_default_size(width, 900)
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK if dark else Adw.ColorScheme.FORCE_LIGHT)
        window.present()
        start = time.monotonic()
        pump(lambda: window.get_mapped() and time.monotonic() - start > .3)
        assert window.get_width() <= width, (name, width, window.get_width())
        if name.startswith('chat'):
            assert tab.chat_input.entry.view.get_width() > 180
        print(name, window.get_width(), window.get_height(), flush=True)
        window.allocate(window.get_width(), window.get_height(), -1, None)
        window.get_child().allocate(window.get_width(), window.get_height(), -1, None)
        if name.startswith('comparison'):
            visible = [(box, bubble) for box, bubble, stop in comparison.results.values() if box.get_parent()]
            assert all(bubble.bubble_box.get_width() >= box.get_width() - 60 for box, bubble in visible)
            if len(visible) == 2:
                assert abs(visible[0][1].bubble_box.get_width() - visible[1][1].bubble_box.get_width()) <= 2
        snapshot = Gtk.Snapshot()
        snapshot.render_background(window.get_style_context(), 0, 0, window.get_width(), window.get_height())
        window.snapshot_child(window.get_child(), snapshot)
        texture = window.get_renderer().render_texture(snapshot.to_node(), None)
        texture.save_to_png(str(output / (name + '.png')))
        captures.append(dict(name=name, width=width, height=window.get_height()))
    tab = window.tabs()[0]
    messages = [dict(uid=str(i), role='user' if i % 2 == 0 else 'assistant',
        content='Explain how drafts survive a restart.' if i % 2 == 0 else
        'Draft text, images, and settings are saved together.\n\n```python\ndef recover_draft(storage, draft_id):\n    return storage.get_draft(draft_id)\n```') for i in range(1000)]
    storage.save_chat(tab.strategy.chat_id, messages).result(5)
    tab.strategy.history = messages
    tab.strategy._saved_count = len(messages)
    window.close_tab(tab)
    pump(lambda: not window.tabs())
    page = storage.conversation_page(tab.strategy.chat_id)
    tab = window.open_chat_tab(page)
    pump(lambda: storage.services.idle)
    rendered = 0
    child = tab.message_list.list_box.get_first_child()
    while child:
        rendered += 1
        child = child.get_next_sibling()
    assert rendered == 50, rendered
    tab.chat_input.entry.restore_draft('  Keep the code indentation.\n  Explain the recovery path next.')
    capture('chat-wide-light', 1200)
    window.split_view.set_show_sidebar(False)
    capture('chat-narrow-dark', 360, True)
    comparison = window.new_comparison_tab()
    pump(lambda: all(p.input.get_selected_model() for p in comparison.targets))
    comparison.targets[1].input.select_model('juniper:4b')
    pump(lambda: all(not p.input.capabilities_loading for p in comparison.targets))
    comparison.chat_input.entry.restore_draft('Explain durable draft recovery in two sentences.')
    def response(**kwargs):
        content = ('```python\nresult = persist_draft(text, images, settings)\n```' if kwargs['model'] == 'cedar:8b' else
                   'Save the editor text and attachment bytes together.\n\n```markdown\n# Literal source stays available\n```')
        yield dict(message={'content': content}, done=True, eval_count=42)
    with patch.object(ollama, 'chat', side_effect=response):
        comparison.send_or_stop()
        pump(lambda: comparison.request is None and storage.writer.idle)
    assert len(comparison.results) == 2, comparison.notice.get_text()
    capture('comparison-wide-light', 1200)
    capture('comparison-narrow-dark', 360, True)
    capture('comparison-narrow-rtl', 360, False, True)
    for box, bubble, stop in comparison.results.values():
        bubble.api_expander.set_expanded(True)
    capture('comparison-api-wide-light', 1200)
    capture('comparison-api-narrow-dark', 360, True)
    conversation = window.new_model_conversation_tab()
    pump(lambda: all(p.input.get_selected_model() and not p.input.capabilities_loading for p in conversation.targets))
    conversation.targets[1].input.select_model('juniper:4b')
    conversation.targets[0].system_entry.restore_draft('Propose a practical design. Explain one improvement per turn.')
    conversation.targets[1].system_entry.restore_draft('Review the proposal. Identify a weakness and suggest a concrete refinement.')
    conversation.chat_input.entry.restore_draft('Design reliable draft recovery for a desktop chat application.')
    conversation.rounds.set_value(2)
    pump(lambda: all(not p.input.capabilities_loading for p in conversation.targets))
    capture('model-conversation-setup-wide-light', 1200)
    capture('model-conversation-setup-narrow-dark', 360, True)
    # Pause after a real response, so the saved transcript also exercises Resume.
    previous_finished = conversation.controller.finished
    def conversation_finished(uid, state):
        previous_finished(uid, state)
        if conversation.controller.run['next_turn'] == 2:
            conversation.controller.pause()
    conversation.controller.finished = conversation_finished
    with patch.object(ollama, 'chat', side_effect=response):
        conversation.start()
        pump(lambda: conversation.request is None and storage.writer.idle)
    assert conversation.controller.run['status'] == 'paused', conversation.notice.get_text()
    assert len(conversation.bubbles) == 2
    capture('model-conversation-transcript-wide-light', 1200)
    capture('model-conversation-transcript-narrow-dark', 360, True)
    capture('model-conversation-transcript-narrow-rtl', 360, False, True)
    for bubble in conversation.bubbles.values():
        bubble.api_expander.set_expanded(True)
    capture('model-conversation-api-narrow-dark', 360, True)
    GLib.source_remove(heartbeat)
    (output / 'layout-report.json').write_text(json.dumps(dict(captures=captures, rendered_messages=rendered,
        stored_messages=1000, heartbeat_max_ms=max(heartbeats), heartbeat_samples=len(heartbeats)), indent=2) + '\n')
    window.request_shutdown()
    pump(lambda: window._allow_close)
