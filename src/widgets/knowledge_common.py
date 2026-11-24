"""Adaptive Knowledge Library, source selection, and retrieval inspection."""
from gettext import gettext as _
from gi.repository import Adw, Gtk, Gio, GLib, Pango

from .search import searchable
from ..knowledge import (PRESETS, embedding_host_key, new_config)


def label(text='', **kwargs):
    kwargs.setdefault('selectable', True)
    return Gtk.Label(label=text, xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, **kwargs)


def dropdown(names):
    widget = Gtk.DropDown.new_from_strings(names)
    factory = Gtk.SignalListItemFactory()
    factory.connect('setup', lambda f, item: item.set_child(Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, max_width_chars=28)))
    factory.connect('bind', lambda f, item: item.get_child().set_text(item.get_item().get_string()))
    widget.set_factory(factory)
    return searchable(widget)


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


def verification_notice(context, kind):
    """What to say of an embedding model on a host of type `kind` that gives no content digest, else ''."""
    try:
        verified = context.capabilities(dict(provider=kind)).model_verification
    except ValueError:
        return ''  # A type this version does not know; its host cannot be found either.
    return '' if verified else _('This model cannot be fully verified: it is recognized by what the server reports about it, not by its contents, so a replacement that reports the same would go unnoticed.')


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
        self.context.services.control.submit(task)


class HostModels(Gtk.Box):
    """Explicit embedding host/model picker with stale-result suppression."""
    def __init__(self, context, host='', model='', on_change=lambda: None, kind=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.context, self.on_change = context, on_change
        self.storage = context.storage
        self.hosts = [host for host in context.storage.get_all_hosts() if context.capabilities(host).embeddings]
        self.models = []
        self.cancel = None
        self.generation = 0
        self.desired_model = model
        self.host_dropdown = dropdown([h['name'] for h in self.hosts])
        self.model_dropdown = dropdown([])
        self.notice = label()
        self.verification = label(visible=False)
        self.verification.add_css_class('dim-label')
        field(self, _('Embedding server'), self.host_dropdown)
        field(self, _('Embedding model'), self.model_dropdown)
        self.append(self.notice)
        self.append(self.verification)
        self.append(actions(button(_('Manage Models…'), self.manage_models),
                            button(_('Refresh'), self.refresh)))
        self.select_host(host, kind)
        self.host_dropdown.connect('notify::selected', self.refresh)
        self.model_dropdown.connect('notify::selected', lambda *args: self.on_change())
        self.refresh()

    def select_host(self, host, kind=None):
        """Select the host of type `kind` at the address `host`, as a collection, index, or chat stores it."""
        key = embedding_host_key(host, kind)
        for i, record in enumerate(self.hosts):
            if key and embedding_host_key(record['hostname'], record.get('provider')) == key:
                self.host_dropdown.set_selected(i)
                break

    def manage_models(self, *args):
        from ..model_manager import ModelManagerDialog
        root = self.get_root()
        if not isinstance(root, Gtk.Window):
            return
        managed = hasattr(root, 'on_manage_models')
        manager = root.on_manage_models() if managed else ModelManagerDialog(
            self.context, is_model_busy=self.context.knowledge.busy)
        i = self.host_dropdown.get_selected()
        selected = self.hosts[i]['id'] if i < len(self.hosts) else None
        for n, host in enumerate(manager.host_list):
            if host['id'] == selected:
                manager.host_dropdown.set_selected(n)
                break
        # Refresh after a model has been downloaded without requiring the picker to close.
        manager.connect('closed', lambda *args: self.refresh() if self.get_mapped() else None)
        if not managed:
            manager.present(root)

    def stop(self):
        self.generation += 1
        if self.cancel:
            self.cancel.cancel()

    def host(self):
        i = self.host_dropdown.get_selected()
        return self.hosts[i]['hostname'] if i < len(self.hosts) else ''

    def kind(self):
        i = self.host_dropdown.get_selected()
        return (self.hosts[i].get('provider') or 'ollama') if i < len(self.hosts) else 'ollama'

    def connection(self):
        i = self.host_dropdown.get_selected()
        return self.context.connection(self.hosts[i]) if i < len(self.hosts) else None

    @staticmethod
    def _empty_notice(host):
        if host is None or host.capabilities.model_management:
            return _('No embedding models found. Download one in Manage Models, then refresh.')
        # A hosted service, whose models the user cannot add to.
        if host.fixed_url:
            return _('No embedding models are available. Try refreshing later.')
        return _('No embedding models found on this server. Load one there, then refresh.')

    def model(self):
        i = self.model_dropdown.get_selected()
        return self.models[i] if i < len(self.models) else None

    def refresh(self, *args):
        self.stop()
        generation = self.generation
        cancel = self.cancel = Gio.Cancellable()
        host = self.connection()
        self.models = []
        self.model_dropdown.set_model(Gtk.StringList.new([]))
        self.notice.set_text(_('Checking embedding models…'))
        notice = verification_notice(self.context, self.kind()) if host else ''
        self.verification.set_text(notice)
        self.verification.set_visible(bool(notice))
        self.on_change()
        def fetch():
            models, error = [], None
            try:
                models = sorted(host.list_embedding_models(cancellable=cancel) if host else [],
                                key=lambda model: model['name'].casefold())
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
                self.notice.set_text(error or ('' if models else self._empty_notice(host)))
                self.on_change()
                return False
            GLib.idle_add(deliver)
        self.context.services.control.submit(fetch)


class IndexDialog(WorkDialog):
    def __init__(self, context, document, on_started, defaults=None):
        super().__init__(_('Create Embeddings'))
        self.context, self.document = context, document
        self.storage = context.storage
        self.defaults = defaults
        self.box.append(label(document['title']))
        self.host_models = HostModels(context, (defaults or {}).get('host', ''), (defaults or {}).get('model', ''),
                                      on_change=self._model_changed, kind=(defaults or {}).get('host_kind'))
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
            config = context.storage.library.embedding_config(defaults['config_id'])
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
            self.context.knowledge.create_index(self.document['id'], *self.settings(), kind=self.host_models.kind())
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


