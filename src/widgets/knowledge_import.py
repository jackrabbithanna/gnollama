"""Collection-aware text imports and a bounded, sequential file review queue."""
from gi.repository import Gtk, Gio, GLib

from ..knowledge import extract_document, make_document
from .json_view import TextEditor, code_view, editor_frame
from .knowledge_view import (CollectionDestination, WorkDialog, actions, button, field,
                             label, plain_editor, preview_text)


class TextImportDialog(TextEditor):
    def __init__(self, storage, collection_id=None):
        super().__init__('', self.save, title=_('Add Text'),
                         hint=_('Paste text, choose a collection, and review it before adding.'),
                         apply_label=_('Add to Collection'))
        self.storage = storage
        self.closed = False
        self.connect('closed', lambda *args: setattr(self, 'closed', True))
        self.destination = CollectionDestination(storage, self, collection_id)
        self.title_entry = Gtk.Entry()
        fields = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        fields.append(self.destination)
        field(fields, _('Document title'), self.title_entry)
        self.content_box.prepend(fields)
        plain_editor(self)

    def save(self, text):
        try:
            target = self.destination.require()
            document = make_document(self.title_entry.get_text(), text)
            self.storage.knowledge.import_document(document, target['id'], self.saved)
        except ValueError as exc:
            self.show_error(exc)
            return False
        self.apply_button.set_sensitive(False)
        self.destination.set_sensitive(False)
        self.title_entry.set_sensitive(False)
        self.editor.set_editable(False)
        return False

    def saved(self, result, error):
        if self.closed:
            return
        if error:
            self.show_error(error)
            self.apply_button.set_sensitive(True)
            self.destination.set_sensitive(True)
            self.title_entry.set_sensitive(True)
            self.editor.set_editable(True)
        elif result['warning']:
            self.show_error(_('Added to Collection — Needs Attention') + '\n' + result['warning'])
            self.apply_button.set_label(_('Close'))
            self.apply_button.set_sensitive(True)
            self.on_apply = lambda text: None
        else:
            self.close()


class FileImportDialog(WorkDialog):
    def __init__(self, storage, collection_id=None):
        super().__init__(_('Add Files'), width=760, height=720)
        self.storage = storage
        self.items = []
        self.current = None
        self.job = None
        self.destination = CollectionDestination(storage, self, collection_id)
        self.box.append(self.destination)
        self.box.append(label(_('Import PDF or UTF-8 text files, including Markdown and source code. Maximum size: 50 MiB per file. Review one file at a time before adding.')))
        self.choose = button(_('Choose Files…'), self.choose_files)
        self.choose.add_css_class('suggested-action')
        self.header.pack_end(self.choose)
        self.rows = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.box.append(Gtk.ScrolledWindow(child=self.rows, max_content_height=120,
                        propagate_natural_height=True, hscrollbar_policy=Gtk.PolicyType.NEVER))
        self.preview = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, visible=False)
        self.box.append(self.preview)
        self.filename = label()
        self.preview.append(self.filename)
        self.title_entry = Gtk.Entry()
        field(self.preview, _('Document title'), self.title_entry)
        self.editor = code_view(language=None)
        self.preview.append(label(_('Extracted content (read-only)')))
        self.preview.append(editor_frame(self.editor, min_content_height=200))
        self.notice = label()
        self.preview.append(self.notice)
        self.add = button(_('Add to Collection'), self.save)
        self.add.add_css_class('suggested-action')
        self.skip_button = button(_('Skip'), self.skip)
        self.retry_button = button(_('Retry'), self.retry)
        self.footer = actions(self.add, self.skip_button, self.retry_button)
        for side in ('start', 'end', 'top', 'bottom'):
            getattr(self.footer, 'set_margin_' + side)(12)
        self.footer.set_visible(False)
        self.toolbar.add_bottom_bar(self.footer)
        self.storage.knowledge.listeners.append(self.update_saved)
        self.connect('closed', self.stop)

    def choose_files(self, *args):
        try:
            self.destination.require()
        except ValueError as exc:
            self.show_error(exc)
            return
        supported = Gtk.FileFilter(name=_('Text and PDF files'))
        for mime in ('text/*', 'application/pdf', 'application/json', 'application/xml', 'application/javascript'):
            supported.add_mime_type(mime)
        for pattern in ('*.md', '*.markdown', '*.txt', '*.py', '*.rs', '*.js', '*.ts', '*.json', '*.pdf'):
            supported.add_pattern(pattern)
        all_files = Gtk.FileFilter(name=_('All files'))
        all_files.add_pattern('*')
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(supported)
        filters.append(all_files)
        dialog = Gtk.FileDialog(title=_('Choose Text or PDF Files'), filters=filters)
        def selected(dialog, result):
            try:
                files = dialog.open_multiple_finish(result)
                if not self.closed:
                    self.start_files([files.get_item(n) for n in range(files.get_n_items())])
            except GLib.Error as exc:
                if not self.closed and not exc.matches(Gtk.dialog_error_quark(), Gtk.DialogError.DISMISSED):
                    self.show_error(exc)
        dialog.open_multiple(self.get_root(), self.cancel, selected)

    def start_files(self, files):
        if self.items or not files or self.closed:
            return
        try:
            self.collection_id = self.destination.require()['id']
        except ValueError as exc:
            self.show_error(exc)
            return
        self.destination.set_sensitive(False)
        self.destination.create.set_visible(False)
        self.choose.set_visible(False)
        self.error.set_visible(False)
        for file in files:
            status = label()
            self.rows.append(status)
            item = dict(file=file, status='queued', row=status, document=None, warnings=[])
            self.items.append(item)
            self.status(item, 'queued')
        self.advance()

    def status(self, item, state):
        item['status'] = state
        text = {'queued': _('Queued'), 'extracting': _('Extracting'), 'ready': _('Ready to Review'),
                'saving': _('Adding to Collection'), 'saved': _('Added to Collection — Preparing'),
                'failed': _('Needs Attention'), 'skipped': _('Skipped')}[state]
        item['row'].set_text(item['file'].get_basename() + ' · ' + text)

    def advance(self):
        self.current = next((i for i in self.items if i['status'] == 'queued'), None)
        self.preview.set_visible(self.current is not None)
        self.footer.set_visible(self.current is not None)
        if self.current:
            self.extract()
        else:
            self.box.append(label(_('Review complete. Added documents prepare in the background; their status is shown in the collection.')))

    def extract(self):
        item = self.current
        self.status(item, 'extracting')
        self.filename.set_text(item['file'].get_basename())
        self.title_entry.set_text('')
        self.editor.get_buffer().set_text('')
        self.notice.set_text(_('Extracting text…'))
        self.add.set_sensitive(False)
        self.skip_button.set_sensitive(False)
        self.retry_button.set_visible(False)
        def run(cancel, progress):
            file = item['file']
            info = file.query_info('standard::size', Gio.FileQueryInfoFlags.NONE, cancel)
            if info.get_size() > 50 * 1024 * 1024:
                raise ValueError(_('Files must be no larger than 50 MiB.'))
            _, raw, _etag = file.load_contents(cancel)
            return extract_document(file.get_basename(), raw, cancel)
        def complete(result, error):
            if self.closed:
                return
            self.skip_button.set_sensitive(True)
            if error:
                self.status(item, 'failed')
                self.notice.set_text(str(error))
                self.retry_button.set_visible(True)
            else:
                item['document'], item['warnings'] = result
                self.status(item, 'ready')
                self.title_entry.set_text(item['document']['title'])
                self.editor.get_buffer().set_text(preview_text(item['document']))
                self.notice.set_text('\n'.join(item['warnings']))
                self.add.set_sensitive(True)
        try:
            self.job = self.storage.knowledge.submit(_('Import file: {0}').format(item['file'].get_basename()), run, complete, importing=True)
        except ValueError as exc:
            complete(None, exc)

    def save(self, *args):
        item = self.current
        if not item or item['status'] != 'ready':
            return
        document = dict(item['document'], title=self.title_entry.get_text().strip() or item['document']['title'])
        def complete(result, error):
            if self.closed:
                return
            if error:
                self.status(item, 'ready')
                self.notice.set_text(str(error))
                self.add.set_sensitive(True)
                self.skip_button.set_sensitive(True)
            else:
                item['saved_id'] = result['id']
                self.status(item, 'saved')
                if result['warning']:
                    item['row'].set_text(document['title'] + ' · ' + _('Added to Collection — Needs Attention') + '\n' + result['warning'])
                item['document'] = None
                self.update_saved()
                self.advance()
        try:
            self.job = self.storage.knowledge.import_document(document, self.collection_id, complete)
            self.status(item, 'saving')
            self.add.set_sensitive(False)
            self.skip_button.set_sensitive(False)
        except ValueError as exc:
            self.notice.set_text(str(exc))

    def skip(self, *args):
        if self.current and self.current['status'] in ('ready', 'failed'):
            self.status(self.current, 'skipped')
            self.current['document'] = None
            self.advance()

    def retry(self, *args):
        if self.current and self.current['status'] == 'failed':
            self.extract()

    def update_saved(self):
        if self.closed or not hasattr(self, 'collection_id'):
            return
        members = {m['id']: m for m in self.storage.db.collection_documents(self.collection_id)}
        for item in self.items:
            if item['status'] != 'saved':
                continue
            member = members.get(item.get('saved_id'))
            ready = member and member['status'] == 'complete' and member['chunks']
            failed = member and member['status'] in ('failed', 'interrupted')
            status = _('Ready to Search') if ready else _('Added to Collection — Needs Attention') if failed else _('Added to Collection — Preparing')
            item['row'].set_text(item['file'].get_basename() + ' · ' + status)

    def stop(self, *args):
        if self.update_saved in self.storage.knowledge.listeners:
            self.storage.knowledge.listeners.remove(self.update_saved)
        if self.job and self.current and self.current['status'] == 'extracting':
            self.job['cancel'].cancel()
