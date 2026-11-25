"""An image session: a prompt, the references and settings it is sent with, and the images of each run."""
from gettext import gettext as _
from types import SimpleNamespace
from gi.repository import Gtk, GObject, GLib
from ..drafts import DraftController
from ..image_run import ImageController, prepare
from ..records import ImageSettings, ModelTarget
from .chat_input import ChatInput
from .decision_images import in_formats
from .image_results import ImageRunView
from .image_settings import ImageSettingsForm
from .model_target import TargetPicker


class ImageTab(Gtk.Box):
    __gsignals__ = {'chat-updated': (GObject.SignalFlags.RUN_FIRST, None, (str, str)),
                    'request-changed': (GObject.SignalFlags.RUN_FIRST, None, ())}
    title = GObject.Property(type=str, default='')

    def __init__(self, context, saved=None, draft=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.context, self.mode = context, 'image'
        self.storage = context.storage
        self.strategy = SimpleNamespace(chat_id=saved['id'] if saved else None, deleted=False)
        self.title = saved['title'] if saved else _('New Image')
        self.closing = self._disposed = self._close_saving = False
        self._close_callback = None
        self._discard_on_close = False
        self._page_loading = False
        # The runs shown are one unbroken stretch of the session's `_total`: from `_offset` up to `_end`.
        self._offset = self._end = self._total = 0
        self.runs = {}
        # The host and model the draft knows of. A list that arrives selects a model, which is no edit.
        self._target = None
        # Whether the selected model takes references, which the images of runs are offered as.
        self._references = False
        self.controller = ImageController(context, self._changed, self._started, self._finished, in_formats)
        if saved:
            self.controller.load(saved)
        self.draft = DraftController(context.storage, self.mode, self.read_draft, id=draft['id'] if draft else None,
                                     chat_id=saved['id'] if saved else None)
        self.draft.restoring = True
        # What the editor holds when it has no draft: a session's last run, or nothing.
        last = (saved.get('last_run') or (saved['runs'][-1] if saved['runs'] else {})) if saved else {}
        self._saved = (ImageSettings.from_stored(saved['options']) if saved else ImageSettings(),
                       last.get('prompt', ''), last.get('references') or [])
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                          margin_start=12, margin_end=12, margin_top=12, margin_bottom=12)
        self.scroll = Gtk.ScrolledWindow(child=content, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.append(self.scroll)
        self.picker = TargetPicker(context, self._target_changed, kind='image')
        content.append(self.picker)

        content.append(Gtk.Label(label=_('Prompt'), xalign=0, css_classes=['heading']))
        # The prompt. Its host and model controls are the picker's, and Generate is this tab's own.
        self.chat_input = ChatInput()
        self.chat_input.services = context.services
        self.chat_input.discovery_enabled = False
        self.chat_input.connection_box.set_visible(False)
        self.chat_input.send_button.set_visible(False)
        self.chat_input.entry.send_on_enter = False
        self.chat_input.entry.set_placeholder_text(_('Describe the image to generate.'))
        self.chat_input.entry.update_property([Gtk.AccessibleProperty.LABEL], [_('Prompt')])
        # Its own margins would set the prompt in from the rest of the tab.
        inner = self.chat_input.entry.get_parent().get_parent()
        for side in ('start', 'end', 'top', 'bottom'):
            inner.set_property('margin-' + side, 0)
        content.append(self.chat_input)
        # The reference images, below the prompt.
        self.attachments = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        for widget in (self.chat_input.image_preview_scrolled, self.chat_input.attach_button.get_parent(),
                       self.chat_input.capability_notice):
            widget.get_parent().remove(widget)
            for side in ('start', 'end', 'bottom'):
                widget.set_property('margin-' + side, 0)
            self.attachments.append(widget)
        content.append(self.attachments)

        self.form = ImageSettingsForm()
        content.append(self.form)
        self.notice = Gtk.Label(wrap=True, xalign=0, selectable=True, visible=False)
        content.append(self.notice)
        actions = Gtk.Box(spacing=6)
        self.run_button = Gtk.Button(css_classes=['suggested-action'])
        self.run_button.connect('clicked', self.run_or_stop)
        self.discard_button = Gtk.Button(label=_('Discard Draft'))
        self.discard_button.connect('clicked', self.discard_draft)
        actions.append(self.run_button)
        actions.append(self.discard_button)
        content.append(actions)

        self.results_title = Gtk.Label(label=_('Results'), xalign=0, css_classes=['title-3'], visible=False)
        content.append(self.results_title)
        self.older = Gtk.Button(label=_('Load earlier runs'), halign=Gtk.Align.START, visible=False)
        self.older.connect('clicked', self.load_older)
        content.append(self.older)
        self.results = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        content.append(self.results)
        self.later = Gtk.Button(label=_('Load later runs'), halign=Gtk.Align.START, visible=False)
        self.later.connect('clicked', self.load_later)
        content.append(self.later)

        self.chat_input.entry.connect('changed', self._edited)
        # An image being read holds a run back until it is attached.
        self.chat_input.connect('attachments-ready', lambda *args: self._changed())
        self.chat_input.entry.connect('activate', self.run)
        self.form.connect('changed', self._edited)
        self._load(*self._saved)
        if saved:
            self._offset = saved.get('message_offset', 0)
            self._end = self._offset + len(saved['runs'])
            self._total = saved.get('message_count', self._end)
            for run in saved['runs']:
                self._show_run(run)
            # Edits made since the last run are a draft kept under the session's own id.
            revision = self.draft.revision
            def read():
                kept = self.storage.get_draft(saved['id'])
                GLib.idle_add(lambda: self.restore_draft(kept) if kept and self.draft.revision == revision else None)
            context.services.control.submit(read)
        elif draft:
            self._apply_draft(draft)
        self.draft.restoring = False
        self._show_capabilities()
        self._changed()

    # The window's view of a tab.

    @property
    def request(self):
        return self.controller.request

    # The editor.

    def parameters(self):
        """What the selected model takes, or None before a model is selected."""
        return self.picker.image_parameters() if self.picker.input.get_selected_model() else None

    def _show_capabilities(self):
        """Offer settings and references as far as the selected model takes them.

        References already attached stay; `_problem` says what holds them back.
        """
        parameters = self.parameters()
        host = self.picker.panel.get_selected_host()
        capabilities = self.context.capabilities(host) if host else None
        self._references = bool(parameters and parameters.references and capabilities
                                and capabilities.image_references)
        self.chat_input.set_image_support(self._references,
                                          '' if self._references else _('This model does not take reference images.'))
        restoring, self.draft.restoring = self.draft.restoring, True
        self.form.set_parameters(parameters)
        self.draft.restoring = restoring

    def references(self):
        """The reference images, as base64."""
        return self.chat_input.get_images()

    def prompt(self):
        return self.chat_input.entry.read_draft()

    def use_reference(self, image):
        """Add a generated image to the references of the next run."""
        if self.request or self.closing or not self._references:
            return
        # Added, not restored: restoring would discard files still being read.
        self.chat_input.add_images([image])
        self._edited()

    def settings(self):
        """What the editor asks, and of which model."""
        target = self.picker.value()
        return ImageSettings(ModelTarget(target['host_id'], model=target['model']), self.form.options())

    def _load(self, settings, prompt, references=()):
        restoring, self.draft.restoring = self.draft.restoring, True
        self.form.set_options(settings.options)
        self.chat_input.entry.restore_draft(prompt)
        self.chat_input.restore_images(references)
        if settings.target is not None:
            self._target = dict(host_id=settings.target.host_id, model=settings.target.model)
            self.picker.select(self._target)
        self.draft.restoring = restoring

    def read_draft(self):
        return dict(text=self.prompt(), images=self.references(), settings=self.settings().to_stored())

    def _apply_draft(self, draft):
        self._load(ImageSettings.from_stored(draft.get('settings')), draft.get('text', ''), draft.get('images') or [])
        self.draft.revision = draft.get('revision', 0)

    def restore_draft(self, draft):
        # An open editor keeps edits more recent than the saved draft.
        if self.draft.dirty or self.request:
            return
        restoring, self.draft.restoring = self.draft.restoring, True
        self._apply_draft(draft)
        self.draft.restoring = restoring
        self._changed()

    def discard_draft(self, *args):
        if self.request:
            return
        self.draft.consumed()
        self.storage.delete_draft(self.draft.id)
        self._load(*self._saved)
        self.controller.error = ''
        self.emit('chat-updated', '', '')
        self._changed()

    def _target_changed(self, *args):
        if not hasattr(self, 'run_button'):
            return
        self._show_capabilities()
        value = self.picker.value()
        # While a list is on its way to the model it was asked for, what it selects first is not a choice.
        if value['model'] and value != self._target and not self.picker.input.pending_model_selection:
            chosen, self._target = self._target is not None, value
            if chosen:
                self.draft.changed()
        self._changed()

    def _edited(self, *args):
        self.draft.changed()
        self.controller.error = ''
        if hasattr(self, 'run_button'):
            self._changed()

    # Running.

    def _problem(self):
        """Why the editor's request cannot be sent, or ''. The picker shows its host's reason, and an empty prompt
        only holds Generate back."""
        if not self.picker.input.get_selected_model() or not self.prompt().strip():
            return ''
        try:
            prepare(self.context, self.picker.panel.get_selected_host(), self.settings(), self.prompt(),
                    self.references(), self.picker.input.selected_details())
        except ValueError as exc:
            return str(exc)
        return ''

    def _ready(self):
        return bool(not self.closing and self.picker.input.get_selected_model()
                    and not self.chat_input.pending_imports and self.prompt().strip())

    def run_or_stop(self, *args):
        if self.request:
            self.controller.stop()
        else:
            self.run()

    def run(self, *args):
        if self.request or not self._ready() or self._problem():
            return
        try:
            settings, prompt, references = self.settings(), self.prompt(), self.references()
            self.draft.flush()
            request = self.controller.start(self.picker.panel.get_selected_host(), settings, prompt, references,
                                            self.picker.input.selected_details(), self.draft.id, self.draft.revision)
        except ValueError as exc:
            self.controller.error = str(exc)
            self._changed()
            return
        if request:
            # The settings as chosen, not as sent: a default the model was sent stays a default.
            self._saved = (settings, prompt, references)

    def _changed(self):
        if self._disposed:
            return
        busy = self.request is not None
        problem = '' if busy else self._problem()
        text = problem or self.controller.error
        self.notice.set_text(text)
        self.notice.set_visible(bool(text))
        self.run_button.set_label(_('Stop') if busy else _('Generate'))
        self.run_button.set_sensitive(busy or (self._ready() and not problem))
        for widget in (self.picker, self.chat_input, self.attachments, self.form):
            widget.set_sensitive(not busy and not self.closing)
        for view in self.runs.values():
            view.allow_references(self._references and not busy and not self.closing)
        self.discard_button.set_sensitive(not busy)
        self.emit('request-changed')
        self._finish_close()

    def _started(self, run):
        session = self.controller.session_id
        if self.strategy.chat_id is None:
            self.strategy.chat_id = session
            self.title = self.controller.title
            # From here on a draft is the session's, kept under its id.
            self.draft.id = self.draft.chat_id = session
        self.draft.consumed()
        if self._disposed:
            return
        self._total += 1
        if self._end == self._total - 1:
            self._end = self._total
            self._show_run(run, reveal=True)
        else:
            # Later runs are not shown, so it would follow the wrong one: show the session's last page.
            self._show_page(lambda: self.storage.conversation_page(session), run['id'])
        self.emit('chat-updated', session, self.title)

    def _finished(self, run):
        if self._disposed:
            return
        view = self.runs.get(run['id'])
        if view:
            view.update(run)
        self.emit('chat-updated', self.controller.session_id, self.title)

    # Results.

    def _show_run(self, run, reveal=False, first=False):
        view = ImageRunView(run, self.context.services.control.submit, self.use_reference)
        view.allow_references(self._references and self.request is None and not self.closing)
        self.runs[run['id']] = view
        if first:
            self.results.prepend(view)
        else:
            self.results.append(view)
        self.results_title.set_visible(True)
        self._show_pages()
        if reveal:
            self._reveal(view)

    def _reveal(self, view):
        def scroll():
            valid, bounds = view.compute_bounds(self.scroll.get_child())
            if valid and not self._disposed:
                self.scroll.get_vadjustment().set_value(bounds.get_y())
            return False
        GLib.idle_add(scroll)

    def _read(self, read, done):
        """Read stored runs on a worker and hand them to `done`; one read at a time."""
        if self._page_loading:
            return
        self._page_loading = True
        def work():
            try:
                result, error = read(), None
            except Exception as exc:
                result, error = None, str(exc)
            GLib.idle_add(ready, result, error)
        def ready(result, error):
            self._page_loading = False
            if self._disposed or self.closing:
                return False
            if error:
                self.controller.error = error
                self._changed()
            elif result is not None:
                done(result)
            return False
        self.context.services.control.submit(work)

    def load_older(self, *args):
        if not self._offset:
            return
        session, offset = self.controller.session_id, max(0, self._offset - 50)
        limit = self._offset - offset
        def done(runs):
            self._offset = offset
            for run in reversed(runs):
                if run['id'] not in self.runs:
                    self._show_run(run, first=True)
            self._show_pages()
        self._read(lambda: self.storage.library.image_runs(session, limit, offset), done)

    def load_later(self, *args):
        if self._end >= self._total:
            return
        session, start = self.controller.session_id, self._end
        def done(runs):
            for run in runs:
                if run['id'] not in self.runs:
                    self._show_run(run)
            self._end = start + len(runs)
            self._show_pages()
        self._read(lambda: self.storage.library.image_runs(session, 50, start), done)

    def _show_pages(self):
        self.older.set_visible(self._offset > 0)
        self.later.set_visible(self._end < self._total)

    def _show_page(self, read, uid):
        """Show in place of the runs shown the page `read` returns, if it holds the run of `uid`."""
        def done(page):
            if not any(uid in (run['id'], run.get('output_id')) for run in page['runs']):
                return
            for view in self.runs.values():
                self.results.remove(view)
            self.runs = {}
            self._offset, self._total = page['message_offset'], page['message_count']
            self._end = self._offset + len(page['runs'])
            for run in page['runs']:
                self._show_run(run)
            self._reveal_run(uid)
        self._read(read, done)

    def show_message(self, uid):
        """Bring the run a search found into view, reading the page it is on if that is not the one shown.

        `uid` is a run's prompt, which is the run's id, or the answer it got.
        """
        if self._reveal_run(uid) or self.request or not self.controller.session_id:
            return
        session = self.controller.session_id
        self._show_page(lambda: self.storage.conversation_page(session, uid), uid)

    def _reveal_run(self, uid):
        """Scroll to the run whose prompt or answer is `uid` if it is shown; whether it is."""
        view = next((view for id, view in self.runs.items() if uid in (id, view.run.get('output_id'))), None)
        if view:
            self._reveal(view)
        return view is not None

    # Hosts, shutdown, and closing.

    def on_host_changed(self):
        self.update_hosts()

    def update_hosts(self):
        self.picker.panel.update_hosts()
        self.picker.refresh()

    def prepare_shutdown(self):
        """Stop work and close dialogs so the application can quit."""
        self.draft.flush()
        self.picker.close()
        self.chat_input.cancel_fetches()
        self.controller.stop()

    def close_session(self, on_done, delete=False):
        self.closing, self._discard_on_close = True, delete
        self._close_callback = on_done
        self.prepare_shutdown()
        self._changed()

    def _finish_close(self):
        if (self.closing and self.request is None and self._close_callback and not self._close_saving
                and not self.chat_input.pending_imports):
            self._close_saving = True
            self.draft.close(discard=self._discard_on_close)
            def complete():
                callback, self._close_callback = self._close_callback, None
                self._disposed = True
                callback()
            self.storage.after_pending_writes(complete)
