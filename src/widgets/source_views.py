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


from .knowledge_common import *

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
        self.collections = storage.library.knowledge_collections()
        self.configs = [c for c in storage.library.embedding_configs() if any(group['config_id'] == c['id'] for group in self.collections)]
        self._refreshing = False
        self.collection_search = Gtk.SearchEntry(placeholder_text=_('Search collections'))
        self.collection_search.connect('search-changed', lambda *args: self._render_sources())
        self.box.append(self.collection_search)
        self.collection_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.box.append(self.collection_box)
        self.search_settings, self.settings_expander = section(self.box, _('Search Settings'))
        self.config_dropdown = dropdown([config_label(c) for c in self.configs])
        self.config_dropdown.set_sensitive(False)
        field(self.search_settings, _('Embedding configuration (from selected collections)'), self.config_dropdown)
        for i, config in enumerate(self.configs):
            if config['id'] == self.options['config_id']:
                self.config_dropdown.set_selected(i)
                break
        if self.options['config_id'] and not any(c['id'] == self.options['config_id'] for c in self.configs):
            self.config_dropdown.set_selected(Gtk.INVALID_LIST_POSITION)
        self.host_models = HostModels(storage, self.options['host'], self.options['model'])
        self.search_settings.append(self.host_models)
        self.connect('closed', lambda *args: self.host_models.stop())
        self.search_settings.append(label(_('The query must use the same embedding configuration as the selected collections. A compatible server can be chosen here.')))
        if self.options['selection']:
            self.box.append(label(_('This chat has older individual sources. Applying a collection selection replaces them. Closing this dialog keeps them unchanged.')))
        self.count = Gtk.SpinButton.new_with_range(1, 20, 1)
        self.count.set_value(self.options['count'])
        self.budget = Gtk.SpinButton.new_with_range(256, 64000, 256)
        self.budget.set_value(self.options['budget'])
        for title, widget in [(_('Maximum passages'), self.count), (_('Source budget (characters)'), self.budget)]:
            self.search_settings.append(label(title))
            self.search_settings.append(widget)
        self.search_settings.append(label(_('Similarity measure')))
        self.metric_dropdown = dropdown([_('Cosine similarity'), _('Euclidean distance (L2)'), _('Manhattan distance (L1)')])
        metric = self.options['metric']
        self.metric_dropdown.set_selected(SEARCH_METRICS.index(metric) if metric in SEARCH_METRICS else Gtk.INVALID_LIST_POSITION)
        self.search_settings.append(self.metric_dropdown)
        self.metric_notice = label()
        self.search_settings.append(self.metric_notice)
        self.threshold_label = label()
        self.search_settings.append(self.threshold_label)
        self.threshold = Gtk.Entry()
        self.search_settings.append(self.threshold)
        self._metric_changed(clear_threshold=False)
        threshold = self.options['minimum'] if metric == 'cosine' else self.options['maximum']
        self.threshold.set_text('' if threshold is None else str(threshold))
        self.metric_dropdown.connect('notify::selected', self._metric_changed)
        self.test_box, self.test_expander = section(self.box, _('Test Search'), expanded=on_apply is None)
        self.query = Gtk.Entry(placeholder_text=_('Enter a search query'))
        self.test_box.append(self.query)
        self.search_button = button(_('Test Search'), self._search)
        self.test_box.append(self.search_button)
        self.results = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.test_box.append(self.results)
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
        self.collections = self.storage.library.knowledge_collections()
        self.configs = [c for c in self.storage.library.embedding_configs() if any(group['config_id'] == c['id'] for group in self.collections)]
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
        self.collection_box.append(label(_('Choose collections to search. New documents added to them are included automatically. All documents must be ready.')))
        self.collection_checks = {}
        missing = self.collection_ids - {c['id'] for c in self.collections}
        if missing:
            self.collection_box.append(label(_('Some selected collections were deleted. Choose sources again.')))
            self.collection_box.append(button(_('Clear missing collections'), lambda *args: self._clear_collections(missing)))
        for collection in self.collections:
            if self.collection_search.get_text().casefold() not in collection['name'].casefold():
                continue
            compatible = not self.collection_ids or (config is not None and collection['config_id'] == config['id'])
            text = _('{0} · {1}/{2} documents ready').format(collection['name'], collection['ready'], collection['documents'])
            if not compatible:
                text += '\n' + _('Different embedding configuration: {0}').format(config_label(collection))
            check = Gtk.CheckButton(child=label(text, selectable=False), sensitive=compatible,
                                     active=collection['id'] in self.collection_ids)
            check.connect('toggled', self._select_collection, collection)
            self.collection_checks[collection['id']] = (check, collection)
            self.collection_box.append(check)
        if self.collections and not self.collection_checks:
            self.collection_box.append(label(_('No matching collections')))
        if not self.collections:
            self.collection_box.append(label(_('Create a collection and add text, files, or URLs in the Knowledge Library.')))
            self.collection_box.append(button(_('Open Knowledge Library'), self.open_library))

    def _clear_collections(self, ids):
        self.collection_ids.difference_update(ids)
        self._render_sources()

    def open_library(self, *args):
        root = self.get_root()
        if hasattr(root, 'section_stack'):
            self.close()
            root.section_stack.set_visible_child_name('knowledge')

    def _select_collection(self, check, collection):
        if check.get_active():
            first = not self.collection_ids
            self.collection_ids.add(collection['id'])
            if first:
                self._refreshing = True
                self.config_dropdown.set_selected(next(n for n, c in enumerate(self.configs) if c['id'] == collection['config_id']))
                self._refreshing = False
                self.host_models.desired_model = collection['model']
                for n, host in enumerate(self.host_models.hosts):
                    if host['hostname'].rstrip('/') == collection['host'].rstrip('/'):
                        self.host_models.host_dropdown.set_selected(n)
                        break
                self.host_models.refresh()
        else:
            self.collection_ids.discard(collection['id'])
        config = self.config()
        # Keep the focused row alive while changing compatibility.
        for widget, value in self.collection_checks.values():
            compatible = not self.collection_ids or (config is not None and value['config_id'] == config['id'])
            widget.set_sensitive(compatible)
            text = _('{0} · {1}/{2} documents ready').format(value['name'], value['ready'], value['documents'])
            if not compatible:
                text += '\n' + _('Uses different embedding settings. Clear the current selection to choose this collection.')
            widget.get_child().set_text(text)

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
        if model is None or config is None or model.get('digest') != config['digest']:
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
            self.run(lambda cancel: self.storage.library.knowledge_chunk_ids(self.index['id']), loaded)

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
        self._load_generation = getattr(self, '_load_generation', 0) + 1
        generation, offset = self._load_generation, self.offset
        self.more.set_sensitive(False)
        def loaded(chunks, error):
            if generation != self._load_generation:
                return
            self.more.set_sensitive(True)
            if error:
                self.show_error(error)
                return
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

        self.run(lambda cancel: self.storage.library.knowledge_chunk_page(self.index['id'], offset), loaded)

    def _vector(self, chunk):
        from ..vectors import vector_values
        try:
            raw = self.storage.library.knowledge_vector(chunk['id'])
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
        self.query.set_placeholder_text(_('Use a different search query for the next message'))
        self.query_options = Gtk.Expander(label=_('Search Query Override'), child=self.query, visible=False)
        self.append(self.query_options)
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
        groups = [c for c in self.storage.library.knowledge_collections() if c['id'] in ids] if ids else []
        text = _('Choose collections to search.') if not ids else ''
        if count:
            text += '\n' + _('Older individual sources: {0}. Choose collections to replace them.').format(count)
        if groups:
            text += ', '.join(_('{0}: {1}/{2} ready').format(c['name'], c['ready'], c['documents']) for c in groups)
        if ids - {c['id'] for c in groups}:
            text += '\n' + _('A selected collection was deleted. Choose sources again.')
        self.notice.set_text(text)
        self.notice.set_visible(self.toggle.get_active())
        self.query.set_visible(True)
        self.query_options.set_visible(self.toggle.get_active())

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


