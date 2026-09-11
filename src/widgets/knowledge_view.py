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
from .collection_views import CollectionDialog, CollectionDestination, DocumentPicker
from .source_views import SourcesView, SourcePicker, ChunkPicker, KnowledgeControl

class KnowledgeView(Gtk.Box):
    def __init__(self, storage):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.storage = storage
        self.document_id = None
        self.collection_id = None
        self.library_scope = 'collections'
        self._library_choices = ['collections', 'all', 'ungrouped']
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
        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        sidebar.add_css_class('knowledge-sidebar')
        create = button(_('New Collection…'), self.new_collection)
        create.add_css_class('suggested-action')
        sidebar.append(create)
        self.collection_dropdown = dropdown([_('Collections'), _('All Documents'), _('Ungrouped Documents')])
        self.collection_dropdown.connect('notify::selected', self._library_changed)
        sidebar.append(label(_('Library')))
        sidebar.append(self.collection_dropdown)
        self.search = Gtk.SearchEntry(placeholder_text=_('Search documents'))
        self.search.connect('search-changed', self.refresh)
        sidebar.append(self.search)
        self._document_limit = 100
        self._document_cache = []
        self._append_documents = False
        self._document_rows = {}
        self.more_documents = button(_('Load More'), self.load_more_documents)
        sidebar.append(self.more_documents)
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
        self.jobs_expander = Gtk.Expander(label=_('Background Tasks'), margin_start=12, margin_end=12, margin_bottom=12)
        self.jobs_expander.set_child(Gtk.ScrolledWindow(child=self.jobs_box, max_content_height=160, propagate_natural_height=True,
                                       hscrollbar_policy=Gtk.PolicyType.NEVER))
        self.append(self.jobs_expander)
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

    def load_more_documents(self, *args):
        self._append_documents = True
        self.refresh()

    def refresh(self, *args):
        if args and args[0] is self.search:
            self._document_limit = 100
        if self.closed:
            return
        if self._refresh_pending:
            self._refresh_again = True
            return
        self._refresh_pending = True
        previous_docs = list(self._document_cache) if self._append_documents else []
        self._append_documents = False
        query, document_id, scope = self.search.get_text().casefold(), self.document_id, self.library_scope
        def fetch():
            try:
                docs = self.storage.library.document_page(query, scope, 101, len(previous_docs)) if scope != 'collections' else []
                collections = self.storage.library.knowledge_collections()
                collection = next((c for c in collections if c['id'] == scope), None)
                members = self.storage.library.collection_documents(scope) if collection else []
                indexes = self.storage.library.knowledge_indexes(document_id=document_id) if document_id else []
                document = self.storage.library.knowledge_document(document_id) if document_id else None
                memberships = self.storage.library.source_collections(document_id=document_id) if document_id else []
                error = None
            except Exception as exc:
                docs, indexes, document, error = [], [], None, exc
                collections, collection, members, memberships = [], None, [], []
            def deliver():
                nonlocal docs
                self._refresh_pending = False
                if self.closed:
                    return False
                if query != self.search.get_text().casefold() or document_id != self.document_id or scope != self.library_scope:
                    self.refresh()
                    return False
                self._refreshing_collections = True
                self._library_choices = ['collections', 'all', 'ungrouped'] + [c['id'] for c in collections]
                self.collection_dropdown.set_model(Gtk.StringList.new(
                    [_('Collections'), _('All Documents'), _('Ungrouped Documents')] + [c['name'] for c in collections]))
                self.collection_dropdown.set_selected(self._library_choices.index(scope) if scope in self._library_choices else 0)
                self._refreshing_collections = False
                if scope not in self._library_choices:
                    self.open_collection(None)
                    return False
                desired = set()
                for key, row in list(self._document_rows.items()):
                    if key == 'empty':
                        self.documents.remove(row)
                        del self._document_rows[key]
                self.more_documents.set_visible(scope != 'collections' and len(docs) > 100)
                self._document_cache = previous_docs + docs[:100]
                docs = self._document_cache
                self.search.set_placeholder_text(_('Search collections') if scope == 'collections' else _('Search documents'))
                shown = 0
                if scope == 'collections':
                    for group in collections:
                        if query not in group['name'].casefold():
                            continue
                        desired.add(group['id'])
                        if group['id'] in self._document_rows:
                            shown += 1
                            continue
                        row = Adw.ActionRow(title=group['name'],
                            subtitle=_('{0}/{1} documents ready').format(group['ready'], group['documents']),
                            activatable=True, use_markup=False)
                        row.add_suffix(Gtk.Image(icon_name='go-next-symbolic'))
                        row.connect('activated', lambda row, id=group['id']: self.open_collection(id))
                        self.documents.append(row)
                        self._document_rows[group['id']] = row
                        shown += 1
                else:
                    for doc in docs:
                        if query in (doc['title'] + '\n' + doc['filename']).casefold():
                            desired.add(doc['id'])
                            if doc['id'] in self._document_rows:
                                self._document_rows[doc['id']].set_title(doc['title'])
                                shown += 1
                                continue
                            count = doc['characters']
                            row = Adw.ActionRow(title=doc['title'], subtitle=ngettext('{0} character', '{0} characters', count).format(count), activatable=True, use_markup=False)
                            row.add_suffix(Gtk.Image(icon_name='go-next-symbolic'))
                            row.connect('activated', lambda row, id=doc['id']: self.open_document(id))
                            self.documents.append(row)
                            self._document_rows[doc['id']] = row
                            shown += 1
                for key in list(self._document_rows):
                    if key not in desired:
                        self.documents.remove(self._document_rows.pop(key))
                if not shown:
                    empty = label(_('No matches') if query else
                        _('Create a collection to organize text, files, and URLs.') if scope == 'collections' else
                        _('Add text, files, or URLs to a collection.'))
                    self.documents.append(empty)
                    self._document_rows['empty'] = empty.get_parent()
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
        self.storage.services.control.submit(fetch)

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
        self._document_limit = 100
        self.library_scope = id or 'collections'
        self.collection_id = id if id and id not in ('collections', 'all', 'ungrouped') else None
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
            if self.library_scope == 'collections':
                page = Adw.StatusPage(title=_('Knowledge Collections'), icon_name='folder-symbolic',
                    description=_('Group documents into collections, then select a collection in chat to ask questions about its content.'))
                page.set_child(button(_('Create Collection…'), self.new_collection))
                self.detail.append(page)
            else:
                self.detail.append(label(_('Select a document to inspect it. New imports always ask for a destination collection.')))
                self.detail.append(self.import_actions())
            return
        self.detail_page.set_title(collection['name'])
        self.detail.append(label(_('{0}/{1} documents ready').format(collection['ready'], collection['documents'])))
        self.detail.append(label(_('Add text, files, or URLs to this collection. Gnollama prepares the documents automatically so chats can search them.')))
        self.detail.append(self.import_actions())
        self.detail.append(button(_('Add Existing Documents…'), lambda *args: self.present_dialog(DocumentPicker(self.storage, collection['id'], self.refresh))))
        if collection['documents'] and collection['ready'] == collection['documents']:
            use = button(_('Use in New Chat'), lambda *args: self.use_collection(collection))
            use.add_css_class('suggested-action')
            self.detail.append(use)
        elif collection['documents']:
            self.detail.append(button(_('Prepare Documents'), lambda *args: self._build_collection(collection)))
        settings, _expander = section(self.detail, _('Embedding Details'))
        settings.append(label(config_label(collection)))
        settings.append(label(_('Chunk size: {0} characters · Overlap: {1} characters').format(collection['chunk_size'], collection['overlap'])))
        settings.append(label(collection['host']))
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
        options.set_halign(Gtk.Align.START)
        self.detail.append(options)
        rows = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        rows.add_css_class('boxed-list')
        self.detail.append(rows)
        for member in members:
            status = document_status(member)
            if member['error']:
                status += '\n' + member['error']
            row = Adw.ActionRow(title=member['title'], subtitle=status, activatable=True, use_markup=False)
            row.connect('activated', lambda row, id=member['id']: self.open_document(id))
            remove = Gtk.Button(icon_name='list-remove-symbolic', tooltip_text=_('Remove from Collection'), valign=Gtk.Align.CENTER)
            remove.add_css_class('flat')
            remove.connect('clicked', lambda b, id=member['id']: self.storage._submit(
                self.storage.library.remove_collection_document, collection['id'], id, on_done=self.storage.knowledge.changed))
            row.add_suffix(remove)
            rows.append(row)

    def import_actions(self):
        return actions(button(_('Add Text…'), self.add_text), button(_('Add Files…'), self.import_files),
                       button(_('Add URLs…'), self.add_urls))

    def use_collection(self, collection):
        root = self.get_root()
        if hasattr(root, 'new_chat_tab'):
            tab = root.new_chat_tab()
            options = dict(copy.deepcopy(DEFAULT_RAG), enabled=True, config_id=collection['config_id'],
                           host=collection['host'], model=collection['model'], collection_ids=[collection['id']])
            tab.knowledge_control.load(options)
            tab.knowledge_control.on_change()
            tab.chat_input.entry.grab_focus()

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
        dialog.connect('response', lambda d, r: self.storage._submit(self.storage.library.rename_knowledge_collection,
            collection['id'], entry.get_text().strip(), on_done=self.storage.knowledge.changed) if r == 'save' else None)
        self.present_dialog(dialog)

    def _delete_collection(self, collection):
        dialog = Adw.AlertDialog(heading=_('Delete Collection?'), body=_('Delete “{0}”? Its documents and embeddings stay in the library. Chats selecting this collection will need new sources; saved answers keep their original passages.').format(collection['name']))
        dialog.add_response('cancel', _('Cancel'))
        dialog.add_response('delete', _('Delete Collection'))
        dialog.set_response_appearance('delete', Adw.ResponseAppearance.DESTRUCTIVE)
        def response(d, choice):
            if choice == 'delete':
                self.storage._submit(self.storage.library.delete_knowledge_collection, collection['id'],
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
            self.storage._submit(self.storage.library.update_collection_endpoint, collection['id'], hosts.host(),
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
                                               else _('Unavailable — the original model changed or was removed. Choose a compatible server for searches.'))
                except Exception as exc:
                    states = {i['id']: _('Unavailable: {0}').format(str(exc)) for i in selected}
                def deliver():
                    self._availability_loading.discard(host)
                    if cancel in self._availability_cancels:
                        self._availability_cancels.remove(cancel)
                    if not self.closed and not cancel.is_cancelled():
                        self.availability.update(states)
                        self.refresh()
                    return False
                GLib.idle_add(deliver)
            self.storage.services.control.submit(fetch)

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
        self.detail.append(button(_('Add to Collection…'), lambda *args: self.assign_document(document)))
        for index in indexes:
            group = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                            margin_start=12, margin_end=12, margin_top=12, margin_bottom=12)
            group.append(label(config_label(index)))
            group.append(label(self.availability.get(index['id'], _('Checking model availability…'))))
            group.append(label('{0}\n{1} · {2} · {3}'.format(index['host'],
                               time.strftime('%Y-%m-%d %H:%M', time.localtime(index['created_at'])), document_status(index),
                               _('Chunks: {0}').format(index['chunks']))))
            if index['error']:
                group.append(label(index['error']))
            row_actions = [button(_('Inspect Chunks'), lambda *args, i=index: self.present_dialog(ChunkPicker(self.storage, i))),
                           button(_('Check Model'), lambda *args, i=index: self._check_model(i)),
                           button(_('Delete Index'), lambda *args, i=index: self._delete(index=i))]
            if index['status'] in ('failed', 'interrupted'):
                row_actions.append(button(_('Retry'), lambda *args, i=index: self._retry(i)))
            group.append(actions(*row_actions))
            self.detail.append(Gtk.Frame(child=group))

    def assign_document(self, document):
        dialog = WorkDialog(_('Add to Collection'), height=320)
        destination = CollectionDestination(self.storage, dialog, self.collection_id)
        dialog.box.append(label(document['title']))
        dialog.box.append(destination)
        def add(*args):
            try:
                target = destination.require()
                self.storage.knowledge.build_collection(target['id'], [document['id']])
                self.open_collection(target['id'])
                dialog.close()
            except ValueError as exc:
                dialog.show_error(exc)
        dialog.header.pack_end(button(_('Add'), add))
        self.present_dialog(dialog)

    def _jobs(self):
        clear(self.jobs_box)
        jobs = list(self.storage.knowledge.jobs.values())
        active = sum(not j['done'] for j in jobs)
        self.jobs_expander.set_visible(bool(jobs))
        self.jobs_expander.set_label(_('Background Tasks · {0} running').format(active) if active else _('Recent Background Tasks'))
        for job in [j for j in jobs if not j['done']] + [j for j in jobs if j['done']][-3:]:
            row = Gtk.Box(spacing=6)
            row.append(label(job['title'] + ': ' + job['progress'], hexpand=True))
            if not job['done']:
                row.append(button(_('Cancel'), lambda *args, j=job: self._cancel_job(j)))
            elif job['error']:
                row.append(button(_('Details'), lambda *args, j=job: self.error(j['error'])))
            self.jobs_box.append(row)

    def _cancel_job(self, job):
        collections = self.storage.library.source_collections(index_id=job['index_id']) if job.get('index_id') else []
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
                            hint=_('Saved source text. Use Fetch Again to update this URL document.') if document.get('web_source') else _('Saved source text. Import revised content as a new document.'), apply_label=_('Close'))
        dialog.editor.set_editable(False)
        plain_editor(dialog)
        self.present_dialog(dialog)

    def add_text(self, *args):
        from .knowledge_import import TextImportDialog
        self.present_dialog(TextImportDialog(self.storage, self.collection_id))

    def add_urls(self, *args, url='', document_id=None):
        from .url_import import URLImportDialog
        collection_id = self.collection_id
        if not collection_id and document_id:
            groups = self.storage.library.source_collections(document_id=document_id)
            if groups:
                collection_id = groups[0]['id']
        self.present_dialog(URLImportDialog(self.storage, collection_id, url))

    def _save_document(self, document, collection_id=None):
        existing = next((d for d in self.storage.library.knowledge_documents() if d['content_hash'] == document['content_hash']), None)
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
        self.storage._submit(self.storage.library.add_knowledge_document, document,
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
        from .knowledge_import import FileImportDialog
        self.present_dialog(FileImportDialog(self.storage, self.collection_id))

    def test_search(self, *args):
        options = copy.deepcopy(DEFAULT_RAG)
        collection = self.storage.library.knowledge_collection(self.collection_id) if self.collection_id else None
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
                self.storage._submit(self.storage.library.rename_knowledge_document, document['id'], entry.get_text().strip(), on_done=self.storage.knowledge.changed)
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
        collections = self.storage.library.source_collections(document_id=document['id']) if document else self.storage.library.source_collections(index_id=index['id'])
        if collections:
            dialog.set_body(dialog.get_body() + '\n\n' + _('Affected collections: {0}.').format(', '.join(c['name'] for c in collections)) +
                            ' ' + (_('This document will be removed from their membership.') if document else
                                   _('Their documents will need embeddings rebuilt.')))
        dialog.add_response('cancel', _('Cancel'))
        dialog.add_response('delete', _('Delete'))
        dialog.set_response_appearance('delete', Adw.ResponseAppearance.DESTRUCTIVE)
        def response(d, r):
            if r == 'delete':
                self.storage._submit(self.storage.library.delete_knowledge_document if document else self.storage.library.delete_knowledge_index,
                                     (document or index)['id'], on_done=self.storage.knowledge.changed)
        dialog.connect('response', response)
        self.present_dialog(dialog)

    def _retry(self, index):
        if any(not j['done'] and j.get('index_id') == index['id'] for j in self.storage.knowledge.jobs.values()):
            return
        config = self.storage.library.embedding_config(index['config_id'])
        self.storage.knowledge.create_index(index['document_id'], index['host'], index['model'], config,
                                             index['chunk_size'], index['overlap'], index_id=index['id'])

    def _check_model(self, index):
        dialog = WorkDialog(_('Embedding Model Availability'), height=280)
        status = label(_('Checking…'))
        dialog.box.append(status)
        dialog.run(lambda cancel: model_identity(index['host'], index['model'], cancel, index['digest']),
                   lambda result, error: status.set_text(str(error) if error else _('The original model is available on this host.')))
        self.present_dialog(dialog)
