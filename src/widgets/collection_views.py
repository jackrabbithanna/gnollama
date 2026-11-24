"""Adaptive Knowledge Library, source selection, and retrieval inspection."""
from gettext import gettext as _
from gi.repository import Gtk



from .knowledge_common import button, clear, dropdown, field, IndexDialog, label, WorkDialog

class CollectionDialog(IndexDialog):
    def __init__(self, context, on_created, collection=None):
        super().__init__(context, dict(title=_('An embedding model makes documents searchable. It can differ from the model used for chat.')), on_created, collection)
        self.set_title(_('Copy Collection with New Settings') if collection else _('Create Collection'))
        self.name = Gtk.Entry(placeholder_text=_('Collection name'),
                              text=_('{0} (copy)').format(collection['name']) if collection else '')
        self.box.prepend(self.name)
        self.box.prepend(label(_('Collection name'), selectable=False, mnemonic_widget=self.name))
        self.document_ids = [m['id'] for m in context.storage.library.collection_documents(collection['id'])] if collection else []

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
            self.context.knowledge.create_collection(self.name.get_text(), *self.settings(),
                                                      document_ids=self.document_ids, callback=done,
                                                      kind=self.host_models.kind())
            self.start.set_sensitive(False)
        except (ValueError, TypeError) as exc:
            self.show_error(exc)


class CollectionDestination(Gtk.Box):
    """Explicit destination shared by all import dialogs, with in-place creation."""
    def __init__(self, context, owner, collection_id=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.context, self.owner = context, owner
        self.storage = context.storage
        self.dropdown = dropdown([])
        field(self, _('Destination collection'), self.dropdown)
        self.create = button(_('New Collection…'), self.new_collection)
        self.create.set_halign(Gtk.Align.START)
        self.append(self.create)
        self.refresh(collection_id)

    def refresh(self, collection_id=None):
        self.collections = self.storage.library.knowledge_collections()
        self.dropdown.set_model(Gtk.StringList.new([_('Choose a collection')] + [c['name'] for c in self.collections]))
        self.dropdown.set_selected(next((n + 1 for n, c in enumerate(self.collections) if c['id'] == collection_id), 0))

    def selected(self):
        n = self.dropdown.get_selected()
        return self.collections[n - 1] if 0 < n <= len(self.collections) else None

    def require(self):
        collection = self.selected()
        if collection is None or not self.storage.library.knowledge_collection(collection['id']):
            self.dropdown.grab_focus()
            raise ValueError(_('Choose a destination collection, or create a new one.'))
        return collection

    def new_collection(self, *args):
        dialog = CollectionDialog(self.context, self.refresh)
        dialog.present(self.owner)


class DocumentPicker(WorkDialog):
    def __init__(self, context, collection_id, on_added):
        super().__init__(_('Add Existing Documents'))
        self.context, self.collection_id, self.on_added = context, collection_id, on_added
        self.storage = context.storage
        self.selected = set()
        self.documents = []
        self._load_generation = 0
        self._rendered = set()
        self.search = Gtk.SearchEntry(placeholder_text=_('Search documents'))
        self.search.connect('search-changed', lambda *args: self._load_documents())
        self.box.append(self.search)
        self.rows = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.box.append(self.rows)
        self.add = button(_('Add'), self._add)
        self.add.add_css_class('suggested-action')
        self.header.pack_end(self.add)
        self.more_documents = button(_('Load More'), lambda *args: self._load_documents(append=True))
        self.box.append(self.more_documents)
        self._load_documents()

    def _load_documents(self, append=False):
        self._load_generation += 1
        generation = self._load_generation
        offset = len(self.documents) if append else 0
        query = self.search.get_text()
        self.more_documents.set_sensitive(False)
        def loaded(docs, error):
            if generation != self._load_generation:
                return
            self.more_documents.set_sensitive(True)
            if error:
                self.show_error(error)
                return
            if not append:
                self.documents = []
                self._rendered.clear()
                clear(self.rows)
            self.documents.extend(docs[:100])
            self.more_documents.set_visible(len(docs) > 100)
            self._render()
        self.run(lambda cancel: self.storage.library.document_page(query, limit=101, offset=offset,
            exclude_collection=self.collection_id), loaded)

    def _render(self, *args):
        query = self.search.get_text().casefold()
        for doc in self.documents:
            if doc['id'] in self._rendered:
                continue
            self._rendered.add(doc['id'])
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
            self.context.knowledge.build_collection(self.collection_id, sorted(self.selected))
            self.on_added()
            self.close()
        except ValueError as exc:
            self.show_error(exc)


