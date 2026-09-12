"""Setup, transcript, and recovery controls for model-to-model conversations."""
import copy
import uuid
from types import SimpleNamespace
from gi.repository import Gtk, Adw, GObject, GLib, Pango
from ..drafts import DraftController
from ..model_conversation import ModelConversationController
from ..bubbles import AiBubble
from ..context import describe_context
from ..markdown_view import MarkdownView
from .chat_input import ChatInput
from .model_target import TargetPicker
from .message_list import MessageList


class ModelConversationTab(Gtk.Box):
    __gsignals__ = {'chat-updated': (GObject.SignalFlags.RUN_FIRST, None, (str, str)),
                    'request-changed': (GObject.SignalFlags.RUN_FIRST, None, ())}
    title = GObject.Property(type=str, default='')

    def __init__(self, storage, saved=None, draft=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.storage, self.mode = storage, 'model_conversation'
        self.strategy = SimpleNamespace(chat_id=saved['id'] if saved else None, deleted=False)
        self.title = saved['title'] if saved else _('New Model to Model Conversation')
        self.closing = self._disposed = self._close_saving = False
        self._close_callback = None
        self._discard_on_close = False
        self._initialized_run = False
        self._wide = None
        self._offset = 0
        self._page_loading = False
        self.bubbles, self.attempts, self.targets = {}, [], []
        self._setup_values = None
        self.controller = ModelConversationController(storage, self._changed, self._started, self._chunk, self._finished)
        self.draft = DraftController(storage, self.mode, self.read_draft, id=draft['id'] if draft else None)
        self.draft.restoring = True
        self.setup = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                             margin_start=12, margin_end=12, margin_top=12, margin_bottom=12)
        self.setup_scroll = Gtk.ScrolledWindow(child=self.setup, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.target_grid = Gtk.Grid(column_spacing=12, row_spacing=12, column_homogeneous=True, hexpand=True)
        self.target_grid.add_tick_callback(self._layout)
        self.setup.append(self.target_grid)
        rounds_box = Gtk.Box(spacing=12)
        self.rounds = Gtk.SpinButton.new_with_range(1, 100, 1)
        self.rounds.set_value(5)
        self.rounds.set_numeric(True)
        rounds_box.append(Gtk.Label(label=_('Rounds'), xalign=0, mnemonic_widget=self.rounds))
        rounds_box.append(self.rounds)
        self.setup.append(rounds_box)
        self.setup.append(Gtk.Label(label=_('One round includes one response from each model. Model A starts.'), wrap=True, xalign=0))
        self.setup.append(Gtk.Label(label=_('Opening prompt'), xalign=0))
        self.chat_input = ChatInput()
        self.chat_input.discovery_enabled = False
        self.chat_input.connection_box.set_visible(False)
        self.chat_input.attach_button.get_parent().set_visible(False)
        self.chat_input.send_button.set_tooltip_text(_('Start conversation'))
        self.chat_input.entry.set_placeholder_text(_('Give Model A a topic or task to begin the exchange.'))
        self.chat_input.send_button.connect('clicked', self.start)
        self.chat_input.entry.connect('activate', self.start)
        self.setup.append(self.chat_input)
        self.discard_button = Gtk.Button(label=_('Discard Draft'), halign=Gtk.Align.START)
        self.discard_button.connect('clicked', self.discard_draft)
        self.setup.append(self.discard_button)
        self.summary = Gtk.Expander(label=_('Conversation settings'), visible=False,
                                    margin_start=12, margin_end=12)
        self.notice = Gtk.Label(wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, xalign=0,
                                margin_start=12, margin_end=12, visible=False)
        self.progress = Gtk.Label(wrap=True, xalign=0, margin_start=12, margin_end=12)
        self.message_list = MessageList(visible=False, vexpand=True)
        self.older = Gtk.Button(label=_('Load earlier messages'), visible=False)
        self.older.connect('clicked', self.load_older)
        self.controls = Adw.WrapBox(child_spacing=6, line_spacing=6, margin_start=12, margin_end=12, margin_bottom=8)
        self.pause_button = Gtk.Button(label=_('Pause'))
        self.resume_button = Gtk.Button(label=_('Resume'))
        self.stop_button = Gtk.Button(label=_('Stop'))
        self.again_button = Gtk.Button(label=_('Run Again'))
        for button, callback in ((self.pause_button, lambda *a: self.controller.pause()),
                                  (self.resume_button, self.resume),
                                  (self.stop_button, lambda *a: self.controller.stop()),
                                  (self.again_button, self.run_again)):
            button.connect('clicked', callback)
            self.controls.append(button)
        for child in (self.setup_scroll, self.summary, self.notice, self.progress, self.older, self.message_list, self.controls):
            self.append(child)
        self.chat_input.entry.connect('changed', self._draft_changed)
        self.rounds.connect('value-changed', self._draft_changed)
        if saved:
            self.controller.load(saved)
            self.attempts = copy.deepcopy(saved['attempts'])
            self._offset = saved.get('message_offset', 0)
            self._initialize_run()
            for message in saved['messages']:
                self._render(message)
            self.older.set_visible(self._offset > 0)
        else:
            values = (draft or {}).get('targets') or [None, None]
            for index in range(2):
                picker = TargetPicker(storage, self._draft_changed, values[index], participant=True)
                picker.panel.settings_title = (_('Model A') if index == 0 else _('Model B')) + ' · ' + _('Advanced settings…')
                card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, hexpand=True)
                card.append(Gtk.Label(label=_('Model A') if index == 0 else _('Model B'), xalign=0, css_classes=['title-3']))
                card.append(picker)
                picker.card = card
                self.targets.append(picker)
            self._layout(force=True)
            if draft:
                self.chat_input.entry.restore_draft(draft.get('text', ''))
                self.rounds.set_value(draft.get('settings', {}).get('rounds', 5))
                self.draft.revision = draft.get('revision', 0)
        self.draft.restoring = False
        self._changed()

    @property
    def request(self):
        return self.controller if self.controller.busy else None

    def _layout(self, *args, force=False):
        wide = self.get_width() >= 720
        if wide == self._wide and not force:
            return True
        self._wide = wide
        for target in self.targets:
            if target.card.get_parent():
                self.target_grid.remove(target.card)
        for index, target in enumerate(self.targets):
            self.target_grid.attach(target.card, index if wide else 0, 0 if wide else index, 1, 1)
        return True

    def read_draft(self):
        return dict(text=self.chat_input.entry.read_draft(), settings=dict(rounds=self.rounds.get_value_as_int()),
                    targets=[p.value() for p in self.targets])

    def restore_draft(self, draft):
        # Existing open editors keep more recent unsaved edits.
        if self.controller.id or self.draft.dirty:
            return
        self.draft.restoring = True
        self.chat_input.entry.restore_draft(draft.get('text', ''))
        self.rounds.set_value(draft.get('settings', {}).get('rounds', 5))
        self.draft.restoring = False

    def _draft_changed(self, *args):
        self.draft.changed()
        if hasattr(self, 'pause_button'):
            self._changed()

    def discard_draft(self, *args):
        if self.controller.id or self.controller.busy:
            return
        self.draft.consumed()
        self.storage.delete_draft(self.draft.id)
        self.draft.restoring = True
        self.chat_input.entry.clear_draft()
        self.rounds.set_value(5)
        for target in self.targets:
            target.panel.restore_draft({'fields': dict.fromkeys(target.panel.field_errors, '')})
            target.system_entry.clear_draft()
            target.input.load_thinking_val(None)
        self.draft.restoring = False
        self.emit('chat-updated', '', '')
        self._changed()

    def start(self, *args):
        if self.closing or self.controller.id or self.controller.busy or not self.chat_input.entry.read_draft().strip():
            return
        if len(self.targets) != 2 or any(not p.input.get_selected_model() or p.input.capabilities_loading for p in self.targets):
            return
        try:
            self.rounds.update()
            settings = [p.request_settings() for p in self.targets]
            self._setup_values = copy.deepcopy(self.read_draft())
            self.draft.flush()
            self.controller.start(self._setup_values['text'], self.rounds.get_value_as_int(), settings,
                                  self.draft.id, self.draft.revision, self._setup_values)
        except Exception as exc:
            self.controller.error = str(exc)
            self._changed()

    def _initialize_run(self):
        if self._initialized_run:
            return
        self._initialized_run = True
        self.strategy.chat_id = self.controller.id
        self._setup_values = copy.deepcopy(self.controller.settings.get('draft_settings', {}))
        self.draft.consumed()
        self.draft.closed = True
        self.setup_scroll.set_visible(False)
        self.message_list.set_visible(True)
        self.summary.set_visible(True)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        content.append(Gtk.Label(label=_('Rounds: {0}').format(self.controller.settings['rounds']), xalign=0))
        for index, settings in enumerate(self.controller.settings['participants']):
            label = _('Model A') if index == 0 else _('Model B')
            content.append(Gtk.Label(label=label + ' · ' + settings['model'] + '\n' + settings['host'],
                                     wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, xalign=0, selectable=True))
            content.append(Gtk.Label(label=settings.get('system') or _('No system prompt'), wrap=True,
                                     wrap_mode=Pango.WrapMode.WORD_CHAR, xalign=0, selectable=True))
            import json
            content.append(Gtk.Expander(label=_('Advanced settings…'), child=MarkdownView(
                '```json\n' + json.dumps(settings, indent=2, ensure_ascii=False) + '\n```', wrap_code=True)))
        self.summary.set_child(Gtk.ScrolledWindow(child=content, hscrollbar_policy=Gtk.PolicyType.NEVER,
            min_content_height=80, max_content_height=300, propagate_natural_height=True))

    def _changed(self):
        if self._disposed:
            return
        controller = self.controller
        if controller.run and not self._initialized_run:
            self._initialize_run()
            prompt = self._setup_values.get('text', '')
            self.title = prompt.strip().splitlines()[0][:60] if prompt.strip() else self.title
            self.message_list.add_user_message(prompt)
        self.setup.set_sensitive(not controller.busy and not controller.id and not self.closing)
        self.chat_input.send_button.set_sensitive(bool(self.chat_input.entry.read_draft().strip()) and len(self.targets) == 2
            and all(p.input.get_selected_model() and not p.input.capabilities_loading for p in self.targets))
        status = (controller.run or {}).get('status', '')
        self.notice.set_text(controller.error or '')
        self.notice.set_visible(bool(controller.error))
        self.controls.set_visible(bool(controller.id or controller.busy))
        self.pause_button.set_visible(controller.busy and not controller._end)
        self.pause_button.set_sensitive(not controller._pause)
        self.stop_button.set_visible(controller.busy or status in ('paused', 'failed', 'interrupted'))
        self.stop_button.set_sensitive(not controller.busy or not controller._end)
        self.resume_button.set_visible(not controller.busy and status in ('paused', 'interrupted', 'failed'))
        next_turn = (controller.run or {}).get('next_turn', 0)
        retry = any(a['turn'] == next_turn and a['status'] != 'complete' for a in self.attempts)
        self.resume_button.set_label(_('Retry turn') if retry else _('Resume'))
        self.again_button.set_visible(bool(controller.id) and not controller.busy)
        labels = {'paused': _('Paused'), 'stopped': _('Stopped'), 'complete': _('Complete'),
                  'failed': _('Failed'), 'interrupted': _('Interrupted')}
        text = labels.get(status, '')
        if controller.busy:
            text = _('Preparing conversation…') if not controller.run else _('Round {0} of {1} · {2} responding').format(
                next_turn // 2 + 1, controller.settings['rounds'], _('Model A') if next_turn % 2 == 0 else _('Model B'))
            if controller._pause:
                text = _('Pausing after this response…')
            if controller._end:
                text = _('Stopping…')
        self.progress.set_text(text)
        self.progress.set_visible(bool(text))
        self.emit('request-changed')
        if controller.id:
            self.emit('chat-updated', controller.id, self.title)
        self._finish_close()

    def _render(self, message):
        if message['role'] == 'user':
            self.message_list.add_user_message(message['content'])
            return
        bubble = AiBubble(model_name=message.get('model'))
        participant = message.get('participant', 0)
        settings = self.controller.settings['participants'][participant]
        label = _('Model A') if participant == 0 else _('Model B')
        bubble.header.set_text(_('Round {0} · {1} · Attempt {2}').format(message.get('turn', 0) // 2 + 1, label, message.get('attempt', 1)) + ' · ' + settings['model'])
        bubble.header.set_tooltip_text(settings['host'])
        host_label = Gtk.Label(label=settings['host'], xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR,
                               selectable=True, css_classes=['dim-label', 'caption'])
        bubble.bubble_box.insert_child_after(host_label, bubble.header)
        bubble.set_api_details(message.get('api_details', settings))
        bubble.append_text(message.get('content', ''))
        bubble.append_thinking(message.get('thinking_content', ''))
        metadata = message.get('response_metadata', {})
        if metadata.get('status') != 'running':
            bubble.show_response_metadata(metadata, settings.get('show_stats', True))
        self._context(bubble, metadata)
        self.message_list.add_ai_bubble(bubble)
        self.bubbles[message['uid']] = bubble

    def _context(self, bubble, metadata):
        estimate = metadata.get('context_estimate')
        if estimate and not hasattr(bubble, '_conversation_context'):
            bubble._conversation_context = Gtk.Expander(label=_('Context estimate'), child=Gtk.Label(
                label=describe_context(estimate), wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, xalign=0))
            bubble.bubble_box.append(bubble._conversation_context)
            if estimate['warning']:
                bubble.bubble_box.append(Gtk.Label(label=_('History is approaching the configured context size.'), wrap=True, xalign=0))

    def _started(self, attempt, state):
        if self._disposed:
            return
        self.attempts.append(attempt)
        self._render(dict(self.controller._message(attempt['id'], state), participant=attempt['turn'] % 2,
                          turn=attempt['turn'], attempt=attempt['attempt'], content='', thinking_content='',
                          response_metadata=dict(state.metadata, status='running')))
        if not self.message_list._user_scrolling:
            while True:
                rows = []
                row = self.message_list.list_box.get_first_child()
                while row:
                    rows.append(row)
                    row = row.get_next_sibling()
                if len(rows) <= 50:
                    break
                oldest = rows[0]
                for uid, bubble in list(self.bubbles.items()):
                    if bubble is oldest:
                        bubble.cancel_delivery()
                        del self.bubbles[uid]
                self.message_list.list_box.remove(oldest)
                self._offset += 1
            self.older.set_visible(self._offset > 0)

    def _chunk(self, uid, content, thinking, logprobs):
        bubble = self.bubbles.get(uid)
        if self._disposed or not bubble:
            return
        bubble.append_text(content)
        bubble.append_thinking(thinking)
        if logprobs:
            bubble.append_logprobs(logprobs)

    def _finished(self, uid, state):
        for attempt in self.attempts:
            if attempt['id'] == uid:
                attempt['status'] = state.metadata['status']
        bubble = self.bubbles.get(uid)
        if bubble and not self._disposed:
            # A busy main loop can deliver coalesced chunks before the row is
            # mapped. The saved state is authoritative at completion.
            bubble.full_text = state.content
            bubble.thinking_text = state.thinking
            bubble.thinking_label.set_text(state.thinking)
            bubble.thinking_expander.set_visible(bool(state.thinking))
            bubble.show_response_metadata(state.metadata, state.settings.get('show_stats', True))

    def load_older(self, *args):
        if self._page_loading or not self._offset:
            return
        self._page_loading = True
        offset = max(0, self._offset - 50)
        limit = self._offset - offset
        def work():
            try:
                messages = self.storage.get_messages(self.controller.id, limit, offset, True)
            except Exception as exc:
                GLib.idle_add(failed, str(exc))
                return
            GLib.idle_add(ready, messages)
        def failed(error):
            self._page_loading = False
            self.controller.error = error
            self._changed()
            return False
        def ready(messages):
            self._page_loading = False
            if self._disposed:
                return False
            adj = self.message_list.get_vadjustment()
            old_upper, old_value = adj.get_upper(), adj.get_value()
            self.message_list._insert_index = 0
            self.message_list._user_scrolling = True
            for message in messages:
                self._render(message)
            self.message_list._insert_index = None
            self._offset = offset
            self.older.set_visible(offset > 0)
            GLib.idle_add(lambda: (adj.set_value(old_value + adj.get_upper() - old_upper), False)[1])
            return False
        self.storage.services.control.submit(work)

    def show_message(self, uid):
        bubble = self.bubbles.get(uid)
        if bubble:
            self.message_list._user_scrolling = True
            def scroll():
                valid, bounds = bubble.compute_bounds(self.message_list.list_box)
                if valid:
                    self.message_list.get_vadjustment().set_value(bounds.get_y())
                return False
            GLib.idle_add(scroll)
        elif not self.controller.busy:
            self._read_page(uid, lambda: self.show_message(uid) if uid in self.bubbles else None)

    def _read_page(self, uid, done):
        if self._page_loading:
            return
        self._page_loading = True
        self.resume_button.set_sensitive(False)
        def work():
            try:
                page, error = self.storage.conversation_page(self.controller.id, uid), None
            except Exception as exc:
                page, error = None, str(exc)
            GLib.idle_add(ready, page, error)
        def ready(page, error):
            self._page_loading = False
            self.resume_button.set_sensitive(True)
            if self._disposed or self.closing or getattr(self.get_root(), '_shutting_down', False):
                return False
            if page:
                self.message_list.cancel_deliveries()
                self.message_list.clear()
                self.bubbles.clear()
                self._offset = page['message_offset']
                self.attempts = page['attempts']
                for message in page['messages']:
                    self._render(message)
                self.older.set_visible(self._offset > 0)
                done()
            elif error:
                self.controller.error = error
                self._changed()
            return False
        self.storage.services.control.submit(work)

    def resume(self, *args):
        if not self.controller.busy and not self.closing and not getattr(self.get_root(), '_shutting_down', False):
            self._read_page(None, self.controller.resume)

    def run_again(self, *args):
        if self.controller.busy or not self.controller.id:
            return
        self.get_root()._add_tab(ModelConversationTab(self.storage, draft=dict(copy.deepcopy(self._setup_values),
            id=str(uuid.uuid4()), mode=self.mode, revision=0)))

    def update_hosts(self):
        if not self.controller.id:
            for target in self.targets:
                target.panel.update_hosts()

    def prepare_shutdown(self):
        self.draft.flush()
        for target in self.targets:
            target.close()
        self.controller.stop(interrupted=True)

    def close_session(self, on_done, delete=False):
        self.closing, self._discard_on_close = True, delete
        self._close_callback = on_done
        self.prepare_shutdown()
        self._finish_close()

    def _finish_close(self):
        if self.closing and not self.controller.busy and self._close_callback and not self._close_saving:
            self._close_saving = True
            self.draft.close(discard=self._discard_on_close)
            def saved():
                self._disposed = True
                self.message_list.cancel_deliveries()
                callback, self._close_callback = self._close_callback, None
                callback()
            self.storage._submit(lambda: None, on_done=saved)
