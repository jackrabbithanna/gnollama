"""Adaptive Knowledge Library, source selection, and retrieval inspection."""
import copy
import json
import time
from gettext import ngettext
from gi.repository import Adw, Gtk, Gio, GLib, Pango

from .. import ollama
from ..knowledge import (DEFAULT_RAG, PRESETS, canonical_model, check_cancel, extract_document, make_document,
                         model_identity, new_config, validate_rag)
from ..session import worker
from ..vectors import SEARCH_METRICS
from .json_view import TextEditor, buffer_text


def label(text='', **kwargs):
    kwargs.setdefault('selectable', True)
    return Gtk.Label(label=text, xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, **kwargs)


def dropdown(names):
    widget = Gtk.DropDown.new_from_strings(names)
    factory = Gtk.SignalListItemFactory()
    factory.connect('setup', lambda f, item: item.set_child(Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, max_width_chars=28)))
    factory.connect('bind', lambda f, item: item.get_child().set_text(item.get_item().get_string()))
    widget.set_factory(factory)
    widget.set_enable_search(True)
    return widget


def button(text, callback):
    widget = Gtk.Button(label=text)
    widget.connect('clicked', callback)
    return widget


def clear(box):
    while box.get_first_child():
        box.remove(box.get_first_child())


def actions(*buttons):
    flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, min_children_per_line=1,
                       max_children_per_line=4, column_spacing=6, row_spacing=6)
    for item in buttons:
        flow.append(item)
    return flow


def field(box, title, widget):
    box.append(label(title, selectable=False, mnemonic_widget=widget))
    box.append(widget)


def section(box, title, expanded=False):
    content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=12)
    expander = Gtk.Expander(label=title, child=content, expanded=expanded)
    expander.add_css_class('knowledge-section')
    box.append(expander)
    return content, expander


def document_status(member):
    if member['status'] == 'complete' and member['chunks']:
        return _('Ready')
    if member['status'] in ('failed', 'interrupted'):
        return _('Needs Attention')
    if member['status'] in ('pending', 'indexing'):
        return _('Preparing')
    return _('Needs Preparation')


def config_label(config):
    return '{0} · {1} · {2} · {3}'.format(config['model'], config['preset'],
                                         config['dimensions'] or _('Native dimensions'), config['digest'][:10])


def preview_text(document):
    if not document['pages']:
        return document['text']
    return '\n\n'.join(_('Page {0}').format(page['page']) + '\n' +
                       document['text'][page['start']:page['end']] for page in document['pages'])


def plain_editor(dialog):
    dialog.editor.set_direction(Gtk.TextDirection.NONE)
    buffer = dialog.editor.get_buffer()
    if hasattr(buffer, 'set_language'):
        buffer.set_language(None)
    return dialog


class WorkDialog(Adw.Dialog):
    def __init__(self, title, width=700, height=600):
        super().__init__(title=title, content_width=width, content_height=height)
        self.cancel = Gio.Cancellable()
        self.closed = False
        self.connect('closed', self._closed)
        toolbar = self.toolbar = Adw.ToolbarView()
        self.header = Adw.HeaderBar()
        toolbar.add_top_bar(self.header)
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                           margin_start=12, margin_end=12, margin_top=12, margin_bottom=12)
        toolbar.set_content(Gtk.ScrolledWindow(child=self.box, hscrollbar_policy=Gtk.PolicyType.NEVER))
        self.error = label(visible=False)
        self.error.add_css_class('error')
        self.box.append(self.error)
        self.set_child(toolbar)

    def _closed(self, *args):
        self.closed = True
        self.cancel.cancel()

    def show_error(self, error):
        self.error.set_text(str(error))
        self.error.set_visible(True)
        self.error.grab_focus()

    def run(self, function, callback):
        def task():
            value = error = None
            try:
                value = function(self.cancel)
            except Exception as exc:
                error = exc
            def deliver():
                if not self.closed:
                    callback(value, error)
                return False
            GLib.idle_add(deliver)
        storage = getattr(self, 'storage', None)
        (storage.services.control if storage else worker).submit(task)


class HostModels(Gtk.Box):
    """Explicit embedding host/model picker with stale-result suppression."""
    def __init__(self, storage, host='', model='', on_change=lambda: None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.storage, self.on_change = storage, on_change
        self.hosts = [host for host in storage.get_all_hosts() if not ollama.is_cloud(host)]
        self.models = []
        self.cancel = None
        self.generation = 0
        self.desired_model = model
        self.host_dropdown = dropdown([h['name'] for h in self.hosts])
        self.model_dropdown = dropdown([])
        self.model_dropdown.set_enable_search(True)
        self.notice = label()
        field(self, _('Embedding server'), self.host_dropdown)
        field(self, _('Embedding model'), self.model_dropdown)
        self.append(self.notice)
        self.append(actions(button(_('Manage Models…'), self.manage_models),
                            button(_('Refresh'), self.refresh)))
        for i, h in enumerate(self.hosts):
            if h['hostname'].rstrip('/') == host.rstrip('/'):
                self.host_dropdown.set_selected(i)
                break
        self.host_dropdown.connect('notify::selected', self.refresh)
        self.model_dropdown.connect('notify::selected', lambda *args: self.on_change())
        self.refresh()

    def manage_models(self, *args):
        from ..model_manager import ModelManagerDialog
        root = self.get_root()
        if not isinstance(root, Gtk.Window):
            return
        manager = root.on_manage_models() if hasattr(root, 'on_manage_models') else ModelManagerDialog(
            self.storage, is_model_busy=self.storage.knowledge.busy, transient_for=root)
        for n, host in enumerate(manager.host_list):
            if host['hostname'].rstrip('/') == self.host().rstrip('/'):
                manager.host_dropdown.set_selected(n)
                break
        # Refresh after a model has been downloaded without requiring the picker to close.
        manager.connect('close-request', lambda *args: (self.refresh() if self.get_mapped() else None) or False)
        manager.present()

    def stop(self):
        self.generation += 1
        if self.cancel:
            self.cancel.cancel()

    def host(self):
        i = self.host_dropdown.get_selected()
        return self.hosts[i]['hostname'] if i < len(self.hosts) else ''

    def model(self):
        i = self.model_dropdown.get_selected()
        return self.models[i] if i < len(self.models) else None

    def refresh(self, *args):
        self.stop()
        generation = self.generation
        cancel = self.cancel = Gio.Cancellable()
        host = self.host()
        self.models = []
        self.model_dropdown.set_model(Gtk.StringList.new([]))
        self.notice.set_text(_('Checking embedding models…'))
        self.on_change()
        def fetch():
            models, error = [], None
            try:
                for model in ollama.fetch_model_details(host, cancellable=cancel) if host else []:
                    check_cancel(cancel)
                    try:
                        info = ollama.show_model(host, model['name'], cancellable=cancel)
                        capabilities = info.get('capabilities')
                    except ollama.RequestCancelled:
                        raise
                    except ollama.OllamaError:
                        capabilities = None
                    if capabilities is None or 'embedding' in capabilities:
                        models.append(model)
            except Exception as exc:
                error = str(exc)
            def deliver():
                if generation != self.generation:
                    return False
                self.models = models
                self.model_dropdown.set_model(Gtk.StringList.new([m['name'] for m in models]))
                for i, model in enumerate(models):
                    if model['name'] == self.desired_model:
                        self.model_dropdown.set_selected(i)
                        break
                self.notice.set_text(error or ('' if models else _('No embedding models found. Download one in Manage Models, then refresh.')))
                self.on_change()
                return False
            GLib.idle_add(deliver)
        self.storage.services.control.submit(fetch)


class IndexDialog(WorkDialog):
    def __init__(self, storage, document, on_started, defaults=None):
        super().__init__(_('Create Embeddings'))
        self.storage, self.document = storage, document
        self.defaults = defaults
        self.box.append(label(document['title']))
        self.host_models = HostModels(storage, (defaults or {}).get('host', ''),
                                      (defaults or {}).get('model', ''), on_change=self._model_changed)
        self.box.append(self.host_models)
        self.connect('closed', lambda *args: self.host_models.stop())
        self.preset = Gtk.DropDown.new_from_strings([_('Plain'), _('EmbeddingGemma retrieval'), _('Nomic retrieval'), _('Qwen3 retrieval'), _('Custom prefixes')])
        self.advanced, self.advanced_expander = section(self.box, _('Advanced Embedding Settings'))
        field(self.advanced, _('Embedding format'), self.preset)
        self.document_prefix = Gtk.Entry()
        self.query_prefix = Gtk.Entry()
        field(self.advanced, _('Document prefix'), self.document_prefix)
        field(self.advanced, _('Query prefix'), self.query_prefix)
        self.dimensions = Gtk.Entry(placeholder_text=_('Model default'))
        self.size = Gtk.SpinButton.new_with_range(64, 32000, 1)
        self.size.set_value(1600)
        self.overlap = Gtk.SpinButton.new_with_range(0, 31999, 1)
        self.overlap.set_value(200)
        for title, widget in [(_('Dimensions'), self.dimensions), (_('Chunk size (characters)'), self.size),
                              (_('Overlap (characters)'), self.overlap)]:
            field(self.advanced, title, widget)
        self.preset.connect('notify::selected', self._preset_changed)
        self._preset_changed()
        self.start = button(_('Create'), lambda *args: self._start(on_started))
        self.start.add_css_class('suggested-action')
        self.header.pack_end(self.start)
        if defaults:
            config = storage.library.embedding_config(defaults['config_id'])
            self.preset.set_selected(list(PRESETS).index(config['preset']))
            self.document_prefix.set_text(config['document_prefix'])
            self.query_prefix.set_text(config['query_prefix'])
            self.dimensions.set_text(str(config['requested_dimensions'] or ''))
            self.size.set_value(defaults['chunk_size'])
            self.overlap.set_value(defaults['overlap'])

    def _model_changed(self):
        if not hasattr(self, 'preset') or self.defaults:
            return
        model = self.host_models.model()
        name = model['name'] if model else ''
        self.preset.set_selected(1 if 'embeddinggemma' in name else 2 if 'nomic-embed-text' in name
                                 else 3 if 'qwen3-embedding' in name.lower() else 0)

    def _preset_changed(self, *args):
        key = list(PRESETS)[self.preset.get_selected()]
        if key != 'custom':
            self.document_prefix.set_text(PRESETS[key][0])
            self.query_prefix.set_text(PRESETS[key][1])
        self.document_prefix.set_editable(key == 'custom')
        self.query_prefix.set_editable(key == 'custom')

    def _start(self, callback):
        try:
            self.storage.knowledge.create_index(self.document['id'], *self.settings())
            callback()
            self.close()
        except (ValueError, TypeError) as exc:
            self.show_error(exc)


    def settings(self):
        model = self.host_models.model()
        if not model:
            raise ValueError(_('Select an embedding model.'))
        if not model.get('digest'):
            raise ValueError(_('The host did not provide a model digest.'))
        config = new_config(model['name'], model['digest'], list(PRESETS)[self.preset.get_selected()],
                            int(self.dimensions.get_text()) if self.dimensions.get_text().strip() else None,
                            self.document_prefix.get_text(), self.query_prefix.get_text())
        size, overlap = self.size.get_value_as_int(), self.overlap.get_value_as_int()
        if overlap >= size:
            raise ValueError(_('Overlap must be smaller than the chunk size.'))
        return self.host_models.host(), model['name'], config, size, overlap


