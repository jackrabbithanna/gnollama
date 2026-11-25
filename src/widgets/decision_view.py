"""A decision session: a state, the questions asked about it, and the answers of each run."""
from gettext import gettext as _
from types import SimpleNamespace
from gi.repository import Gtk, GObject, GLib
from ..decision import box_size_hint, check_request
from ..decision_run import DecisionController, prepare
from ..drafts import DraftController
from ..records import DecisionSettings, ModelTarget
from .chat_input import ChatInput
from .decision_answers import RunView
from .decision_form import QuestionForm
from .decision_images import in_formats, load
from .json_view import buffer_text, code_view, editor_frame
from .model_target import TargetPicker


class DecisionTab(Gtk.Box):
    __gsignals__ = {'chat-updated': (GObject.SignalFlags.RUN_FIRST, None, (str, str)),
                    'request-changed': (GObject.SignalFlags.RUN_FIRST, None, ())}
    title = GObject.Property(type=str, default='')

    def __init__(self, context, saved=None, draft=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.context, self.mode = context, 'decision'
        self.storage = context.storage
        self.strategy = SimpleNamespace(chat_id=saved['id'] if saved else None, deleted=False)
        self.title = saved['title'] if saved else _('New Decision')
        self.closing = self._disposed = self._close_saving = False
        self._close_callback = None
        self._discard_on_close = False
        self._page_loading = False
        # The runs shown are one unbroken stretch of the session's `_total`: from `_offset` up to `_end`.
        self._offset = self._end = self._total = 0
        self.runs = {}
        # The host and model the draft knows of. A list that arrives selects a model, which is no edit.
        self._target = None
        # The size of each attached image a box question may be asked about; () until it is read, or if it cannot be.
        self._sizes = {}
        self.controller = DecisionController(context, self._changed, self._started, self._finished, in_formats)
        if saved:
            self.controller.load(saved)
        self.draft = DraftController(context.storage, self.mode, self.read_draft, id=draft['id'] if draft else None,
                                     chat_id=saved['id'] if saved else None)
        self.draft.restoring = True
        # What the editor holds when it has no draft: a session's last run, or nothing.
        last = (saved.get('last_run') or (saved['runs'][-1] if saved['runs'] else {})) if saved else {}
        self._saved = (DecisionSettings.from_stored(saved['options']) if saved else DecisionSettings(),
                       last.get('state', ''), last.get('images') or [])
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                          margin_start=12, margin_end=12, margin_top=12, margin_bottom=12)
        self.scroll = Gtk.ScrolledWindow(child=content, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.append(self.scroll)
        self.picker = TargetPicker(context, self._target_changed, kind='decision')
        content.append(self.picker)

        state_head = Gtk.Box(spacing=6)
        state_head.append(Gtk.Label(label=_('State'), xalign=0, hexpand=True, css_classes=['heading']))
        self.text_toggle = Gtk.ToggleButton(label=_('Text'), active=True)
        self.json_toggle = Gtk.ToggleButton(label=_('JSON'), group=self.text_toggle,
                                            tooltip_text=_('Send the state as the JSON it holds, not as text.'))
        modes = Gtk.Box(css_classes=['linked'])
        modes.append(self.text_toggle)
        modes.append(self.json_toggle)
        state_head.append(modes)
        content.append(state_head)
        # The state as text. Its host and model controls are the picker's, and Run is this tab's own.
        self.chat_input = ChatInput()
        self.chat_input.services = context.services
        self.chat_input.discovery_enabled = False
        self.chat_input.connection_box.set_visible(False)
        self.chat_input.send_button.set_visible(False)
        self.chat_input.entry.send_on_enter = False
        self.chat_input.entry.set_placeholder_text(_('Enter what the questions are asked about.'))
        # Its own margins would set the state in from the rest of the tab.
        inner = self.chat_input.entry.get_parent().get_parent()
        for side in ('start', 'end', 'top', 'bottom'):
            inner.set_property('margin-' + side, 0)
        content.append(self.chat_input)
        # The state as JSON; whichever of the two shows holds the text.
        self.json_view = code_view(editable=True)
        self.json_view.update_property([Gtk.AccessibleProperty.LABEL], [_('State')])
        self.json_frame = editor_frame(self.json_view, min_content_height=80, max_content_height=240,
                                       propagate_natural_height=True, visible=False)
        content.append(self.json_frame)
        # Images go with the state however it is written, so their controls sit below both editors.
        self.attachments = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        for widget in (self.chat_input.image_preview_scrolled, self.chat_input.attach_button.get_parent(),
                       self.chat_input.capability_notice):
            widget.get_parent().remove(widget)
            for side in ('start', 'end', 'bottom'):
                widget.set_property('margin-' + side, 0)
            self.attachments.append(widget)
        content.append(self.attachments)
        # Advice on the attached images, which blocks nothing.
        self.hint = Gtk.Label(wrap=True, xalign=0, visible=False, css_classes=['dim-label'])
        content.append(self.hint)

        self.form = QuestionForm(self._edited)
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
        self.json_view.get_buffer().connect('changed', self._edited)
        self.json_toggle.connect('toggled', self._mode_toggled)
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

    def _show_capabilities(self):
        """Offer images and box questions as far as the selected model takes them.

        Images already attached and questions that already ask for a box stay; `_problem` says what holds them back.
        """
        capabilities = self.picker.decision_capabilities() if self.picker.input.get_selected_model() else None
        refusal = check_request((), ('image',), capabilities) if capabilities else ''
        self.chat_input.set_image_support(capabilities is not None and not refusal, refusal)
        self.form.offer_boxes(bool(capabilities and capabilities.decision_boxes))

    def images(self):
        """The images sent beside the state, as base64."""
        return self.chat_input.get_images()

    def _show_hint(self):
        """Advise on attached images that some models refuse a box question about; read their sizes once for it."""
        images = self.images() if self.form.asks_box() else []
        asked = self._sizes
        self._sizes = {image: asked.get(image, ()) for image in images}
        for image in images:
            if image not in asked:
                load(image, lambda seen, image=image: self._measured(image, seen), self.context.services.control.submit)
        text = box_size_hint(bool(images), [size for size in self._sizes.values() if size])
        self.hint.set_text(text)
        self.hint.set_visible(bool(text))

    def _measured(self, image, seen):
        if seen and not self._disposed and image in self._sizes and not self._sizes[image]:
            self._sizes[image] = (seen.width, seen.height)
            self._show_hint()

    def _mode(self):
        return 'json' if self.json_toggle.get_active() else 'text'

    def state_text(self):
        return buffer_text(self.json_view) if self._mode() == 'json' else self.chat_input.entry.read_draft()

    def _set_state(self, text, mode):
        restoring, self.draft.restoring = self.draft.restoring, True
        self.json_toggle.set_active(mode == 'json')
        self.text_toggle.set_active(mode != 'json')
        self.chat_input.set_visible(mode != 'json')
        self.json_frame.set_visible(mode == 'json')
        self.chat_input.entry.restore_draft('' if mode == 'json' else text)
        self.json_view.get_buffer().set_text(text if mode == 'json' else '')
        self.draft.restoring = restoring

    def _mode_toggled(self, *args):
        if self.draft.restoring:
            return
        # The text moves to the editor now shown.
        mode = self._mode()
        text = self.chat_input.entry.read_draft() if mode == 'json' else buffer_text(self.json_view)
        self._set_state(text, mode)
        self._edited()

    def settings(self):
        """What the editor asks, and of which model."""
        target = self.picker.value()
        return DecisionSettings(ModelTarget(target['host_id'], model=target['model']), self.form.value(), self._mode())

    def _load(self, settings, text, images=()):
        restoring, self.draft.restoring = self.draft.restoring, True
        self.form.load(settings.questions)
        self._set_state(text, settings.state_mode)
        self.chat_input.restore_images(images)
        if settings.target is not None:
            self._target = dict(host_id=settings.target.host_id, model=settings.target.model)
            self.picker.select(self._target)
        self.draft.restoring = restoring

    def read_draft(self):
        settings = self.settings().to_stored()
        settings['questions'] = self.form.draft()
        return dict(text=self.state_text(), images=self.images(), settings=settings)

    def _apply_draft(self, draft):
        self._load(DecisionSettings.from_stored(draft.get('settings')), draft.get('text', ''), draft.get('images') or [])
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
        """Why the editor's request cannot be sent, or ''. The form shows its own errors, and the picker its host's."""
        if self.form.error or not self.picker.input.get_selected_model():
            return ''
        try:
            prepare(self.context, self.picker.panel.get_selected_host(), self.settings(), self.state_text(),
                    self.images(), self.picker.input.selected_details())
        except ValueError as exc:
            return str(exc)
        return ''

    def _ready(self):
        return bool(not self.closing and not self.form.error and self.picker.input.get_selected_model()
                    and not self.chat_input.pending_imports and (self.state_text().strip() or self.images()))

    def run_or_stop(self, *args):
        if self.request:
            self.controller.stop()
        else:
            self.run()

    def run(self, *args):
        if self.request or not self._ready() or self._problem():
            return
        try:
            settings, text, images = self.settings(), self.state_text(), self.images()
            self.draft.flush()
            request = self.controller.start(self.picker.panel.get_selected_host(), settings, text, images,
                                            self.picker.input.selected_details(), self.draft.id, self.draft.revision)
        except ValueError as exc:
            self.controller.error = str(exc)
            self._changed()
            return
        if request:
            self._saved = (request.settings, text, images)

    def _changed(self):
        if self._disposed:
            return
        busy = self.request is not None
        problem = '' if busy else self._problem()
        text = problem or self.controller.error
        self.notice.set_text(text)
        self.notice.set_visible(bool(text))
        self.run_button.set_label(_('Stop') if busy else _('Run'))
        self.run_button.set_sensitive(busy or (self._ready() and not problem))
        for widget in (self.picker, self.chat_input, self.json_frame, self.attachments, self.form, self.text_toggle,
                       self.json_toggle):
            widget.set_sensitive(not busy and not self.closing)
        self._show_hint()
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
        view = RunView(run, self.context.services.control.submit)
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
        self._read(lambda: self.storage.library.decision_runs(session, limit, offset), done)

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
        self._read(lambda: self.storage.library.decision_runs(session, 50, start), done)

    def _show_pages(self):
        self.older.set_visible(self._offset > 0)
        self.later.set_visible(self._end < self._total)

    def _show_page(self, read, uid):
        """Show in place of the runs shown the page `read` returns, if it holds the run of `uid`."""
        def done(page):
            if not any(run['id'] == uid for run in page['runs']):
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
        """Bring the run a search found into view, reading the page it is on if that is not the one shown."""
        if self._reveal_run(uid) or self.request or not self.controller.session_id:
            return
        session = self.controller.session_id
        self._show_page(lambda: self.storage.conversation_page(session, uid), uid)

    def _reveal_run(self, uid):
        """Scroll to the run of `uid` if it is shown; whether it is."""
        view = self.runs.get(uid)
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
        self.form.close_dialog()
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
