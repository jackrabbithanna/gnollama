from typing import List, Optional, Any, Dict, Union
from gi.repository import Adw, Gtk, Gio, GLib, GObject
import copy
import base64
from .session import RequestState, worker
from . import ollama
from .storage import ChatStorage
from .session import GenerationStrategy, ChatStrategy, display_chat_title
from .structured import InvalidSchema, validate_response
from .tool_calling import InvalidTools, inspect_calls
from .widgets.tool_view import ToolCallsView
from .widgets.knowledge_view import KnowledgeControl, SourcesView
from .knowledge import validate_rag

from .widgets.message_list import MessageList
from .widgets.chat_input import ChatInput
from .widgets.options_panel import OptionsPanel

@Gtk.Template(resource_path='/io/github/jackrabbithanna/Gnollama/tab.ui')
class GenerationTab(Gtk.Box):
    """The main widget for a chat or generation session."""
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
                 initial_history: Optional[List[Dict[str, Any]]] = None, storage: Optional[ChatStorage] = None,
                 draft=None, chat_data=None, discover_models=True, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.init_template()
        self.title = _('New Chat') if mode == 'chat' else _('New Response')
        self.request = None
        self.closing = False
        self._close_callback = None
        self._disposed = False
        self._close_saving = False
        self._tool_busy = False
        self._tool_views = []
        
        if not storage:
            storage = ChatStorage()
        self.storage = storage
        self.chat_input.services = storage.services
        self.chat_input.discovery_enabled = discover_models
        self.chat_input.connect('attachments-ready', lambda *args: self._finish_close())

        if mode == 'chat':
            self.strategy = ChatStrategy(storage, chat_id=chat_id, initial_history=initial_history)
        else:
            self.strategy = GenerationStrategy()
            
        self.mode = mode
        self._paged = bool(chat_data and chat_data.get('paged'))
        self._message_offset = (chat_data or {}).get('message_offset', 0)
        self._message_count = (chat_data or {}).get('message_count', len(initial_history or []))
        self.knowledge_control = KnowledgeControl(storage, self._persist_tool_options)
        self.knowledge_control.set_visible(mode == 'chat')
        self.insert_child_after(self.knowledge_control, self.message_list)
        self._retrieval_dialog = None
        self.chat_input.has_history_images = any(m.get('images') for m in getattr(self.strategy, 'history', []))
        
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
        from .drafts import DraftController
        self._configured = False
        self._compact_options()
        self.draft = DraftController(storage, mode, self.read_draft,
            id=draft['id'] if draft else None, chat_id=chat_id)
        self.chat_input.entry.connect('changed', self.draft.changed)
        self.options_panel.watch_draft(self._settings_edited)
        self.chat_input.thinking_dropdown.connect('notify::selected', self._settings_edited)
        self.options_panel.host_dropdown.connect('notify::selected', self._settings_edited)
        self.chat_input.model_dropdown.connect('notify::selected', self._model_edited)
        self.older_button = Gtk.Button(label=_('Load Older Messages'), visible=self._message_offset > 0)
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
                GLib.idle_add(lambda: self.restore_draft(saved) if saved and not self.closing and self.draft.revision == revision else None)
            storage.services.control.submit(fetch)

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

    def update_context(self, state=None):
        if not hasattr(self, 'context_label'):
            return
        from .context import estimate_context, describe_context
        if state:
            self._context_generation = getattr(self, '_context_generation', 0) + 1
            messages = getattr(self.strategy, 'history', [dict(role='user', content=state.prompt, images=state.images)])
            estimate = estimate_context(messages, state.settings, state.retrieval)
            state.metadata['context_estimate'] = estimate
        else:
            if not self.context_expander.get_expanded():
                return
            panel = self.options_panel
            options = {}
            for name in ('num_ctx', 'num_predict'):
                try:
                    options[name] = int(getattr(panel, name + '_entry').get_text())
                except ValueError:
                    pass
            messages = list(getattr(self.strategy, 'history', [])) + [dict(role='user', content=self.chat_input.entry.read_draft())]
            settings = dict(options=options, system=panel.system_prompt_entry.get_text(),
                tools=panel.tools_text, format=panel.schema_text)
            if self._paged:
                self._context_generation = getattr(self, '_context_generation', 0) + 1
                generation = self._context_generation
                prompt = messages[-1]
                def read():
                    history = self.storage.get_messages(self.strategy.chat_id)
                    estimate = estimate_context(history + [prompt], settings)
                    def deliver():
                        if generation == self._context_generation and not self.closing:
                            self.context_label.set_text(describe_context(estimate))
                        return False
                    GLib.idle_add(deliver)
                self.storage.services.control.submit(read)
                return
            estimate = estimate_context(messages, settings)
        self.context_label.set_text(describe_context(estimate))
        if estimate['warning']:
            self.context_expander.set_expanded(True)

    def load_older(self, *args):
        if self._message_offset <= 0:
            return
        self.older_button.set_sensitive(False)
        offset = max(0, self._message_offset - 50)
        count = self._message_offset - offset
        def read():
            messages = self.storage.get_messages(self.strategy.chat_id, count, offset, True)
            GLib.idle_add(deliver, messages)
        def deliver(messages):
            if self.closing:
                return False
            adjustment = self.message_list.get_vadjustment()
            previous, value = adjustment.get_upper(), adjustment.get_value()
            self.message_list._user_scrolling = True
            self.message_list._insert_index = 0
            self.load_initial_history(messages)
            self.message_list._insert_index = None
            self._message_offset = offset
            self.older_button.set_visible(offset > 0)
            self.older_button.set_sensitive(True)
            def anchor():
                adjustment.set_value(value + adjustment.get_upper() - previous)
                return False
            GLib.timeout_add(50, anchor)
            return False
        self.storage.services.control.submit(read)

    def show_message(self, uid):
        def focus():
            row = self.message_list.list_box.get_first_child()
            while row:
                if getattr(row, 'message_uid', None) == uid:
                    self.message_list._user_scrolling = True
                    row.set_focusable(True)
                    row.grab_focus()
                    break
                row = row.get_next_sibling()
            return False
        GLib.timeout_add(100, focus)

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
                self.message_list._last_row.message_uid = msg.get('uid')

    def on_host_changed(self, *args):
        if not self.chat_input.discovery_enabled:
            return
        host = self.options_panel.get_selected_host()
        self.options_panel.set_cloud(ollama.is_cloud(host))
        self.chat_input.fetch_models(self.storage.connection(host) if host else None,
                                     host_id=host['id'] if host else None)

    def update_hosts(self):
        self.options_panel.update_hosts()
        self.on_host_changed()

    def on_send_or_stop(self, *args):
        if self.request:
            self.request.cancellable.cancel()
            self.chat_input.send_button.set_sensitive(False)
        else:
            self.on_send_clicked()

    def on_send_clicked(self, *args, continuation=False, without_knowledge=False):
        if (self.request or self.closing or self._disposed or self._tool_busy
                or self.chat_input.pending_imports or self.chat_input.capabilities_loading):
            return
        pending = self.strategy.pending_round if isinstance(self.strategy, ChatStrategy) else None
        if pending is not None and not continuation:
            return
        if continuation and (pending is None or any(r is None for r in pending['response_metadata']['tool_round']['results'])):
            return
        prompt = '' if continuation else self.chat_input.entry.read_draft()
        if not prompt.strip() and not continuation:
            return
        try:
            host = self.options_panel.get_selected_host()
            if not host:
                raise ValueError(_('No host configured.'))
            hostname = ollama.validate_host(host['hostname'])
            model = self.chat_input.get_selected_model()
            if not model:
                raise ValueError(_('Select an available model before sending.'))
            from .services import model_key
            unloading_models = self.storage.services.models.reserved
            if model_key(hostname, model) in unloading_models:
                raise ValueError(_('Wait for this model to finish unloading before sending.'))
            options = self.options_panel.get_options_from_ui()
            request_settings = self.options_panel.get_request_settings()
            draft_images = [] if continuation else self.chat_input.get_images()
            if self.chat_input.capabilities_loading and (draft_images or self.chat_input.has_history_images):
                raise ValueError(_('Wait for image support to be checked before sending.'))
            if self.chat_input.image_support is False and draft_images:
                raise ValueError(_('Remove draft images or select a vision model to send.'))
            logprobs = self.options_panel.logprobs_check.get_active()
            top = self.options_panel.get_logprobs()
            images = draft_images
            settings = dict(host=hostname, host_id=host['id'], model=model,
                            options=options, thinking=self.chat_input.get_thinking_value(),
                            system=self.options_panel.system_prompt_entry.get_text().strip() or None,
                            logprobs=logprobs, top_logprobs=top,
                            show_stats=self.options_panel.stats_check.get_active(), endpoint=self.mode)
            settings.update(request_settings)
            settings['knowledge'] = copy.deepcopy(self.knowledge_control.options)
            settings['query_override'] = self.knowledge_control.query.get_text().strip()
            retrieve = (self.mode == 'chat' and settings['knowledge']['enabled'] and
                        not continuation and not without_knowledge)
            if retrieve:
                validate_rag(settings['knowledge'])
            settings['history_images_omitted'] = self.chat_input.image_support is False and self.chat_input.has_history_images
        except InvalidTools as exc:
            self.options_panel.edit_tools(error=str(exc))
            return
        except InvalidSchema as exc:
            self.options_panel.edit_schema(error=str(exc))
            return
        except (ValueError, OSError, RecursionError, GLib.Error, ollama.OllamaError) as exc:
            self.message_list.add_system_message(str(exc))
            return

        state = RequestState(settings, prompt, images, continuation=continuation,
                             connection=self.storage.connection(host))
        if not continuation:
            self.draft.flush()
            state.draft_id, state.draft_revision = self.draft.id, self.draft.revision
        if continuation:
            self.strategy.commit_results()
        self.request = state
        self.emit('request-changed')
        self.chat_input.set_running(True)
        if retrieve:
            self.chat_input.entry.set_sensitive(False)
            self.knowledge_control.set_sensitive(False)
            self.knowledge_control.notice.set_text(_('Retrieving sources…'))
            self.storage.services.inference.submit(self._retrieve_sources, state)
            return
        self._prepare_generation(state)

    def _retrieve_sources(self, state):
        error = None
        try:
            state.retrieval = self.storage.knowledge.retrieve(state.settings['knowledge'],
                state.settings['query_override'] or state.prompt, state.cancellable)
        except Exception as exc:
            error = exc
        GLib.idle_add(self._retrieval_finished, state, error)

    def _retrieval_finished(self, state, error):
        if self.request is not state:
            return False
        self.knowledge_control.set_sensitive(True)
        self.knowledge_control._notice()
        self.chat_input.entry.set_sensitive(True)
        if error or state.cancellable.is_cancelled() or self.closing:
            self.request = None
            self.emit('request-changed')
            self.chat_input.set_running(False)
            if not self.closing and not state.cancellable.is_cancelled():
                dialog = Adw.AlertDialog(heading=_('Could Not Retrieve Sources'), body=str(error))
                dialog.add_response('adjust', _('Adjust Sources'))
                dialog.add_response('retry', _('Retry'))
                dialog.add_response('without', _('Send Without Knowledge'))
                dialog.add_response('cancel', _('Keep Draft'))
                dialog.set_default_response('cancel')
                self._retrieval_dialog = dialog
                def response(d, choice):
                    self._retrieval_dialog = None
                    if self.closing or self._disposed:
                        return
                    if choice == 'adjust':
                        self.knowledge_control.open_picker()
                    elif choice == 'retry':
                        self.on_send_clicked()
                    elif choice == 'without':
                        self.on_send_clicked(without_knowledge=True)
                dialog.connect('response', response)
                dialog.present(self.get_root())
            self._finish_close()
        else:
            self._prepare_generation(state)
        return False

    def _prepare_generation(self, state):
        if not self._paged:
            self._begin_generation(state)
            return
        self.chat_input.entry.set_sensitive(False)
        def read():
            try:
                history = self.storage.get_messages(self.strategy.chat_id, include_ids=True)
                GLib.idle_add(ready, history)
            except Exception as exc:
                GLib.idle_add(self._retrieval_finished, state, exc)
        def ready(history):
            self.strategy.history = history
            self.strategy._saved_count = len(history)
            self._paged = False
            self.chat_input.entry.set_sensitive(True)
            self._begin_generation(state)
            return False
        self.storage.services.control.submit(read)

    def _begin_generation(self, state):
        continuation = state.continuation
        if not continuation:
            self.draft.consumed()
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
        self._sync_tools()
        if isinstance(self.strategy, ChatStrategy):
            # Results are committed once before any follow-up HTTP request.
            future = self.strategy.begin(state, on_done=lambda: self.storage.services.inference.submit(self.process_request, state, bubble))
            if future is None:
                self.storage.services.inference.submit(self.process_request, state, bubble)
        else:
            self.strategy.begin(state)
            self.storage.delete_draft(state.draft_id, state.draft_revision,
                on_done=lambda: self.storage.services.inference.submit(self.process_request, state, bubble))
        self.update_context(state)
        self._add_sources(state.metadata, bubble)
        self.chat_input.has_history_images = any(m.get('images') for m in getattr(self.strategy, 'history', []))
        self.chat_input.update_capability_controls()

    def _add_sources(self, metadata, bubble):
        if metadata.get('retrieval'):
            bubble.bubble_box.append(SourcesView(metadata['retrieval']))

    def process_request(self, state, bubble):
        from .request_runner import RequestRunner
        RequestRunner(self.storage.services).run(self.strategy, state,
            lambda content, thinking, logprobs: self._display_chunk(state, bubble, content, thinking, logprobs),
            lambda status, error: self._finish_request(state, bubble, status, error))

    def _display_chunk(self, state, bubble, content, thinking, logprobs):
        if self._disposed or self.request is not state:
            return False
        if content:
            bubble.append_text(content)
        if thinking:
            bubble.append_thinking(thinking)
        if logprobs:
            bubble.append_logprobs(logprobs)
        return False

    def _finish_request(self, state, bubble, status, error):
        if not state.finish(status, error):
            return False
        if not self._disposed:
            bubble.show_response_metadata(state.metadata, state.settings['show_stats'])
        self._tool_busy = bool(state.tool_calls)
        self.request = None
        self.emit('request-changed')
        self.chat_input.set_running(False)

        def saved():
            self._tool_busy = False
            self._sync_tools()
            if isinstance(self.strategy, ChatStrategy) and not self.strategy.deleted:
                def read_title():
                    title = self.storage.chat_title(self.strategy.chat_id)
                    def deliver():
                        if title is not None and not self._disposed:
                            self.title = display_chat_title(title)
                            self.emit('chat-updated', self.strategy.chat_id, title)
                        self._finish_close()
                        return False
                    GLib.idle_add(deliver)
                self.storage.services.control.submit(read_title)
            else:
                self._finish_close()

        future = self.strategy.save(state)
        if future is not None:
            self._add_tool_view(state.saved_message, bubble)
            self._sync_tools()
            # Retain definitions edited while the previous request was running.
            self.storage.save_tool_state(self.strategy.chat_id, options=self._local_options(), on_done=saved)
        if future is None:
            saved()
        return False

    def _tool_capabilities_changed(self, *args):
        self.options_panel.tool_support = self.chat_input.tool_support
        self.options_panel.tools_loading = self.chat_input.capabilities_loading
        self.options_panel.update_tools_notice()

    def _persist_tool_options(self, *args):
        self._settings_edited()
        if isinstance(self.strategy, ChatStrategy) and not self.closing and not self.strategy.deleted:
            def saved():
                if not self._disposed and not self.strategy.deleted:
                    self.emit('chat-updated', self.strategy.chat_id, self.title)
            self.storage.save_tool_state(self.strategy.chat_id, options=self._local_options(), on_done=saved)

    def _local_options(self):
        return dict(self.options_panel.get_tools_options(), knowledge=copy.deepcopy(self.knowledge_control.options))

    def _add_tool_view(self, message, bubble):
        if not message or not (message.get('response_metadata') or {}).get('tool_round'):
            return
        view = ToolCallsView(message, self._save_tool_result,
                             lambda: self.on_send_clicked(continuation=True), self._cancel_tool_round)
        self._tool_views.append(view)
        bubble.bubble_box.append(view)

    def _sync_tools(self):
        pending = self.strategy.pending_round if isinstance(self.strategy, ChatStrategy) else None
        editable = not (self.request or self._tool_busy or self.closing or self._disposed)
        for view in self._tool_views:
            view.update(editable and view.message is pending)
        self.chat_input.awaiting_tools = pending is not None or self._tool_busy
        self.chat_input.update_capability_controls()

    def _save_tool_result(self, message, index, text):
        if (self.request or self._tool_busy or self.closing or self._disposed
                or self.strategy.pending_round is not message):
            return
        message['response_metadata']['tool_round']['results'][index] = text
        self._save_tool_history()

    def _save_tool_history(self):
        self._tool_busy = True
        self._sync_tools()
        def saved():
            self._tool_busy = False
            self._sync_tools()
        self.storage.save_tool_state(self.strategy.chat_id, messages=self.strategy.history, on_done=saved)

    def _cancel_tool_round(self):
        if (self.request or self._tool_busy or self.closing or self._disposed
                or self.strategy.pending_round is None):
            return
        self.strategy.commit_results(cancel=True)
        self._save_tool_history()

    def close_session(self, on_done, delete=False):
        self._discard_on_close = delete
        self.closing = True
        self._close_callback = on_done
        self.chat_input.cancel_fetches()
        self.message_list.cancel_deliveries()
        self.knowledge_control.close_dialog(dispose=True)
        if self._retrieval_dialog:
            self._retrieval_dialog.close()
        for view in self._tool_views:
            view.close_editor()
        if self.options_panel._tools_dialog is not None:
            self.options_panel._tools_dialog.close()
        if self.options_panel._schema_dialog is not None:
            self.options_panel._schema_dialog.close()
        if self.options_panel._settings_dialog is not None:
            self.options_panel._settings_dialog.close()
        self.set_sensitive(False)
        if delete and isinstance(self.strategy, ChatStrategy):
            self.strategy.deleted = True
        if self.request:
            self.request.cancellable.cancel()
        else:
            # Keep the tab until earlier saves have completed, so reopening cannot
            # start another request from an older database snapshot.
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
