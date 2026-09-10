"""Adaptive Knowledge Library, source selection, and retrieval inspection."""
import copy
import json
import time
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


def config_label(config):
    return '{0} · {1} · {2} · {3}'.format(config['model'], config['preset'],
                                         config['dimensions'] or _('Native dimensions'), config['digest'][:10])


def preview_text(document):
    if not document['pages']:
        return document['text']
    return '\n\n'.join(_('Page {0}').format(page['page']) + '\n' +
                       document['text'][page['start']:page['end']] for page in document['pages'])


def plain_editor(dialog):
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
        toolbar = Adw.ToolbarView()
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
        worker.submit(task)


class HostModels(Gtk.Box):
    """Explicit embedding host/model picker with stale-result suppression."""
    def __init__(self, storage, host='', model='', on_change=lambda: None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.storage, self.on_change = storage, on_change
        self.hosts = storage.get_all_hosts()
        self.models = []
        self.cancel = None
        self.generation = 0
        self.desired_model = model
        self.host_dropdown = dropdown([h['name'] for h in self.hosts])
        self.model_dropdown = dropdown([])
        self.model_dropdown.set_enable_search(True)
        self.notice = label()
        self.append(label(_('Embedding host')))
        self.append(self.host_dropdown)
        self.append(label(_('Embedding model')))
        self.append(self.model_dropdown)
        self.append(self.notice)
        for i, h in enumerate(self.hosts):
            if h['hostname'].rstrip('/') == host.rstrip('/'):
                self.host_dropdown.set_selected(i)
                break
        self.host_dropdown.connect('notify::selected', self.refresh)
        self.model_dropdown.connect('notify::selected', lambda *args: self.on_change())
        self.refresh()

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
                self.notice.set_text(error or ('' if models else _('No embedding models found. Pull one in Manage Models, then refresh.')))
                self.on_change()
                return False
            GLib.idle_add(deliver)
        worker.submit(fetch)


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
        self.box.append(label(_('Embedding format')))
        self.box.append(self.preset)
        self.document_prefix = Gtk.Entry()
        self.query_prefix = Gtk.Entry()
        self.box.append(label(_('Document prefix')))
        self.box.append(self.document_prefix)
        self.box.append(label(_('Query prefix')))
        self.box.append(self.query_prefix)
        self.dimensions = Gtk.Entry(placeholder_text=_('Model default'))
        self.size = Gtk.SpinButton.new_with_range(64, 32000, 1)
        self.size.set_value(1600)
        self.overlap = Gtk.SpinButton.new_with_range(0, 31999, 1)
        self.overlap.set_value(200)
        for title, widget in [(_('Dimensions'), self.dimensions), (_('Chunk size (characters)'), self.size),
                              (_('Overlap (characters)'), self.overlap)]:
            self.box.append(label(title))
            self.box.append(widget)
        self.preset.connect('notify::selected', self._preset_changed)
        self._preset_changed()
        self.start = button(_('Create'), lambda *args: self._start(on_started))
        self.start.add_css_class('suggested-action')
        self.header.pack_end(self.start)
        if defaults:
            config = storage.db.embedding_config(defaults['config_id'])
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


class CollectionDialog(IndexDialog):
    def __init__(self, storage, on_created, collection=None):
        super().__init__(storage, dict(title=_('Documents share these embedding settings.')), on_created, collection)
        self.set_title(_('Copy Collection with New Settings') if collection else _('Create Collection'))
        self.name = Gtk.Entry(placeholder_text=_('Collection name'),
                              text=_('{0} (copy)').format(collection['name']) if collection else '')
        self.box.prepend(self.name)
        self.document_ids = [m['id'] for m in storage.db.collection_documents(collection['id'])] if collection else []

    def _start(self, callback):
        try:
            def done(id, error):
                if self.closed:
                    return
                self.start.set_sensitive(True)
                if error:
                    self.show_error(error)
                else:
                    callback(id)
                    self.close()
            self.storage.knowledge.create_collection(self.name.get_text(), *self.settings(),
                                                      document_ids=self.document_ids, callback=done)
            self.start.set_sensitive(False)
        except (ValueError, TypeError) as exc:
            self.show_error(exc)


class DocumentPicker(WorkDialog):
    def __init__(self, storage, collection_id, on_added):
        super().__init__(_('Add Existing Documents'))
        self.storage, self.collection_id, self.on_added = storage, collection_id, on_added
        self.selected = set()
        members = {m['id'] for m in storage.db.collection_documents(collection_id)}
        self.documents = [d for d in storage.db.knowledge_documents() if d['id'] not in members]
        self.search = Gtk.SearchEntry(placeholder_text=_('Search documents'))
        self.search.connect('search-changed', self._render)
        self.box.append(self.search)
        self.rows = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.box.append(self.rows)
        self.add = button(_('Add'), self._add)
        self.add.add_css_class('suggested-action')
        self.header.pack_end(self.add)
        self._render()

    def _render(self, *args):
        clear(self.rows)
        query = self.search.get_text().casefold()
        for doc in self.documents:
            if query not in (doc['title'] + '\n' + doc['filename']).casefold():
                continue
            check = Gtk.CheckButton(child=label(doc['title'], selectable=False), active=doc['id'] in self.selected)
            check.connect('toggled', self._toggle, doc['id'])
            self.rows.append(check)
        if not self.documents:
            self.rows.append(label(_('All library documents are already in this collection.')))
        self.add.set_sensitive(bool(self.selected))

    def _toggle(self, check, id):
        self.selected.add(id) if check.get_active() else self.selected.discard(id)
        self.add.set_sensitive(bool(self.selected))

    def _add(self, *args):
        try:
            self.storage.knowledge.build_collection(self.collection_id, sorted(self.selected))
            self.on_added()
            self.close()
        except ValueError as exc:
            self.show_error(exc)


class SourcesView(Gtk.Expander):
    def __init__(self, snapshot):
        super().__init__(label=_('Sources used ({0})').format(len(snapshot['hits'])))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.set_child(box)
        box.append(label(_('Search query: {0}').format(snapshot['query'])))
        box.append(label(config_label(snapshot['config']) + '\n' + snapshot['host']))
        metric = snapshot.get('metric', 'cosine')
        box.append(label({
            'cosine': _('Cosine similarity · Higher is closer'),
            'euclidean': _('Euclidean distance (L2) · Lower is closer'),
            'manhattan': _('Manhattan distance (L1) · Lower is closer'),
        }.get(metric, _('Unknown similarity measure'))))
        if snapshot.get('collections'):
            box.append(label(_('Collections: {0}').format(', '.join(c['name'] for c in snapshot['collections']))))
        for i, hit in enumerate(snapshot['hits'], 1):
            pages = ', '.join(map(str, hit['pages']))
            value = (_('Similarity: {0:.4f}') if metric == 'cosine' else _('Distance: {0:.4f}')).format(hit['score'])
            title = '[S{0}] {1} · {2}'.format(i, hit['title'], value)
            if pages:
                title += ' · ' + _('Pages: {0}').format(pages)
            item = Gtk.Expander(label_widget=label(title, selectable=False))
            content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            source = hit.get('web_source')
            if source:
                content.append(Gtk.LinkButton(uri=source['final_url'], label=_('Open Source URL'), halign=Gtk.Align.START))
                content.append(label(source['final_url']))
                content.append(label(_('Fetched: {0}').format(time.strftime('%Y-%m-%d %H:%M', time.localtime(source['fetched_at'])))))
            content.append(label(hit['text'] + ('\n' + _('Shortened to fit the source budget.') if hit.get('truncated') else '')))
            item.set_child(content)
            box.append(item)
        box.append(label(_('Scores compare embeddings; they are not confidence estimates.')))


class SourcePicker(WorkDialog):
    def __init__(self, storage, options, on_apply=None):
        super().__init__(_('Knowledge Sources') if on_apply else _('Test Search'))
        self.storage, self.on_apply = storage, on_apply
        self.options = dict(copy.deepcopy(DEFAULT_RAG), **copy.deepcopy(options))
        self.collection_ids = set(self.options['collection_ids'])
        self.collections = storage.db.knowledge_collections()
        self.configs = [c for c in storage.db.embedding_configs() if any(group['config_id'] == c['id'] for group in self.collections)]
        self._refreshing = False
        self.config_dropdown = dropdown([config_label(c) for c in self.configs])
        self.box.append(label(_('Embedding configuration')))
        self.box.append(self.config_dropdown)
        for i, config in enumerate(self.configs):
            if config['id'] == self.options['config_id']:
                self.config_dropdown.set_selected(i)
                break
        if self.options['config_id'] and not any(c['id'] == self.options['config_id'] for c in self.configs):
            self.config_dropdown.set_selected(Gtk.INVALID_LIST_POSITION)
        self.host_models = HostModels(storage, self.options['host'], self.options['model'])
        self.box.append(self.host_models)
        self.connect('closed', lambda *args: self.host_models.stop())
        self.box.append(button(_('Refresh models'), lambda *args: self.host_models.refresh()))
        self.collection_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.box.append(self.collection_box)
        if self.options['selection']:
            self.box.append(label(_('This chat has older individual sources. Applying a collection selection replaces them. Closing this dialog keeps them unchanged.')))
        self.count = Gtk.SpinButton.new_with_range(1, 20, 1)
        self.count.set_value(self.options['count'])
        self.budget = Gtk.SpinButton.new_with_range(256, 64000, 256)
        self.budget.set_value(self.options['budget'])
        for title, widget in [(_('Maximum passages'), self.count), (_('Source budget (characters)'), self.budget)]:
            self.box.append(label(title))
            self.box.append(widget)
        self.box.append(label(_('Similarity measure')))
        self.metric_dropdown = dropdown([_('Cosine similarity'), _('Euclidean distance (L2)'), _('Manhattan distance (L1)')])
        metric = self.options['metric']
        self.metric_dropdown.set_selected(SEARCH_METRICS.index(metric) if metric in SEARCH_METRICS else Gtk.INVALID_LIST_POSITION)
        self.box.append(self.metric_dropdown)
        self.metric_notice = label()
        self.box.append(self.metric_notice)
        self.threshold_label = label()
        self.box.append(self.threshold_label)
        self.threshold = Gtk.Entry()
        self.box.append(self.threshold)
        self._metric_changed(clear_threshold=False)
        threshold = self.options['minimum'] if metric == 'cosine' else self.options['maximum']
        self.threshold.set_text('' if threshold is None else str(threshold))
        self.metric_dropdown.connect('notify::selected', self._metric_changed)
        self.query = Gtk.Entry(placeholder_text=_('Enter a search query'))
        self.box.append(self.query)
        self.search_button = button(_('Test Search'), self._search)
        self.box.append(self.search_button)
        self.results = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.box.append(self.results)
        if on_apply:
            apply = button(_('Apply'), self._apply)
            apply.add_css_class('suggested-action')
            self.header.pack_end(apply)
        self.config_dropdown.connect('notify::selected', self._config_changed)
        self._render_sources()
        self.storage.knowledge.listeners.append(self._refresh_sources)
        self.connect('closed', self._disconnect_sources)

    def _disconnect_sources(self, *args):
        if self._refresh_sources in self.storage.knowledge.listeners:
            self.storage.knowledge.listeners.remove(self._refresh_sources)

    def _refresh_sources(self):
        if self.closed:
            return
        config = self.config()
        id = config['id'] if config else None
        self.collections = self.storage.db.knowledge_collections()
        self.configs = [c for c in self.storage.db.embedding_configs() if any(group['config_id'] == c['id'] for group in self.collections)]
        self._refreshing = True
        self.config_dropdown.set_model(Gtk.StringList.new([config_label(c) for c in self.configs]))
        self.config_dropdown.set_selected(next((i for i, c in enumerate(self.configs) if c['id'] == id),
                                               Gtk.INVALID_LIST_POSITION if id else 0))
        self._refreshing = False
        self._render_sources()

    def config(self):
        i = self.config_dropdown.get_selected()
        return self.configs[i] if i < len(self.configs) else None

    def _config_changed(self, *args):
        if self._refreshing:
            return
        self.collection_ids.clear()
        config = self.config()
        if config:
            self.host_models.desired_model = config['model']
            self.host_models.refresh()
        self._render_sources()

    def metric(self):
        n = self.metric_dropdown.get_selected()
        return SEARCH_METRICS[n] if n < len(SEARCH_METRICS) else None

    def _metric_changed(self, *args, clear_threshold=True):
        metric = self.metric()
        cosine = metric == 'cosine'
        self.threshold_label.set_text(_('Minimum cosine similarity') if cosine else _('Maximum distance'))
        self.threshold.set_placeholder_text(_('No minimum') if cosine else _('No maximum'))
        if clear_threshold:
            self.threshold.set_text('')
            clear(self.results)
        self.metric_notice.set_text({
            'cosine': _('Higher is closer (−1 to 1). This is the default for semantic search.'),
            'euclidean': _('Lower is closer. With normalized embeddings, Euclidean and cosine usually rank results alike.'),
            'manhattan': _('Lower is closer. Adds absolute differences between vector components and can rank results differently from cosine.'),
        }.get(metric, _('Choose a supported similarity measure.')))

    def _render_sources(self):
        self._render_collections(self.config())

    def _render_collections(self, config):
        clear(self.collection_box)
        self.collection_box.append(label(_('Collections')))
        self.collection_box.append(label(_('Selected collections include every document and follow future additions. All documents must be ready to search.')))
        missing = self.collection_ids - {c['id'] for c in self.collections}
        if missing:
            self.collection_box.append(label(_('Some selected collections were deleted. Choose sources again.')))
            self.collection_box.append(button(_('Clear missing collections'), lambda *args: self._clear_collections(missing)))
        for collection in self.collections:
            compatible = config is not None and collection['config_id'] == config['id']
            text = _('{0} · {1}/{2} documents ready').format(collection['name'], collection['ready'], collection['documents'])
            if not compatible:
                text += '\n' + _('Different embedding configuration: {0}').format(config_label(collection))
            check = Gtk.CheckButton(child=label(text, selectable=False), sensitive=compatible,
                                     active=collection['id'] in self.collection_ids)
            check.connect('toggled', self._select_collection, collection)
            self.collection_box.append(check)
        if not self.collections:
            self.collection_box.append(label(_('Create collections in the Knowledge Library.')))

    def _clear_collections(self, ids):
        self.collection_ids.difference_update(ids)
        self._render_sources()

    def _select_collection(self, check, collection):
        if check.get_active():
            first = not self.collection_ids
            self.collection_ids.add(collection['id'])
            if first:
                self.host_models.desired_model = collection['model']
                for n, host in enumerate(self.host_models.hosts):
                    if host['hostname'].rstrip('/') == collection['host'].rstrip('/'):
                        self.host_models.host_dropdown.set_selected(n)
                        break
                self.host_models.refresh()
        else:
            self.collection_ids.discard(collection['id'])

    def current_options(self):
        config, model = self.config(), self.host_models.model()
        options = dict(enabled=True, config_id=config['id'] if config else None,
                       host=self.host_models.host(), model=model['name'] if model else '', selection={},
                       collection_ids=sorted(self.collection_ids),
                       count=self.count.get_value_as_int(), budget=self.budget.get_value_as_int(),
                       metric=self.metric(), minimum=None, maximum=None)
        if not options['collection_ids']:
            raise ValueError(_('Select at least one collection. Create collections in Knowledge first if needed.'))
        if self.threshold.get_text().strip():
            options['minimum' if self.metric() == 'cosine' else 'maximum'] = float(self.threshold.get_text())
        validate_rag(options)
        if model.get('digest') != config['digest']:
            raise ValueError(_('This model digest does not match the selected embedding configuration.'))
        return options

    def _apply(self, *args):
        try:
            self.on_apply(self.current_options())
            self.close()
        except (ValueError, TypeError) as exc:
            self.show_error(exc)

    def _search(self, *args):
        try:
            options = self.current_options()
            query = self.query.get_text().strip()
            if not query:
                raise ValueError(_('Enter a search query.'))
        except (ValueError, TypeError) as exc:
            self.show_error(exc)
            return
        self.search_button.set_sensitive(False)
        self.error.set_visible(False)
        def completed(snapshot, error):
            self.search_button.set_sensitive(True)
            clear(self.results)
            if error:
                self.show_error(error)
            else:
                view = SourcesView(snapshot)
                view.set_expanded(True)
                self.results.append(view)
        self.run(lambda cancel: self.storage.knowledge.retrieve(options, query, cancel), completed)


class ChunkPicker(WorkDialog):
    def __init__(self, storage, index, selected=None, on_apply=None):
        super().__init__(_('Document Chunks'))
        self.storage, self.index = storage, index
        self.selected = None if selected is None else set(selected)
        self.on_apply = on_apply
        self.all_ids = None
        self.checks = []
        self.offset = 0
        self.box.append(label(index['title']))
        if on_apply:
            self.box.append(actions(button(_('Select all'), lambda *args: self._all(True)),
                                    button(_('Select none'), lambda *args: self._all(False))))
            self.header.pack_end(button(_('Apply'), self._apply))
        self.rows = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.box.append(self.rows)
        self.more = button(_('Load more chunks'), self._load)
        self.box.append(self.more)
        self._load()
        if on_apply:
            def loaded(ids, error):
                if error:
                    self.show_error(error)
                else:
                    self.all_ids = ids
                    for check in self.checks:
                        check.set_sensitive(True)
            self.run(lambda cancel: self.storage.db.knowledge_chunk_ids(self.index['id']), loaded)

    def _all(self, all):
        self.selected = None if all else set()
        clear(self.rows)
        self.checks = []
        self.offset = 0
        self._load()

    def _apply(self, *args):
        self.on_apply(None if self.selected is None else sorted(self.selected))
        self.close()

    def _toggle(self, check, id):
        if self.selected is None:
            self.selected = set(self.all_ids or ())
        if check.get_active():
            self.selected.add(id)
        else:
            self.selected.discard(id)

    def _load(self, *args):
        chunks = self.storage.db.knowledge_chunk_page(self.index['id'], self.offset)
        for chunk in chunks:
            group = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            if self.on_apply:
                check = Gtk.CheckButton(label=_('Chunk {0}').format(chunk['ordinal'] + 1))
                check.set_active(self.selected is None or chunk['id'] in self.selected)
                check.connect('toggled', self._toggle, chunk['id'])
                check.set_sensitive(self.all_ids is not None)
                self.checks.append(check)
                group.append(check)
            expander = Gtk.Expander(label=_('Characters {0}–{1}').format(chunk['start'], chunk['end']))
            expander.set_child(label(chunk['text']))
            group.append(expander)
            group.append(button(_('Inspect vector'), lambda *args, chunk=chunk: self._vector(chunk)))
            self.rows.append(group)
        self.offset += len(chunks)
        self.more.set_visible(len(chunks) == 100)

    def _vector(self, chunk):
        from ..vectors import vector_values
        try:
            raw = self.storage.db.knowledge_vector(chunk['id'])
            values = vector_values(raw) if raw is not None else None
        except Exception as exc:
            self.show_error(exc)
            return
        if raw is None:
            self.show_error(_('This vector was deleted.'))
            return
        dialog = TextEditor(json.dumps(values, indent=2), lambda text: None,
                            title=_('Embedding Vector'), hint=_('Stored normalized vector. Changes are not saved.'), apply_label=_('Close'))
        dialog.editor.set_editable(False)
        self.connect('closed', lambda *args: dialog.close())
        dialog.present(self)


class KnowledgeControl(Gtk.Box):
    def __init__(self, storage, on_change):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin_start=12, margin_end=12)
        self.storage, self.on_change = storage, on_change
        self.options = copy.deepcopy(DEFAULT_RAG)
        self.dialog = None
        self.restoring = False
        self.toggle = Gtk.CheckButton(label=_('Use Knowledge'))
        self.choose = button(_('Choose sources…'), self.open_picker)
        self.append(actions(self.toggle, self.choose))
        self.notice = label()
        self.append(self.notice)
        self.query = Gtk.Entry(placeholder_text=_('Search query override for the next turn (optional)'), visible=False)
        self.append(self.query)
        self.toggle.connect('toggled', self._changed)
        self.storage.knowledge.listeners.append(self._notice)
        self.load({})

    def load(self, options):
        self.restoring = True
        self.options = dict(copy.deepcopy(DEFAULT_RAG), **copy.deepcopy(options))
        self.toggle.set_active(self.options['enabled'])
        self.restoring = False
        self._notice()

    def _notice(self):
        if not self.get_sensitive():
            return
        count = len(self.options['selection'])
        ids = set(self.options['collection_ids'])
        groups = [c for c in self.storage.db.knowledge_collections() if c['id'] in ids] if ids else []
        text = _('Collections: {0}').format(len(ids))
        if count:
            text += '\n' + _('Older individual sources: {0}. Choose collections to replace them.').format(count)
        if groups:
            text += '\n' + ', '.join(_('{0}: {1}/{2} ready').format(c['name'], c['ready'], c['documents']) for c in groups)
        if ids - {c['id'] for c in groups}:
            text += '\n' + _('A selected collection was deleted. Choose sources again.')
        self.notice.set_text(text if self.toggle.get_active() else _('Knowledge is off.'))
        self.query.set_visible(self.toggle.get_active())

    def _changed(self, *args):
        self.options['enabled'] = self.toggle.get_active()
        self._notice()
        if not self.restoring:
            self.on_change()
            if self.options['enabled'] and not (self.options['selection'] or self.options['collection_ids']) and self.get_mapped():
                self.open_picker()

    def open_picker(self, *args):
        if self.dialog or not self.get_root():
            return
        def apply(options):
            self.load(options)
            self.on_change()
        self.dialog = SourcePicker(self.storage, self.options, apply)
        self.dialog.connect('closed', lambda *args: setattr(self, 'dialog', None))
        self.dialog.present(self.get_root())

    def close_dialog(self, dispose=False):
        if dispose and self._notice in self.storage.knowledge.listeners:
            self.storage.knowledge.listeners.remove(self._notice)
        if self.dialog:
            self.dialog.close()


class KnowledgeView(Gtk.Box):
    def __init__(self, storage):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.storage = storage
        self.document_id = None
        self.collection_id = None
        self.library_scope = 'all'
        self._library_choices = ['all', 'ungrouped']
        self._refreshing_collections = False
        self.dialogs = []
        self.closed = False
        self._refresh_pending = False
        self._refresh_again = False
        self._render_key = None
        self.availability = {}
        self._availability_loading = set()
        self._file_cancels = []
        self._availability_cancels = []
        self.append(actions(button(_('New Collection'), self.new_collection),
                            button(_('Add Text'), self.add_text), button(_('Import Files…'), self.import_files),
                            button(_('Add URLs…'), self.add_urls),
                            button(_('Test Search'), self.test_search), button(_('Refresh'), self.refresh_models)))
        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.collection_dropdown = dropdown([_('All Documents'), _('Ungrouped Documents')])
        self.collection_dropdown.connect('notify::selected', self._library_changed)
        sidebar.append(label(_('Library')))
        sidebar.append(self.collection_dropdown)
        self.search = Gtk.SearchEntry(placeholder_text=_('Search documents'))
        self.search.connect('search-changed', self.refresh)
        sidebar.append(self.search)
        self.documents = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        sidebar.append(Gtk.ScrolledWindow(child=self.documents, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER))
        sidebar_page = Adw.NavigationPage.new(sidebar, _('Knowledge Library'))
        detail_toolbar = Adw.ToolbarView()
        detail_toolbar.add_top_bar(Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False))
        self.detail = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                              margin_start=12, margin_end=12, margin_top=12, margin_bottom=12)
        detail_toolbar.set_content(Gtk.ScrolledWindow(child=self.detail, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER))
        self.detail_page = Adw.NavigationPage.new(detail_toolbar, _('Document'))
        self.split = Adw.NavigationSplitView(sidebar=sidebar_page, content=self.detail_page,
                                             min_sidebar_width=220, max_sidebar_width=300)
        bin = Adw.BreakpointBin(child=self.split, vexpand=True, width_request=320, height_request=200)
        breakpoint = Adw.Breakpoint.new(Adw.BreakpointCondition.parse('max-width: 700sp'))
        breakpoint.add_setter(self.split, 'collapsed', True)
        bin.add_breakpoint(breakpoint)
        self.append(bin)
        self.jobs_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin_start=12, margin_end=12)
        self.append(Gtk.ScrolledWindow(child=self.jobs_box, max_content_height=160, propagate_natural_height=True,
                                       hscrollbar_policy=Gtk.PolicyType.NEVER))
        self.storage.knowledge.listeners.append(self.refresh)
        self.connect('destroy', self._destroyed)
        self.refresh()

    def _destroyed(self, *args):
        self.closed = True
        if self.refresh in self.storage.knowledge.listeners:
            self.storage.knowledge.listeners.remove(self.refresh)
        self.close_dialogs()

    def close_dialogs(self):
        for cancel in self._file_cancels + self._availability_cancels:
            cancel.cancel()
        self._file_cancels.clear()
        self._availability_cancels.clear()
        self._availability_loading.clear()
        for dialog in list(self.dialogs):
            dialog.close()

    def present_dialog(self, dialog):
        self.dialogs.append(dialog)
        dialog.connect('closed', lambda *args: self.dialogs.remove(dialog) if dialog in self.dialogs else None)
        dialog.present(self.get_root())

    def error(self, message):
        dialog = Adw.AlertDialog(heading=_('Knowledge Library'), body=str(message))
        dialog.add_response('close', _('Close'))
        self.present_dialog(dialog)

    def refresh(self, *args):
        if self.closed:
            return
        if self._refresh_pending:
            self._refresh_again = True
            return
        self._refresh_pending = True
        query, document_id, scope = self.search.get_text().casefold(), self.document_id, self.library_scope
        def fetch():
            try:
                docs = self.storage.db.knowledge_documents()
                collections = self.storage.db.knowledge_collections()
                collection = next((c for c in collections if c['id'] == scope), None)
                members = self.storage.db.collection_documents(scope) if collection else []
                if scope == 'ungrouped':
                    ungrouped = self.storage.db.ungrouped_document_ids()
                    docs = [d for d in docs if d['id'] in ungrouped]
                elif scope != 'all':
                    ids = {m['id'] for m in members}
                    docs = [d for d in docs if d['id'] in ids]
                indexes = self.storage.db.knowledge_indexes(document_id=document_id) if document_id else []
                document = self.storage.db.knowledge_document(document_id) if document_id else None
                memberships = self.storage.db.source_collections(document_id=document_id) if document_id else []
                error = None
            except Exception as exc:
                docs, indexes, document, error = [], [], None, exc
                collections, collection, members, memberships = [], None, [], []
            def deliver():
                self._refresh_pending = False
                if self.closed:
                    return False
                if query != self.search.get_text().casefold() or document_id != self.document_id or scope != self.library_scope:
                    self.refresh()
                    return False
                self._refreshing_collections = True
                self._library_choices = ['all', 'ungrouped'] + [c['id'] for c in collections]
                self.collection_dropdown.set_model(Gtk.StringList.new(
                    [_('All Documents'), _('Ungrouped Documents')] + [c['name'] for c in collections]))
                self.collection_dropdown.set_selected(self._library_choices.index(scope) if scope in self._library_choices else 0)
                self._refreshing_collections = False
                if scope not in self._library_choices:
                    self.open_collection(None)
                    return False
                clear(self.documents)
                for doc in docs:
                    if query in (doc['title'] + '\n' + doc['filename']).casefold():
                        row = Adw.ActionRow(title=doc['title'], subtitle=_('{0} characters').format(doc['characters']), activatable=True, use_markup=False)
                        row.connect('activated', lambda row, id=doc['id']: self.open_document(id))
                        self.documents.append(row)
                if not docs:
                    self.documents.append(label(_('Add text, import files, or add existing documents to this collection.') if collection else
                                                _('Add text or import files to build your library.')))
                key = (document_id, document['title'] if document else '',
                       document['content_hash'] if document else '',
                       json.dumps(document.get('web_source'), sort_keys=True) if document else '',
                       scope, tuple((c['id'], c['name']) for c in memberships),
                       tuple(collection.items()) if collection else (),
                       tuple(tuple(m.items()) for m in members),
                       tuple((i['id'], i['status'], i['chunks'], i['error'], self.availability.get(i['id'])) for i in indexes))
                if key != self._render_key:
                    self._render_key = key
                    if document:
                        self._show_document(document, indexes, memberships)
                    else:
                        self._show_collection(collection, members)
                self._check_availability(indexes)
                self._jobs()
                if error:
                    self.error(error)
                if self._refresh_again:
                    self._refresh_again = False
                    self.refresh()
                return False
            GLib.idle_add(deliver)
        worker.submit(fetch)

    def refresh_models(self, *args):
        self.availability.clear()
        self.refresh()

    def _library_changed(self, *args):
        if self._refreshing_collections:
            return
        n = self.collection_dropdown.get_selected()
        if n < len(self._library_choices):
            self.open_collection(self._library_choices[n])

    def open_collection(self, id):
        self.library_scope = id or 'all'
        self.collection_id = id if id and id not in ('all', 'ungrouped') else None
        self.document_id = None
        self.search.set_text('')
        self.split.set_show_content(bool(self.collection_id))
        self.refresh()

    def new_collection(self, *args, collection=None):
        self.present_dialog(CollectionDialog(self.storage, self.open_collection, collection))

    def _show_collection(self, collection, members):
        clear(self.detail)
        if not collection:
            self.detail_page.set_title(_('Knowledge Library'))
            self.detail.append(label(_('Select a document to inspect it, or create a collection to search several documents together.')))
            return
        self.detail_page.set_title(collection['name'])
        self.detail.append(label(config_label(collection)))
        self.detail.append(label(_('{0}/{1} documents ready · Chunk size: {2} · Overlap: {3}').format(
            collection['ready'], collection['documents'], collection['chunk_size'], collection['overlap'])))
        self.detail.append(label(collection['host']))
        menu = Gio.Menu()
        menu_actions = Gio.SimpleActionGroup()
        for name, title, callback in (
                ('rename', _('Rename'), lambda: self._rename_collection(collection)),
                ('copy', _('Copy with New Settings'), lambda: self.new_collection(collection=collection)),
                ('host', _('Change Embedding Model'), lambda: self._collection_host(collection)),
                ('delete', _('Delete Collection'), lambda: self._delete_collection(collection))):
            action = Gio.SimpleAction.new(name, None)
            action.connect('activate', lambda a, p, callback=callback: callback())
            menu_actions.add_action(action)
            menu.append(title, 'collection.' + name)
        options = Gtk.MenuButton(label=_('Collection Options'), menu_model=menu)
        options.insert_action_group('collection', menu_actions)
        self.detail.append(actions(
            button(_('Add Existing Documents'), lambda *args: self.present_dialog(DocumentPicker(self.storage, collection['id'], self.refresh))),
            button(_('Build Missing / Retry Failed'), lambda *args: self._build_collection(collection)),
            options))
        self.detail.append(label(_('Add Text and Import Files add to this collection. Missing embeddings build automatically; changes affect future chat questions.')))
        rows = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        rows.add_css_class('boxed-list')
        self.detail.append(rows)
        for member in members:
            status = _('Ready') if member['status'] == 'complete' and member['chunks'] else member['error'] or member['status'] or _('Needs embeddings')
            row = Adw.ActionRow(title=member['title'], subtitle=status, activatable=True, use_markup=False)
            row.connect('activated', lambda row, id=member['id']: self.open_document(id))
            remove = Gtk.Button(icon_name='list-remove-symbolic', tooltip_text=_('Remove from Collection'), valign=Gtk.Align.CENTER)
            remove.add_css_class('flat')
            remove.connect('clicked', lambda b, id=member['id']: self.storage._submit(
                self.storage.db.remove_collection_document, collection['id'], id, on_done=self.storage.knowledge.changed))
            row.add_suffix(remove)
            rows.append(row)

    def _build_collection(self, collection):
        try:
            self.storage.knowledge.build_collection(collection['id'])
        except ValueError as exc:
            self.error(exc)

    def _rename_collection(self, collection):
        dialog = Adw.AlertDialog(heading=_('Rename Collection'))
        entry = Gtk.Entry(text=collection['name'])
        dialog.set_extra_child(entry)
        dialog.add_response('cancel', _('Cancel'))
        dialog.add_response('save', _('Save'))
        dialog.set_response_enabled('save', bool(entry.get_text().strip()))
        entry.connect('changed', lambda *args: dialog.set_response_enabled('save', bool(entry.get_text().strip())))
        dialog.connect('response', lambda d, r: self.storage._submit(self.storage.db.rename_knowledge_collection,
            collection['id'], entry.get_text().strip(), on_done=self.storage.knowledge.changed) if r == 'save' else None)
        self.present_dialog(dialog)

    def _delete_collection(self, collection):
        dialog = Adw.AlertDialog(heading=_('Delete Collection?'), body=_('Delete “{0}”? Its documents and embeddings stay in the library. Chats selecting this collection will need new sources; saved answers keep their original passages.').format(collection['name']))
        dialog.add_response('cancel', _('Cancel'))
        dialog.add_response('delete', _('Delete Collection'))
        dialog.set_response_appearance('delete', Adw.ResponseAppearance.DESTRUCTIVE)
        def response(d, choice):
            if choice == 'delete':
                self.storage._submit(self.storage.db.delete_knowledge_collection, collection['id'],
                                     on_done=self.storage.knowledge.changed)
        dialog.connect('response', response)
        self.present_dialog(dialog)

    def _collection_host(self, collection):
        dialog = WorkDialog(_('Change Embedding Model'))
        hosts = HostModels(self.storage, collection['host'], collection['model'])
        dialog.box.append(hosts)
        dialog.box.append(label(_('Choose a host and embedding model matching the collection’s original model digest. Embedding settings stay the same.')))
        dialog.connect('closed', lambda *args: hosts.stop())
        def save(*args):
            model = hosts.model()
            if not model or model.get('digest') != collection['digest']:
                dialog.show_error(_('Choose a model with the collection’s original digest.'))
                return
            self.storage._submit(self.storage.db.update_collection_endpoint, collection['id'], hosts.host(),
                                 model['name'], model['digest'], on_done=self.storage.knowledge.changed)
            dialog.close()
        dialog.header.pack_end(button(_('Save'), save))
        self.present_dialog(dialog)

    def _check_availability(self, indexes):
        if self.storage.knowledge.closed:
            return
        hosts = {i['host'] for i in indexes if i['id'] not in self.availability}
        for host in hosts - self._availability_loading:
            self._availability_loading.add(host)
            cancel = Gio.Cancellable()
            self._availability_cancels.append(cancel)
            selected = [i for i in indexes if i['host'] == host]
            def fetch(host=host, selected=selected, cancel=cancel):
                states = {}
                try:
                    models = ollama.fetch_model_details(host, cancellable=cancel)
                    for index in selected:
                        match = next((m for m in models if canonical_model(m['name']) == canonical_model(index['model'])), None)
                        states[index['id']] = (_('Original model available') if match and match.get('digest') == index['digest']
                                               else _('Original model unavailable or changed — choose a compatible host for searches.'))
                except Exception as exc:
                    states = {i['id']: _('Host unavailable: {0}').format(str(exc)) for i in selected}
                def deliver():
                    self._availability_loading.discard(host)
                    if cancel in self._availability_cancels:
                        self._availability_cancels.remove(cancel)
                    if not self.closed and not cancel.is_cancelled():
                        self.availability.update(states)
                        self.refresh()
                    return False
                GLib.idle_add(deliver)
            worker.submit(fetch)

    def open_document(self, id):
        self.document_id = id
        self.split.set_show_content(True)
        self.availability.clear()
        self.refresh()

    def _show_document(self, document, indexes, memberships=()):
        clear(self.detail)
        if not document:
            self.detail.append(label(_('Select a document to inspect its text and embeddings.')))
            return
        self.detail_page.set_title(document['title'])
        self.detail.append(label(document['title']))
        source = document.get('web_source')
        if source:
            self.detail.append(Gtk.LinkButton(uri=source['final_url'], label=_('Open Source URL'), halign=Gtk.Align.START))
            self.detail.append(label(source['final_url']))
            self.detail.append(label(_('Fetched: {0}').format(time.strftime('%Y-%m-%d %H:%M', time.localtime(source['fetched_at'])))))
            self.detail.append(button(_('Fetch Again…'), lambda *args: self.add_urls(url=source['source_url'], document_id=document['id'])))
        if memberships:
            self.detail.append(label(_('Collections: {0}').format(', '.join(c['name'] for c in memberships))))
        if self.collection_id:
            self.detail.append(button(_('Back to Collection'), lambda *args: self.open_collection(self.collection_id)))
        self.detail.append(actions(button(_('Preview Text'), lambda *args: self._preview(document)),
                                   button(_('Create Embeddings'), lambda *args: self.present_dialog(IndexDialog(self.storage, document, self.refresh))),
                                   button(_('Rename'), lambda *args: self._rename(document)),
                                   button(_('Delete Document'), lambda *args: self._delete(document=document))))
        for index in indexes:
            group = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            group.append(label(config_label(index)))
            group.append(label(self.availability.get(index['id'], _('Checking model availability…'))))
            group.append(label('{0}\n{1} · {2} · {3}'.format(index['host'],
                               time.strftime('%Y-%m-%d %H:%M', time.localtime(index['created_at'])), index['status'],
                               _('Chunks: {0}').format(index['chunks']))))
            if index['error']:
                group.append(label(index['error']))
            row_actions = [button(_('Inspect Chunks'), lambda *args, i=index: self.present_dialog(ChunkPicker(self.storage, i))),
                           button(_('Check Model'), lambda *args, i=index: self._check_model(i)),
                           button(_('Delete Index'), lambda *args, i=index: self._delete(index=i))]
            if index['status'] in ('failed', 'interrupted'):
                row_actions.append(button(_('Retry'), lambda *args, i=index: self._retry(i)))
            group.append(actions(*row_actions))
            self.detail.append(group)

    def _jobs(self):
        clear(self.jobs_box)
        jobs = list(self.storage.knowledge.jobs.values())
        for job in [j for j in jobs if not j['done']] + [j for j in jobs if j['done']][-3:]:
            row = Gtk.Box(spacing=6)
            row.append(label(job['title'] + ': ' + job['progress'], hexpand=True))
            if not job['done']:
                row.append(button(_('Cancel'), lambda *args, j=job: self._cancel_job(j)))
            elif job['error']:
                row.append(button(_('Details'), lambda *args, j=job: self.error(j['error'])))
            self.jobs_box.append(row)

    def _cancel_job(self, job):
        collections = self.storage.db.source_collections(index_id=job['index_id']) if job.get('index_id') else []
        if len(collections) < 2:
            job['cancel'].cancel()
            return
        dialog = Adw.AlertDialog(heading=_('Stop Shared Embedding Build?'),
                                 body=_('These collections use this build: {0}. They will need a retry before chat search can use them.').format(
                                     ', '.join(c['name'] for c in collections)))
        dialog.add_response('keep', _('Keep Building'))
        dialog.add_response('stop', _('Stop Build'))
        dialog.set_default_response('keep')
        dialog.connect('response', lambda d, choice: job['cancel'].cancel() if choice == 'stop' else None)
        self.present_dialog(dialog)

    def _preview(self, document):
        dialog = TextEditor(preview_text(document), lambda text: None, title=document['title'],
                            hint=_('Saved source text. Import revised content as a new document.'), apply_label=_('Close'))
        dialog.editor.set_editable(False)
        plain_editor(dialog)
        self.present_dialog(dialog)

    def add_text(self, *args):
        collection_id = self.collection_id
        dialog = TextEditor('', lambda text: None, title=_('Add Text'), hint=_('Paste text to save in the Knowledge Library.'),
                            validate=lambda text: make_document('text', text), apply_label=_('Save'))
        title = Gtk.Entry(placeholder_text=_('Document title'))
        dialog.content_box.prepend(title)
        dialog.on_apply = lambda text: self._save_document(make_document(title.get_text(), text), collection_id)
        plain_editor(dialog)
        self.present_dialog(dialog)

    def add_urls(self, *args, url='', document_id=None):
        from .url_import import URLImportDialog
        collection_id = self.collection_id
        if not collection_id and document_id:
            groups = self.storage.db.source_collections(document_id=document_id)
            if groups:
                collection_id = groups[0]['id']
        self.present_dialog(URLImportDialog(self.storage, collection_id, url))

    def _save_document(self, document, collection_id=None):
        existing = next((d for d in self.storage.db.knowledge_documents() if d['content_hash'] == document['content_hash']), None)
        if existing:
            dialog = Adw.AlertDialog(heading=_('This Text Is Already Saved'), body=existing['title'])
            dialog.add_response('cancel', _('Cancel'))
            dialog.add_response('open', _('Open Existing'))
            if collection_id:
                dialog.add_response('add', _('Add Existing to Collection'))
            def response(d, choice):
                if choice == 'open':
                    self.open_document(existing['id'])
                elif choice == 'add':
                    self._document_saved(existing['id'], collection_id)
            dialog.connect('response', response)
            self.present_dialog(dialog)
            return
        self.storage._submit(self.storage.db.add_knowledge_document, document,
                             on_done=lambda: self._document_saved(document['id'], collection_id))

    def _document_saved(self, document_id, collection_id):
        self.storage.knowledge.changed()
        if self.closed or self.storage.knowledge.closed:
            return
        if collection_id:
            try:
                self.storage.knowledge.build_collection(collection_id, [document_id])
                self.open_collection(collection_id)
            except ValueError as exc:
                self.error(exc)
        else:
            self.open_document(document_id)

    def import_files(self, *args):
        collection_id = self.collection_id
        dialog = Gtk.FileDialog(title=_('Import Text or PDF Files'))
        cancel = Gio.Cancellable()
        self._file_cancels.append(cancel)
        def selected(dialog, result):
            if cancel in self._file_cancels:
                self._file_cancels.remove(cancel)
            if self.closed or cancel.is_cancelled() or self.storage.knowledge.closed:
                return
            try:
                files = dialog.open_multiple_finish(result)
            except GLib.Error as exc:
                if not exc.matches(Gtk.dialog_error_quark(), Gtk.DialogError.DISMISSED):
                    self.error(exc)
                return
            for n in range(files.get_n_items()):
                file = files.get_item(n)
                def extract(cancel, progress, file=file):
                    progress(_('Extracting text…'))
                    info = file.query_info('standard::size', Gio.FileQueryInfoFlags.NONE, cancel)
                    if info.get_size() > 50 * 1024 * 1024:
                        raise ValueError(_('Files must be no larger than 50 MiB.'))
                    _, raw, _etag = file.load_contents(cancel)
                    return extract_document(file.get_basename(), raw, cancel)
                def preview(result, error):
                    if self.closed or self.storage.knowledge.closed:
                        return
                    if error:
                        self.error(error)
                        return
                    document, warnings = result
                    editor = TextEditor(preview_text(document), lambda text: self._save_document(document, collection_id),
                                        title=_('Import Preview'), hint=document['filename'] + '\n' + '\n'.join(warnings),
                                        apply_label=_('Save'))
                    editor.editor.set_editable(False)
                    plain_editor(editor)
                    self.present_dialog(editor)
                self.storage.knowledge.submit(file.get_basename(), extract, preview)
        dialog.open_multiple(self.get_root(), cancel, selected)

    def test_search(self, *args):
        options = copy.deepcopy(DEFAULT_RAG)
        collection = self.storage.db.knowledge_collection(self.collection_id) if self.collection_id else None
        if collection:
            options.update(config_id=collection['config_id'], host=collection['host'], model=collection['model'],
                           collection_ids=[collection['id']])
        self.present_dialog(SourcePicker(self.storage, options))

    def _rename(self, document):
        dialog = Adw.AlertDialog(heading=_('Rename Document'))
        entry = Gtk.Entry(text=document['title'])
        dialog.set_extra_child(entry)
        dialog.add_response('cancel', _('Cancel'))
        dialog.add_response('save', _('Save'))
        def response(d, r):
            if r == 'save' and entry.get_text().strip():
                self.storage._submit(self.storage.db.rename_knowledge_document, document['id'], entry.get_text().strip(), on_done=self.storage.knowledge.changed)
        dialog.connect('response', response)
        self.present_dialog(dialog)

    def _delete(self, document=None, index=None):
        if any(not j['done'] and (j.get('document_id') == (document or {}).get('id') if document else j.get('index_id') == index['id'])
               for j in self.storage.knowledge.jobs.values()):
            self.error(_('Cancel this document’s indexing job before deleting it.'))
            return
        dialog = Adw.AlertDialog(heading=_('Delete Document?') if document else _('Delete Embedding Index?'),
                                 body=_('Chats selecting this data will need new sources. Passages already saved with answers remain in chat history.') +
                                 ('\n' + _('The saved source text and all its indexes will be deleted.') if document else '\n' + _('The saved source text will be kept.')))
        collections = self.storage.db.source_collections(document_id=document['id']) if document else self.storage.db.source_collections(index_id=index['id'])
        if collections:
            dialog.set_body(dialog.get_body() + '\n\n' + _('Affected collections: {0}.').format(', '.join(c['name'] for c in collections)) +
                            ' ' + (_('This document will be removed from their membership.') if document else
                                   _('Their documents will need embeddings rebuilt.')))
        dialog.add_response('cancel', _('Cancel'))
        dialog.add_response('delete', _('Delete'))
        dialog.set_response_appearance('delete', Adw.ResponseAppearance.DESTRUCTIVE)
        def response(d, r):
            if r == 'delete':
                self.storage._submit(self.storage.db.delete_knowledge_document if document else self.storage.db.delete_knowledge_index,
                                     (document or index)['id'], on_done=self.storage.knowledge.changed)
        dialog.connect('response', response)
        self.present_dialog(dialog)

    def _retry(self, index):
        if any(not j['done'] and j.get('index_id') == index['id'] for j in self.storage.knowledge.jobs.values()):
            return
        config = self.storage.db.embedding_config(index['config_id'])
        self.storage.knowledge.create_index(index['document_id'], index['host'], index['model'], config,
                                             index['chunk_size'], index['overlap'], index_id=index['id'])

    def _check_model(self, index):
        dialog = WorkDialog(_('Embedding Model Availability'), height=280)
        status = label(_('Checking…'))
        dialog.box.append(status)
        dialog.run(lambda cancel: model_identity(index['host'], index['model'], cancel, index['digest']),
                   lambda result, error: status.set_text(str(error) if error else _('The original model is available on this host.')))
        self.present_dialog(dialog)
