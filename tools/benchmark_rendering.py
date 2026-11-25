#!/usr/bin/env python3
"""Measure what one streamed flush costs as a response grows. Needs a display."""
import argparse
import gettext
import json
import platform
from pathlib import Path
import statistics
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
gettext.install('tureen')
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gio, GLib, Gtk
from src.markdown_view import MarkdownView

PARAGRAPH = ('Streaming responses arrive a few words at a time, and **every** flush has to turn the '
             'text so far into widgets. This sentence has `inline code`, a [link](https://example.org), '
             'and inline math \\(a^2 + b^2 = c^2\\) so the usual conversions all run.\n\n')
SECTIONS = [
    '## Section {n}\n\n' + PARAGRAPH + PARAGRAPH,
    '- first point of section {n}\n- second point with *emphasis*\n    - a nested point\n- third point\n\n',
    '```python\n' + ''.join(f'def function_{{n}}_{i}(value):\n    return value * {i}  # line {i}\n' for i in range(8)) + '```\n\n',
    '| Name | Quantity | Note |\n|:--|--:|:-:|\n' + ''.join(f'| row {i} of {{n}} | {i * 7} | **ok** |\n' for i in range(6)) + '\n',
    'The result follows:\n\n\\[\nx_{n} = \\frac{{-b \\pm \\sqrt{{b^2 - 4ac}}}}{{2a}}\n\\]\n\n',
    '1. step one of section {n}\n2. step two\n3. step three\n\n' + PARAGRAPH,
]


def fixture(kind, size):
    """Deterministic Markdown of at least `size` bytes."""
    parts, n = [], 0
    while sum(map(len, parts)) < size:
        parts.append(PARAGRAPH if kind == 'prose' else SECTIONS[n % len(SECTIONS)].replace('{n}', str(n)))
        n += 1
    return ''.join(parts)


def settle():
    context = GLib.MainContext.default()
    deadline = time.monotonic() + .2
    while time.monotonic() < deadline:
        while context.pending():
            context.iteration(False)
        time.sleep(.002)


def count_children(view):
    count, child = 0, view.get_first_child()
    while child:
        count, child = count + 1, child.get_next_sibling()
    return count


def measure(kind, size, flushes, step):
    text = fixture(kind, size)
    view = MarkdownView()
    scrolled = Gtk.ScrolledWindow(child=view)
    window = Gtk.Window(default_width=800, default_height=600, child=scrolled)
    window.present()
    position = size - flushes * step
    view.update(text[:position], streaming=True)
    settle()
    width = max(view.get_width(), 400)
    updates, layouts = [], []
    for _ in range(flushes):
        position += step
        start = time.perf_counter()
        view.update(text[:position], streaming=True)
        middle = time.perf_counter()
        # The changed blocks are laid out again; unchanged ones answer from their cache.
        view.measure(Gtk.Orientation.VERTICAL, width)
        end = time.perf_counter()
        updates.append((middle - start) * 1000)
        layouts.append((end - middle) * 1000)
        settle()
    blocks = count_children(view)
    window.destroy()
    settle()
    return dict(bytes=position, blocks=blocks,
                update_ms_median=round(statistics.median(updates), 3), update_ms_max=round(max(updates), 3),
                layout_ms_median=round(statistics.median(layouts), 3), layout_ms_max=round(max(layouts), 3))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--resource', required=True, help='Compiled tureen.gresource')
    parser.add_argument('--output', default='rendering-benchmark.json')
    parser.add_argument('--sizes', default='5,20,50', help='Response sizes in kB')
    parser.add_argument('--flushes', type=int, default=30)
    parser.add_argument('--step', type=int, default=24, help='Characters added per flush')
    args = parser.parse_args()
    Gio.Resource.load(args.resource)._register()
    Adw.init()
    if not Gtk.init_check():
        raise SystemExit('A display is required')
    results = dict(python=platform.python_version(), machine=platform.machine(),
                   gtk='.'.join(map(str, (Gtk.get_major_version(), Gtk.get_minor_version(), Gtk.get_micro_version()))),
                   flushes=args.flushes, step=args.step, fixtures={})
    for kind in ('mixed', 'prose'):
        results['fixtures'][kind] = {size + 'kB': measure(kind, int(size) * 1000, args.flushes, args.step)
                                     for size in args.sizes.split(',')}
    Path(args.output).write_text(json.dumps(results, indent=2) + '\n')
    print(json.dumps(results, indent=2))


if __name__ == '__main__':
    main()
