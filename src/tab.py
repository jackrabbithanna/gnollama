from typing import List, Optional, Any, Dict
from gi.repository import Adw, Gtk, GLib, GObject
import copy
from .chat_controller import ChatController, ComposerInput
from .drafts import DraftController
from .providers import ProviderError
from .session import display_chat_title
from .structured import InvalidSchema
from .tool_calling import InvalidTools
from .widgets.tool_view import ToolCallsView
from .widgets.knowledge_view import KnowledgeControl, SourcesView

from .widgets.message_list import MessageList
from .widgets.chat_input import ChatInput
from .widgets.options_panel import OptionsPanel

@Gtk.Template(resource_path='/io/github/jackrabbithanna/Gnollama/tab.ui')
class GenerationTab(Gtk.Box):
    """The view of a chat or generation session; ChatController runs it."""
    __gtype_name__ = 'GenerationTab'
    
    __gsignals__ = {
        'chat-updated': (GObject.SignalFlags.RUN_FIRST, None, (str, str)),
        'request-changed': (GObject.SignalFlags.RUN_FIRST, None, ()),
    }
    title = GObject.Property(type=str, default='')

    message_list: MessageList = Gtk.Template.Child()
    chat_input: ChatInput = Gtk.Template.Child()
    options_panel: OptionsPanel = Gtk.Template.Child()

    def __init__(self, mode: str = 'generate', chat_id: Optional[str] = None,
                 initial_history: Optional[List[Dict[str, Any]]] = None, context=None,
                 draft=None, chat_data=None, discover_models=True, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.init_template()
        self.title = _('New Chat') if mode == 'chat' else _('New Response')
        self._tool_views = []

        self.context = context
        self.storage = storage = context.storage
        self.mode = mode
        self.controller = ChatController(context, self, mode, chat_id, initial_history, chat_data)
        self.chat_input.services = context.services
        self.chat_input.discovery_enabled = discover_models
        self.chat_input.connect('attachments-ready', lambda *args: self.controller.view_ready())

        self.knowledge_control = KnowledgeControl(context, self._persist_tool_options)
        self.knowledge_control.set_visible(mode == 'chat')
        self.insert_child_after(self.knowledge_control, self.message_list)
        self._retrieval_dialog = None
        self.chat_input.has_history_images = self.controller.has_history_images
        
        self.options_panel.tools_available = mode == 'chat'
        self.options_panel.tools_box.set_visible(mode == 'chat')
        self.options_panel.storage = self.storage
        self.chat_input.connection_box.prepend(self.options_panel.host_row)
        self.options_panel.update_hosts()
        
        self.chat_input.send_button.connect('clicked', self.on_send_or_stop)
        self.chat_input.entry.connect('activate', self.on_send_clicked)
        
        self.options_panel.host_dropdown.connect('notify::selected-item', self.on_host_changed)
        self.options_panel.connect('tools-options-changed', self._persist_tool_options)
        self.chat_input.connect('capabilities-changed', self._tool_capabilities_changed)
        
        if mode == 'chat':
            if chat_id:
                chat_data = chat_data or storage.get_chat(chat_id)
                if chat_data:
                    self.load_chat_settings(chat_data)
            
            if initial_history:
                 self.load_initial_history(self.strategy.history)
        self.on_host_changed()
        self._sync_tools()
        self._configured = False
        self._compact_options()
        self.draft = self.controller.draft = DraftController(storage, mode, self.read_draft,
            id=draft['id'] if draft else None, chat_id=chat_id)
        self.chat_input.entry.connect('changed', self.draft.changed)
        self.options_panel.watch_draft(self._settings_edited)
        self.chat_input.thinking_dropdown.connect('notify::selected', self._settings_edited)
        self.options_panel.host_dropdown.connect('notify::selected', self._settings_edited)
        self.chat_input.model_dropdown.connect('notify::selected', self._model_edited)
        self.older_button = Gtk.Button(label=_('Load Older Messages'), visible=self.controller.message_offset > 0)
        self.older_button.connect('clicked', self.load_older)
        self.prepend(self.older_button)
        self.context_label = Gtk.Label(wrap=True, xalign=0, selectable=True)
        self.context_expander = Gtk.Expander(label=_('Context estimate'), child=self.context_label)
        self.context_expander.connect('notify::expanded', lambda *args: self.update_context())
        self.append(self.context_expander)
        self.chat_input.entry.connect('changed', lambda *args: self.update_context())
        self.discard_button = Gtk.Button(label=_('Discard Draft'), halign=Gtk.Align.END)
        self.discard_button.connect('clicked', self.discard_draft)
        self.append(self.discard_button)
        if draft:
            self.restore_draft(draft)
        elif chat_id:
            revision = self.draft.revision
            def fetch():
                saved = storage.get_draft(chat_id)
                GLib.idle_add(lambda: self.restore_draft(saved) if saved and not self.controller.closing and self.draft.revision == revision else None)
            context.services.control.submit(fetch)

    def _compact_options(self):
        self.remove(self.options_panel)
        thinking = self.chat_input.thinking_dropdown.get_parent()
        flow_child = thinking.get_parent()
        flow_child.set_child(None)
        self.chat_input.connection_box.remove(flow_child)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.append(thinking)
        box.append(self.options_panel)
        self.advanced = Gtk.Expander(label=_('Options'), child=box, margin_start=12, margin_end=12)
        self.append(self.advanced)
        def summary(*args):
            active = []
            mode = self.options_panel.output_dropdown.get_selected_item()
            if mode and self.options_panel.output_dropdown.get_selected():
                active.append(mode.get_string())
            if self.options_panel.tools_check.get_active():
                active.append(_('Tool Calling'))
            if self.chat_input.get_thinking_value() is not None:
                active.append(_('Thinking'))
            self.advanced.set_label(_('Options') + (': ' + ', '.join(active) if active else ''))
        self.options_panel.watch_draft(summary)
        self.chat_input.thinking_dropdown.connect('notify::selected', summary)
        summary()

    # The window and tests read these; the controller owns them.
    request = property(lambda self: self.controller.request)
    strategy = property(lambda self: self.controller.strategy)
    closing = property(lambda self: self.controller.closing)

    def update_context(self):
        """Estimate the context the draft would use, while the estimate is shown."""
        if not hasattr(self, 'context_label') or not self.context_expander.get_expanded():
            return
        panel = self.options_panel
        options = {}
        for name in ('num_ctx', 'num_predict'):
            try:
                options[name] = int(getattr(panel, name + '_entry').get_text())
            except ValueError:
                pass
        settings = dict(options=options, system=panel.system_prompt_entry.get_text(),
            tools=panel.tools_text, format=panel.schema_text)
        self.controller.estimate_draft(self.chat_input.entry.read_draft(), settings)

    def context_estimated(self, estimate, expand):
        from .context import describe_context
        if not hasattr(self, 'context_label'):
            return
        self.context_label.set_text(describe_context(estimate))
        if expand and estimate['warning']:
            self.context_expander.set_expanded(True)

    def load_older(self, *args):
        if self.controller.load_older():
            self.older_button.set_sensitive(False)

    def older_loaded(self, messages):
        self.message_list.prepend_page(lambda: self.load_initial_history(messages))
        self.older_button.set_visible(self.controller.message_offset > 0)
        self.older_button.set_sensitive(True)

    def show_message(self, uid):
        self.message_list.scroll_to_message(uid)

    def _model_edited(self, *args):
        if self.chat_input.get_selected_model() and hasattr(self, 'draft'):
            # Model discovery is not an explicit settings edit.
            if self.draft.dirty:
                self.draft.changed()

    def _settings_edited(self, *args):
        if hasattr(self, 'draft') and not self.draft.restoring and not getattr(self.chat_input, '_setting_thinking', False):
            self._configured = True
            self.draft.changed()

    def read_draft(self):
        host = self.options_panel.get_selected_host()
        return dict(text=self.chat_input.entry.read_draft(), images=self.chat_input.get_images(),
            settings=dict(panel=self.options_panel.snapshot_draft(), host_id=host['id'] if host else None,
                model=self.chat_input.get_selected_model(), thinking=self.chat_input.get_thinking_value(),
                knowledge=copy.deepcopy(self.knowledge_control.options), query_override=self.knowledge_control.query.get_text(),
                _configured=self._configured))

    def restore_draft(self, draft):
        self.draft.restoring = True
        try:
            self.draft.revision = draft.get('revision', 0)
            settings = draft.get('settings', {})
            self._configured = settings.get('_configured', False)
            self.options_panel.restore_draft(settings.get('panel', {}))
            self.knowledge_control.load(settings.get('knowledge', {}))
            self.knowledge_control.query.set_text(settings.get('query_override', ''))
            for index, host in enumerate(self.options_panel.host_list):
                if host['id'] == settings.get('host_id'):
                    self.options_panel.host_dropdown.set_selected(index)
            self.chat_input.pending_model_selection = settings.get('model')
            self.chat_input.load_thinking_val(settings.get('thinking'))
            self.on_host_changed()
            self.chat_input.entry.restore_draft(draft.get('text', ''))
            self.chat_input.restore_images(draft.get('images', []))
        finally:
            self.draft.restoring = False

    def discard_draft(self, *args):
        self.draft.consumed()
        self.draft.restoring = True
        try:
            self.chat_input.entry.clear_draft()
            self.chat_input.restore_images([])
            self._configured = False
        finally:
            self.draft.restoring = False
        self.storage.delete_draft(self.draft.id, on_done=lambda: self.emit('chat-updated', '', ''))

    def load_chat_settings(self, chat_data: Dict[str, Any]) -> None:
        self.title = display_chat_title(chat_data.get('title', _('Chat')))
        if 'options' in chat_data:
            options = chat_data['options']
            self.options_panel.load_options(options)
            self.knowledge_control.load(options.get('knowledge', {}))
            
            if 'thinking_val' in options:
                self.chat_input.load_thinking_val(options['thinking_val'])
        
        if 'system' in chat_data and chat_data['system']:
            self.options_panel.system_prompt_entry.set_text(chat_data['system'])
            
        if 'host' in chat_data:
            host_id = chat_data['host']
            for i, h in enumerate(self.options_panel.host_list):
                if h['id'] == host_id:
                    self.options_panel.host_dropdown.set_selected(i)
                    break

        if 'model' in chat_data:
            self.chat_input.pending_model_selection = chat_data['model']
            GLib.idle_add(self.chat_input.select_model, chat_data['model'])

    def load_initial_history(self, history: List[Dict[str, Any]]) -> None:
        for msg in history:
            role = msg.get('role')
            content = msg.get('content', '')
            if role == 'user':
                images = msg.get('images')
                self.message_list.add_user_message(content, images=images)
            elif role == 'assistant':
                from .bubbles import AiBubble
                bubble = AiBubble(model_name=msg.get('model', ''), output_format=(msg.get('api_details') or {}).get('format'))
                if 'thinking_content' in msg:
                    bubble.append_thinking(msg['thinking_content'])
                bubble.append_text(content)
                if 'api_details' in msg:
                    bubble.set_api_details(msg['api_details'])
                bubble.show_response_metadata(msg.get('response_metadata', {}),
                                              self.options_panel.stats_check.get_active())
                self.message_list.add_ai_bubble(bubble)
                self._add_tool_view(msg, bubble)
                self._add_sources(msg.get('response_metadata', {}), bubble)
            elif role == 'system':
                self.message_list.add_system_message(content)
            if role in ('user', 'assistant', 'system'):
                self.message_list.tag_last(msg.get('uid'))

    def on_host_changed(self, *args):
        if not self.chat_input.discovery_enabled:
            return
        host = self.options_panel.get_selected_host()
        self.options_panel.set_capabilities(self.context.capabilities(host))
        self.chat_input.fetch_models(self.context.connection(host) if host else None,
                                     host_id=host['id'] if host else None)

    def update_hosts(self):
        self.options_panel.update_hosts()
        self.on_host_changed()

    def on_send_or_stop(self, *args):
        if self.controller.request:
            self.controller.stop()
            self.chat_input.send_button.set_sensitive(False)
        else:
            self.on_send_clicked()

    def on_send_clicked(self, *args, continuation=False, without_knowledge=False):
        if (self.chat_input.pending_imports or self.chat_input.capabilities_loading
                or not self.controller.can_send(continuation)):
            return
        prompt = '' if continuation else self.chat_input.entry.read_draft()
        if not prompt.strip() and not continuation:
            return
        try:
            host = self.options_panel.get_selected_host()
            model = self.chat_input.get_selected_model()
            self.controller.check_target(host, model)
            self.controller.send(self._read_input(prompt, host, model, continuation),
                                 continuation=continuation, without_knowledge=without_knowledge)
        except InvalidTools as exc:
            self.options_panel.edit_tools(error=str(exc))
        except InvalidSchema as exc:
            self.options_panel.edit_schema(error=str(exc))
        except (ValueError, OSError, RecursionError, GLib.Error, ProviderError) as exc:
            self.message_list.add_system_message(str(exc))

    def _read_input(self, prompt, host, model, continuation):
        panel = self.options_panel
        options = panel.get_options_from_ui()
        request = panel.get_request_settings()
        images = () if continuation else tuple(self.chat_input.get_images())
        return ComposerInput(
            prompt=prompt, images=images, host=host, model=model,
            thinking=self.chat_input.get_thinking_value(), options=options, request=request,
            system=panel.system_prompt_entry.get_text().strip() or None,
            logprobs=panel.logprobs_check.get_active(), top_logprobs=panel.get_logprobs(),
            show_stats=panel.stats_check.get_active(),
            knowledge=copy.deepcopy(self.knowledge_control.options),
            query_override=self.knowledge_control.query.get_text().strip(),
            image_support=self.chat_input.image_support,
            capabilities_loading=self.chat_input.capabilities_loading)

    # --- What the controller reports ---

    def request_changed(self):
        self.emit('request-changed')
        self.chat_input.set_running(self.controller.request is not None)

    def retrieval_started(self):
        self.chat_input.entry.set_sensitive(False)
        self.knowledge_control.set_sensitive(False)
        self.knowledge_control.notice.set_text(_('Retrieving sources…'))

    def retrieval_ended(self):
        self.knowledge_control.set_sensitive(True)
        self.knowledge_control.refresh_notice()
        self.chat_input.entry.set_sensitive(True)

    def retrieval_failed(self, error):
        dialog = Adw.AlertDialog(heading=_('Could Not Retrieve Sources'), body=str(error))
        dialog.add_response('adjust', _('Adjust Sources'))
        dialog.add_response('retry', _('Retry'))
        dialog.add_response('without', _('Send Without Knowledge'))
        dialog.add_response('cancel', _('Keep Draft'))
        dialog.set_default_response('cancel')
        self._retrieval_dialog = dialog
        def response(d, choice):
            self._retrieval_dialog = None
            if self.controller.closing or self.controller.disposed:
                return
            if choice == 'adjust':
                self.knowledge_control.open_picker()
            elif choice == 'retry':
                self.on_send_clicked()
            elif choice == 'without':
                self.on_send_clicked(without_knowledge=True)
        dialog.connect('response', response)
        dialog.present(self.get_root())

    def history_loading(self, active):
        self.chat_input.entry.set_sensitive(not active)

    def generation_started(self, state):
        if not state.continuation:
            self.draft.restoring = True
            self.chat_input.entry.clear_draft()
            self.chat_input.on_clear_image_clicked(None)
            self.draft.restoring = False
            self.knowledge_control.query.set_text('')
            self.message_list.add_user_message(state.prompt, images=state.images)
        from .bubbles import AiBubble
        bubble = AiBubble(model_name=state.settings['model'], output_format=state.settings.get('format'))
        bubble.set_api_details(state.api_details())
        self.message_list.add_ai_bubble(bubble)
        state.presentation = bubble
        self._sync_tools()

    def generation_prepared(self, state):
        self._add_sources(state.metadata, state.presentation)
        self.chat_input.has_history_images = self.controller.has_history_images
        self.chat_input.update_capability_controls()

    def _add_sources(self, metadata, bubble):
        if metadata.get('retrieval'):
            bubble.bubble_box.append(SourcesView(metadata['retrieval']))

    def chunk(self, state, content, thinking, logprobs):
        bubble = state.presentation
        if content:
            bubble.append_text(content)
        if thinking:
            bubble.append_thinking(thinking)
        if logprobs:
            bubble.append_logprobs(logprobs)

    def request_finished(self, state):
        state.presentation.show_response_metadata(state.metadata, state.settings['show_stats'])

    def message_saved(self, state):
        self._add_tool_view(state.saved_message, state.presentation)

    def tools_changed(self):
        self._sync_tools()

    def title_changed(self, chat_id, title):
        self.title = display_chat_title(title)
        self.emit('chat-updated', chat_id, title)

    def options_saved(self):
        self.emit('chat-updated', self.controller.chat_id, self.title)

    def attachments_pending(self):
        return bool(self.chat_input.pending_imports)

    def _tool_capabilities_changed(self, *args):
        self.options_panel.tool_support = self.chat_input.tool_support
        self.options_panel.tools_loading = self.chat_input.capabilities_loading
        self.options_panel.update_tools_notice()
        self._sync_tools()

    def _persist_tool_options(self, *args):
        self._settings_edited()
        self.controller.persist_options()

    def local_options(self):
        return dict(self.options_panel.get_tools_options(), knowledge=copy.deepcopy(self.knowledge_control.options))

    def _add_tool_view(self, message, bubble):
        if not message or bubble is None or not (message.get('response_metadata') or {}).get('tool_round'):
            return
        view = ToolCallsView(message, self.controller.save_tool_result,
                             lambda: self.on_send_clicked(continuation=True), self.controller.cancel_tool_round)
        self._tool_views.append(view)
        bubble.bubble_box.append(view)

    def _sync_tools(self):
        pending = self.controller.pending_round
        can_continue = bool(self.chat_input.get_selected_model()) and not self.chat_input.capabilities_loading
        for view in self._tool_views:
            view.update(self.controller.tools_editable and view.message is pending, can_continue=can_continue)
        self.chat_input.awaiting_tools = pending is not None or self.controller.tool_busy
        self.chat_input.update_capability_controls()

    def _close_dialogs(self):
        self.chat_input.cancel_fetches()
        if self._retrieval_dialog:
            self._retrieval_dialog.close()
        for view in self._tool_views:
            view.close_editor()
        self.options_panel.close_dialogs()

    def prepare_shutdown(self):
        """Stop work and close dialogs so the application can quit."""
        self.draft.flush()
        self.knowledge_control.close_dialog()
        self._close_dialogs()
        self.controller.stop()

    def close_session(self, on_done, delete=False):
        # Closing is recorded first, so a dialog that saves as it closes is ignored.
        self.controller.close(on_done, delete=delete)
        self.knowledge_control.close_dialog(dispose=True)
        self._close_dialogs()
        self.set_sensitive(False)
