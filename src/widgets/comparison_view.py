"""Comparison editor and adaptive saved results."""
import copy
from types import SimpleNamespace
from gi.repository import Gtk, GObject, GLib, Pango
from ..tab import GenerationTab
from ..comparison import ComparisonRun, ComparisonController, prepare_run
from ..drafts import DraftController
from ..context import describe_context
from ..bubbles import AiBubble
from .chat_input import ChatInput
from .options_panel import OptionsPanel
from .knowledge_view import SourcesView


class TargetPicker(Gtk.Box):
    def __init__(self, storage, changed, target=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.panel = OptionsPanel()
        self.panel.storage = storage
        self.panel.update_hosts()
        self.input = ChatInput()
        self.input.services = storage.services
        self.input.connection_box.get_parent().remove(self.input.connection_box)
        self.input.connection_box.prepend(self.panel.host_row)
        self.input.thinking_dropdown.get_parent().set_visible(False)
        self.append(self.input.connection_box)
        self.input.capability_notice.get_parent().remove(self.input.capability_notice)
        self.append(self.input.capability_notice)
        def refresh(*args):
            host = self.panel.get_selected_host()
            self.input.fetch_models(storage.connection(host) if host else None,
                                    host_id=host['id'] if host else None)
            changed()
        if target:
            for index, host in enumerate(self.panel.host_list):
                if host['id'] == target.get('host_id'):
                    self.panel.host_dropdown.set_selected(index)
            self.input.pending_model_selection = target.get('model')
        self.panel.host_dropdown.connect('notify::selected-item', refresh)
        self.input.model_dropdown.connect('notify::selected', lambda *args: changed())
        self.input.connect('capabilities-changed', lambda *args: changed())
        refresh()

    def value(self):
        host = self.panel.get_selected_host()
        return dict(host_id=host['id'] if host else None, model=self.input.get_selected_model())


class ComparisonTab(Gtk.Box):
    __gsignals__ = {'chat-updated': (GObject.SignalFlags.RUN_FIRST, None, (str, str)),
                    'request-changed': (GObject.SignalFlags.RUN_FIRST, None, ())}
    title = GObject.Property(type=str, default='')

    def __init__(self, storage, saved=None, draft=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.storage, self.mode = storage, 'comparison'
        self.strategy = SimpleNamespace(chat_id=saved['id'] if saved else None, deleted=False)
        self.title = saved['title'] if saved else _('New Comparison')
        self.request = None
        self.closing = self._disposed = False
        self._close_callback = None
        self._close_saving = False
        self.targets, self.results = [], {}
        self._wide = None
        self.editor = GenerationTab(storage=storage, discover_models=False)
        self.editor.draft.close(discard=True)
        self.chat_input, self.options_panel = self.editor.chat_input, self.editor.options_panel
        self.chat_input.cancel_fetches()
        self.chat_input.discovery_enabled = False
        self.chat_input._set_thinking_options(None)
        self.options_panel.set_cloud(False)
        self.knowledge_control = self.editor.knowledge_control
        self._retrieval_dialog, self._tool_views = None, []
        self.knowledge_control.set_visible(True)
        self.editor.message_list.set_visible(False)
        self.editor.context_expander.set_visible(False)
        self.chat_input.connection_box.set_visible(False)
        self.chat_input.send_button.disconnect_by_func(self.editor.on_send_or_stop)
        self.chat_input.entry.disconnect_by_func(self.editor.on_send_clicked)
        self.chat_input.send_button.connect('clicked', self.send_or_stop)
        self.chat_input.entry.connect('activate', self.send_or_stop)
        self.chat_input.connect('attachments-ready', lambda *args: self._update_send())
        self.editor.discard_button.disconnect_by_func(self.editor.discard_draft)
        self.editor.discard_button.connect('clicked', self.discard_draft)
        self.target_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.target_controls = Gtk.Box(spacing=6)
        self.add_target_button = Gtk.Button(label=_('Add Target'))
        self.add_target_button.connect('clicked', lambda *args: self.add_target())
        self.remove_target_button = Gtk.Button(label=_('Remove Target'))
        self.remove_target_button.connect('clicked', self.remove_target)
        for button in (self.add_target_button, self.remove_target_button):
            self.target_controls.append(button)
        self.notice = Gtk.Label(wrap=True, xalign=0, visible=False)
        self.result_selector = Gtk.DropDown(visible=False)
        self.result_selector.connect('notify::selected', lambda *args: self._layout_results(force=True))
        self.result_grid = Gtk.Grid(column_spacing=12, row_spacing=12, column_homogeneous=True, hexpand=True)
        self.result_scroll = Gtk.ScrolledWindow(child=self.result_grid, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.result_scroll.add_tick_callback(self._layout_results)
        self.again_button = Gtk.Button(label=_('Run Again'), visible=bool(saved))
        self.again_button.connect('clicked', self.run_again)
        for child in (self.target_box, self.target_controls, self.notice, self.result_selector,
                      self.result_scroll, self.again_button, self.editor):
            self.append(child)
        self.draft = DraftController(storage, 'comparison', self.read_draft, id=draft['id'] if draft else None)
        self.chat_input.entry.connect('changed', self.draft.changed)
        self.options_panel.watch_draft(self.draft.changed)
        if saved:
            self.target_box.set_visible(False)
            self.target_controls.set_visible(False)
            self.editor.set_visible(False)
            self.saved = saved
            for target in saved['targets']:
                self._add_result(target['id'], target['settings'])
                message = next((m for m in saved['messages'] if m.get('uid') == target['id']), None)
                box, bubble, stop = self.results[target['id']]
                stop.set_visible(False)
                if message:
                    bubble.append_text(message['content'])
                    bubble.append_thinking(message.get('thinking_content', ''))
                    bubble.show_response_metadata(message.get('response_metadata', {}), True)
                    self._sources(box, message.get('response_metadata', {}))
                else:
                    box.append(Gtk.Label(label=target['status'], wrap=True))
        else:
            self.draft.restoring = True
            if draft:
                self.editor.restore_draft(draft)
                self.draft.revision = draft.get('revision', 0)
            for target in (draft or {}).get('targets') or [None, None]:
                self.add_target(target)
            self.draft.restoring = False
        self._update_send()

    def _local_options(self):
        return self.editor._local_options()

    def read_draft(self):
        value = self.editor.read_draft()
        value['targets'] = [picker.value() for picker in self.targets]
        return value

    def restore_draft(self, draft):
        self.editor.restore_draft(draft)

    def discard_draft(self, *args):
        self.draft.consumed()
        self.storage.delete_draft(self.draft.id)
        self.draft.restoring = True
        self.editor.discard_draft()
        self.draft.restoring = False
        self.emit('chat-updated', '', '')

    def add_target(self, target=None):
        if len(self.targets) >= 4:
            return
        picker = TargetPicker(self.storage, self._targets_changed, target)
        self.targets.append(picker)
        self.target_box.append(picker)
        self._targets_changed()

    def remove_target(self, *args):
        if len(self.targets) > 2:
            picker = self.targets.pop()
            picker.input.cancel_fetches()
            self.target_box.remove(picker)
            self._targets_changed()

    def _targets_changed(self):
        self.draft.changed()
        self._update_send()

    def _update_send(self):
        self.add_target_button.set_sensitive(len(self.targets) < 4 and not self.request)
        self.remove_target_button.set_sensitive(len(self.targets) > 2 and not self.request)
        self.chat_input.send_button.set_sensitive(bool(self.request) or
            not self.chat_input.pending_imports and len(self.targets) >= 2
            and all(p.input.get_selected_model() and not p.input.capabilities_loading for p in self.targets))
        self.chat_input.attach_button.set_sensitive(not self.request)
        self.chat_input.capability_notice.set_visible(False)

    def send_or_stop(self, *args):
        if self.request:
            self.request.cancellable.cancel()
            return
        prompt = self.chat_input.entry.read_draft()
        if self.closing or self.chat_input.pending_imports or not prompt.strip():
            return
        if any(p.input.capabilities_loading for p in self.targets):
            return
        try:
            settings = self.options_panel.get_request_settings()
            settings.update(options=self.options_panel.get_options_from_ui(),
                system=self.options_panel.system_prompt_entry.read_draft() or None,
                thinking=self.chat_input.get_thinking_value(), logprobs=self.options_panel.logprobs_check.get_active(),
                top_logprobs=self.options_panel.get_logprobs(), show_stats=self.options_panel.stats_check.get_active(),
                knowledge=copy.deepcopy(self.knowledge_control.options), query_override=self.knowledge_control.query.get_text())
            settings['draft_settings'] = self.editor.read_draft()['settings']
            images = self.chat_input.get_images()
            self.draft.flush()
            revision = self.draft.revision
            targets = [p.value() for p in self.targets]
        except Exception as exc:
            self._error(exc)
            return
        active = self.request = ComparisonRun()
        self.emit('request-changed')
        self.chat_input.set_running(True)
        self.chat_input.send_button.set_tooltip_text(_('Stop All'))
        self.chat_input.entry.set_sensitive(False)
        self.target_box.set_sensitive(False)
        self._update_send()
        def prepare():
            try:
                run = prepare_run(self.storage, active, prompt, images, settings, targets, self.draft.id, revision)
                self.storage._submit(self.storage.library.create_comparison, run, on_done=lambda: self._start(active, run))
            except Exception as exc:
                GLib.idle_add(failed, exc)
        def failed(exc):
            self.request = None
            self.emit('request-changed')
            self.chat_input.set_running(False)
            self.chat_input.entry.set_sensitive(True)
            self.target_box.set_sensitive(True)
            self._update_send()
            if not active.cancellable.is_cancelled():
                self._error(exc)
            self._finish_close()
            return False
        self.storage.services.inference.submit(prepare)

    def _error(self, error):
        self.notice.set_text(str(error))
        self.notice.set_visible(True)

    def _start(self, active, run):
        self.strategy.chat_id = run['id']
        self.title = run['prompt'].splitlines()[0][:60]
        self.draft.consumed()
        self.draft.restoring = True
        self.chat_input.entry.clear_draft()
        self.chat_input.restore_images([])
        self.draft.restoring = False
        self.notice.set_visible(False)
        self.saved_input = self.read_draft()
        self.saved_input.update(text=run['prompt'], images=run['images'])
        self.target_box.set_visible(False)
        self.target_controls.set_visible(False)
        self.options_panel.set_visible(False)
        self.chat_input.entry.set_visible(False)
        self.emit('chat-updated', run['id'], self.title)
        for target in run['targets']:
            self._add_result(target['id'], target['settings'])
        def chunk(id, content, thinking, logprobs):
            if not self._disposed:
                bubble = self.results[id][1]
                bubble.append_text(content)
                bubble.append_thinking(thinking)
                if logprobs:
                    bubble.append_logprobs(logprobs)
        def done(id, state):
            box, bubble, stop = self.results[id]
            stop.set_visible(False)
            bubble.show_response_metadata(state.metadata, state.settings['show_stats'])
            self._sources(box, state.metadata)
            if not active.pending:
                self.request = None
                self.editor.set_visible(False)
                self.again_button.set_visible(True)
                self.emit('request-changed')
                self.emit('chat-updated', run['id'], self.title)
                self._finish_close()
        ComparisonController(self.storage, active).dispatch(chunk, done)

    def _add_result(self, id, settings):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, hexpand=True)
        title = Gtk.Label(label=settings['model'] + '\n' + settings['host'], wrap=True,
                          wrap_mode=Pango.WrapMode.WORD_CHAR, selectable=True)
        box.append(title)
        bubble = AiBubble(model_name=settings['model'], output_format=settings.get('format'), hexpand=True)
        bubble.set_api_details(settings)
        box.append(bubble)
        stop = Gtk.Button(label=_('Stop response'))
        stop.connect('clicked', lambda *args: self.request.states[id].cancellable.cancel() if self.request else None)
        box.append(stop)
        self.results[id] = (box, bubble, stop)
        bubble.model_name = settings['model']
        self.result_selector.set_model(Gtk.StringList.new([r[1].model_name for r in self.results.values()]))
        self._layout_results(force=True)

    def _sources(self, box, metadata):
        if metadata.get('retrieval'):
            box.append(SourcesView(metadata['retrieval']))
        if metadata.get('context_estimate'):
            box.append(Gtk.Expander(label=_('Context estimate'), child=Gtk.Label(
                label=describe_context(metadata['context_estimate']), wrap=True, xalign=0)))

    def _layout_results(self, *args, force=False):
        wide = self.get_width() >= 720
        if wide == self._wide and not force:
            return True
        self._wide = wide
        for box, bubble, stop in self.results.values():
            if box.get_parent():
                self.result_grid.remove(box)
        for index, (box, bubble, stop) in enumerate(self.results.values()):
            if wide or index == self.result_selector.get_selected():
                self.result_grid.attach(box, index % 2 if wide else 0, index // 2 if wide else 0, 1, 1)
        self.result_selector.set_visible(not wide and len(self.results) > 1)
        return True

    def run_again(self, *args):
        root = self.get_root()
        if hasattr(self, 'saved_input'):
            values = copy.deepcopy(self.saved_input)
        else:
            saved = self.saved
            first = saved['messages'][0]
            settings = saved['targets'][0]['settings']
            self.options_panel.load_options(dict(settings.get('options', {}), **{
                k: settings.get(k) for k in ('output_mode', 'schema_text', 'keep_alive', 'tools_text')}))
            self.options_panel.system_prompt_entry.restore_draft(settings.get('system'))
            self.knowledge_control.load(settings.get('knowledge', {}))
            values = self.read_draft()
            values['settings'] = copy.deepcopy(saved['options'].get('draft_settings', values['settings']))
            values.update(text=first['content'], images=first.get('images', []),
                targets=[dict(host_id=t['settings']['host_id'], model=t['settings']['model']) for t in saved['targets']])
        import uuid
        root._add_tab(ComparisonTab(self.storage, draft=dict(values, id=str(uuid.uuid4()), mode='comparison', revision=0)))

    def show_message(self, uid):
        for index, id in enumerate(self.results):
            if id == uid:
                self.result_selector.set_selected(index)

    def on_host_changed(self):
        self.update_hosts()

    def update_hosts(self):
        for picker in self.targets:
            picker.panel.update_hosts()

    def close_session(self, on_done, delete=False):
        self.closing = True
        self._discard_on_close = delete
        self._close_callback = on_done
        for picker in self.targets:
            picker.input.cancel_fetches()
        self.editor.close_session(self._finish_close, delete=True)
        if self.request:
            self.request.cancellable.cancel()
        else:
            self.storage._submit(lambda: None, on_done=self._finish_close)

    def _finish_close(self):
        if (self.closing and self.request is None and self._close_callback
                and not self.chat_input.pending_imports and not self._close_saving):
            self._close_saving = True
            self.draft.close(discard=self._discard_on_close)
            def complete():
                callback, self._close_callback = self._close_callback, None
                self._disposed = True
                callback()
            self.storage._submit(lambda: None, on_done=complete)
