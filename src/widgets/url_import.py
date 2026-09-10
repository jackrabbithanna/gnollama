"""One batch review dialog for URL imports; only explicit saves modify the library."""
import tempfile

from gi.repository import Adw, Gtk

from ..knowledge import check_cancel, make_document
from ..web_import import MAX_CACHE, MAX_DOWNLOAD, extract_download, fetch_url, parse_urls
from .json_view import buffer_text, code_view, editor_frame
from .knowledge_view import CollectionDestination, WorkDialog, actions, button, field, label, section


class URLImportDialog(WorkDialog):
    def __init__(self, storage, collection_id=None, url=''):
        super().__init__(_('Add URLs'), width=800, height=760)
        self.storage = storage
        self.collections = storage.db.knowledge_collections()
        self.collection_id = None
        self.items = []
        self.current = None
        self.active = 0
        self.cache = tempfile.TemporaryDirectory(prefix='gnollama-urls-')
        self.connect('closed', self._stop)
        self.setup = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.box.append(self.setup)
        self.destination_picker = CollectionDestination(storage, self, collection_id)
        self.destination = self.destination_picker.dropdown
        self.setup.append(self.destination_picker)
        self.setup.append(label(_('Paste up to 20 URLs, one per line. Review each page before saving.')))
        self.urls = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.urls.get_buffer().set_text(url)
        self.urls.set_left_margin(12)
        self.urls.set_right_margin(12)
        self.urls.set_top_margin(8)
        self.urls.set_bottom_margin(8)
        self.setup.append(editor_frame(self.urls, min_content_height=80))
        self.fetch_button = button(_('Fetch URLs'), self.start)
        self.fetch_button.add_css_class('suggested-action')
        self.header.pack_end(self.fetch_button)
        self.destination_label = label(visible=False)
        self.box.append(self.destination_label)
        self.notice = label()
        self.box.append(self.notice)
        self.rows = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.rows.add_css_class('boxed-list')
        self.rows.connect('row-selected', self._selected)
        self.box.append(Gtk.ScrolledWindow(child=self.rows, max_content_height=150,
                                           propagate_natural_height=True, hscrollbar_policy=Gtk.PolicyType.NEVER))
        self.preview = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, visible=False)
        self.box.append(self.preview)
        self.source_label = label()
        self.preview.append(self.source_label)
        self.title_entry = Gtk.Entry()
        field(self.preview, _('Document title'), self.title_entry)
        self.selector_box, self.selector_expander = section(self.preview, _('Refine Extraction'))
        self.selector_box.append(label(_('If the preview includes navigation or misses the article, select the page region to extract using a CSS selector.')))
        self.selector_box.append(label(_('CSS selector (optional)')))
        self.selector = Gtk.Entry(placeholder_text=_('For example: main, article, or .article-body'))
        self.selector_box.append(self.selector)
        self.reextract_button = button(_('Re-extract'), self.reextract)
        self.selector_box.append(self.reextract_button)
        self.editor = code_view(editable=True)
        if hasattr(self.editor.get_buffer(), 'set_language'):
            self.editor.get_buffer().set_language(None)
        self.preview.append(label(_('Extracted content (editable)')))
        self.preview.append(editor_frame(self.editor, min_content_height=220))
        self.page_notice = label()
        self.preview.append(self.page_notice)
        self.save_button = button(_('Add to Collection'), self.save)
        self.save_button.add_css_class('suggested-action')
        self.retry_button = button(_('Fetch Again'), self.retry)
        self.skip_button = button(_('Skip'), self.skip)
        self.footer = actions(self.save_button, self.skip_button, self.retry_button)
        for side in ('start', 'end', 'top', 'bottom'):
            getattr(self.footer, 'set_margin_' + side)(12)
        self.footer.set_visible(False)
        self.toolbar.add_bottom_bar(self.footer)
        self.storage.knowledge.listeners.append(self.update_saved)

    def start(self, *args):
        try:
            urls = parse_urls(buffer_text(self.urls))
            target = self.destination_picker.require()
            self.collection_id = target['id']
        except ValueError as exc:
            self.show_error(exc)
            return
        if self.items:
            return
        self.error.set_visible(False)
        self.urls.set_editable(False)
        self.destination.set_sensitive(False)
        self.fetch_button.set_sensitive(False)
        self.setup.set_visible(False)
        self.fetch_button.set_visible(False)
        self.destination_label.set_text(_('Collection: {0}').format(target['name']))
        self.destination_label.set_visible(True)
        for url in urls:
            row = Gtk.ListBoxRow()
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                          margin_start=8, margin_end=8, margin_top=6, margin_bottom=6)
            box.append(label(url, selectable=False))
            status = label(_('Queued'), selectable=False)
            box.append(status)
            row.set_child(box)
            item = dict(url=url, row=row, status_label=status, status='queued', download=None,
                        document=None, source=None, expected=None, selector='', title='', text='', error='', warnings=[], job=None)
            self.items.append(item)
            self.rows.append(row)
        self.rows.select_row(self.items[0]['row'])
        self._schedule()

    def _status(self, item, status):
        item['status'] = status
        item['status_label'].set_text({
            'queued': _('Queued'), 'fetching': _('Downloading…'), 'extracting': _('Extracting…'),
            'ready': _('Ready to review'), 'failed': _('Needs attention'), 'saving': _('Saving…'),
            'saved': _('Added to Collection — Preparing'), 'skipped': _('Skipped'),
        }[status])

    def _schedule(self):
        if self.closed:
            return
        for item in self.items:
            used = sum(max(i['download'].size if i['download'] else 0, i.get('reserved', 0)) for i in self.items)
            if self.active >= 2 or used + MAX_DOWNLOAD > MAX_CACHE:
                break
            if item['status'] == 'queued':
                self._launch(item)
        queued = any(i['status'] == 'queued' for i in self.items)
        self.notice.set_text(_('Save or skip reviewed pages to make room for queued downloads.') if queued and self.active == 0 else
                             _('Saved documents build embeddings in the background. Closing discards unsaved previews.'))
        self._refresh_actions()

    def _launch(self, item, extract_only=False):
        if self.closed or self.active >= 2:
            return
        resource = item['download'] if extract_only else None
        if not extract_only:
            if item['download']:
                item['download'].discard()
                item['download'] = None
            item['expected'] = self.storage.db.web_document(item['url'])
            item['selector'] = (item['expected'] or {}).get('web_source', {}).get('selector', item['selector'])
        selector = item['selector']
        item['error'] = ''
        self._status(item, 'extracting' if extract_only else 'fetching')
        item['reserved'] = resource.size if resource else MAX_DOWNLOAD
        self.active += 1
        def task(cancel, progress):
            downloaded = resource
            handed_off = False
            try:
                if downloaded is None:
                    downloaded = fetch_url(item['url'], self.cache.name, cancel, progress)
                progress(_('Extracting content…'))
                try:
                    result = extract_download(downloaded, selector, cancel)
                    error = ''
                except ValueError as exc:
                    result, error = None, str(exc)
                check_cancel(cancel)
                handed_off = True
                return downloaded, result, error
            finally:
                if downloaded and not handed_off:
                    downloaded.discard()
        def completed(value, error):
            self.active -= 1
            item['reserved'] = 0
            if self.closed or item['status'] == 'skipped':
                if value:
                    value[0].discard()
                if self.closed:
                    if not self.active:
                        self.cache.cleanup()
                    return
                self._schedule()
                return
            if value:
                item['download'], result, item['error'] = value
                if result:
                    document, source, warnings = result
                    item.update(document=document, source=source, warnings=warnings, text=document['text'])
                    if not extract_only:
                        item['title'] = (item['expected'] or document)['title']
            else:
                item['download'] = None  # The failed/cancelled task discarded its temporary file.
                item['error'] = str(error)
            self._status(item, 'failed' if item['error'] else 'ready')
            if item is self.current:
                self._render()
            self._schedule()
        try:
            item['job'] = self.storage.knowledge.submit(_('Import URL: {0}').format(item['url']), task, completed, importing=True)
        except ValueError as exc:
            self.active -= 1
            item['reserved'] = 0
            item['error'] = str(exc)
            self._status(item, 'failed')
        if item is self.current:
            self._render()

    def _capture(self):
        if self.current and self.current['status'] in ('ready', 'failed'):
            self.current.update(title=self.title_entry.get_text(), text=buffer_text(self.editor), selector=self.selector.get_text())

    def _selected(self, box, row):
        self._capture()
        self.current = next((i for i in self.items if i['row'] is row), None)
        self._render()

    def _render(self):
        item = self.current
        self.preview.set_visible(item is not None)
        self.footer.set_visible(item is not None)
        if item is None:
            return
        busy = item['status'] in ('queued', 'fetching', 'extracting', 'saving')
        finished = item['status'] in ('saved', 'skipped')
        editable = not busy and not finished
        self.title_entry.set_text(item['title'])
        self.title_entry.set_sensitive(editable)
        self.editor.get_buffer().set_text(item['text'])
        self.editor.set_editable(editable)
        self.selector.set_text(item['selector'])
        download = item['download']
        html = download is not None and download.content_type in ('text/html', 'application/xhtml+xml')
        self.selector_expander.set_visible(html)
        self.selector_box.set_sensitive(editable)
        self.source_label.set_text(download.final_url if download else item['url'])
        self.page_notice.set_text(item['error'] or '\n'.join(item['warnings']))
        self.save_button.set_sensitive(editable and item['document'] is not None and not item['error'])
        self.save_button.set_label(_('Replace Document…') if item['expected'] else _('Add to Collection'))
        self.retry_button.set_label(_('Retry') if item['status'] == 'failed' else _('Fetch Again'))
        self.skip_button.set_sensitive(not finished and item['status'] != 'saving')
        self._refresh_actions()

    def _refresh_actions(self):
        editable = self.current is not None and self.current['status'] in ('ready', 'failed')
        self.retry_button.set_sensitive(editable)
        self.reextract_button.set_sensitive(editable and self.active < 2)

    def _discard_edits(self, callback):
        item = self.current
        if item['document'] and item['text'] != item['document']['text']:
            dialog = Adw.AlertDialog(heading=_('Discard Preview Edits?'),
                                     body=_('Extracting again replaces your edited text with the page content.'))
            dialog.add_response('cancel', _('Cancel'))
            dialog.add_response('discard', _('Discard Edits'))
            dialog.set_response_appearance('discard', Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.connect('response', lambda d, r: callback() if r == 'discard' and not self.closed else None)
            dialog.present(self)
        else:
            callback()

    def reextract(self, *args):
        self._capture()
        item = self.current
        if item and item['download'] and item['status'] in ('ready', 'failed'):
            self._discard_edits(lambda: self._launch(item, extract_only=True))

    def retry(self, *args):
        self._capture()
        item = self.current
        if item and item['status'] in ('ready', 'failed'):
            def queue():
                if item['download']:
                    item['download'].discard()
                    item['download'] = None
                self._status(item, 'queued')
                self._render()
                self._schedule()
            self._discard_edits(queue)

    def skip(self, *args):
        item = self.current
        if not item or item['status'] in ('saved', 'saving', 'skipped'):
            return
        if item['job']:
            item['job']['cancel'].cancel()
        if item['download'] and item['status'] != 'extracting':
            item['download'].discard()
        item['download'] = None
        self._status(item, 'skipped')
        item.update(text='', document=None)
        self._render()
        self._schedule()

    def save(self, *args):
        self._capture()
        item = self.current
        if not item or item['status'] not in ('ready', 'failed') or not item['document'] or item['error']:
            return
        try:
            original = item['document']
            edited = item['text'] != original['text']
            document = make_document(item['title'], item['text'], original['filename'],
                                     [] if edited else original['pages'], original['file_hash'])
            source = dict(item['source'], edited=edited)
        except ValueError as exc:
            self.show_error(exc)
            return
        def commit():
            def done(result, error):
                if self.closed:
                    return
                if error:
                    item['error'] = str(error)
                    self._status(item, 'failed')
                else:
                    item['warnings'] = result['warnings']
                    item['saved_id'] = result['id']
                    self._status(item, 'saved')
                    if item['download']:
                        item['download'].discard()
                        item['download'] = None
                    item['document'] = None
                    item['text'] = ''
                if item is self.current:
                    self._render()
                self.update_saved()
                self._schedule()
            try:
                item['job'] = self.storage.knowledge.save_web_document(document, source, self.collection_id, item['expected'], done)
                self.error.set_visible(False)
                self._status(item, 'saving')
                self._render()
            except ValueError as exc:
                self.show_error(exc)
        expected = item['expected']
        if expected and expected['content_hash'] != document['content_hash']:
            collections = self.storage.db.source_collections(document_id=expected['id'])
            count = len(self.storage.db.knowledge_indexes(document_id=expected['id']))
            dialog = Adw.AlertDialog(heading=_('Replace Document and Rebuild?'),
                body=_('Replace “{0}” and rebuild {1} embedding indexes? Saved chat answers keep their original passages. Affected collections cannot search this content until rebuilding succeeds.').format(expected['title'], count)
                     + '\n\n' + _('Collections: {0}').format(', '.join(c['name'] for c in collections) or _('None')))
            dialog.add_response('cancel', _('Cancel'))
            dialog.add_response('replace', _('Replace and Rebuild'))
            dialog.set_response_appearance('replace', Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.connect('response', lambda d, r: commit() if r == 'replace' and not self.closed else None)
            dialog.present(self)
        else:
            commit()

    def update_saved(self):
        if self.closed or not self.collection_id:
            return
        members = {m['id']: m for m in self.storage.db.collection_documents(self.collection_id)}
        for item in self.items:
            if item['status'] != 'saved':
                continue
            member = members.get(item.get('saved_id'))
            ready = member and member['status'] == 'complete' and member['chunks']
            failed = member and member['status'] in ('failed', 'interrupted')
            item['status_label'].set_text(_('Ready to Search') if ready else _('Added to Collection — Needs Attention') if failed else _('Added to Collection — Preparing'))

    def _stop(self, *args):
        if self.update_saved in self.storage.knowledge.listeners:
            self.storage.knowledge.listeners.remove(self.update_saved)
        for item in self.items:
            if item['job'] and item['status'] in ('fetching', 'extracting'):
                item['job']['cancel'].cancel()
            elif item['download']:
                item['download'].discard()
        if not self.active:
            self.cache.cleanup()
