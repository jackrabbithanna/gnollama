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
                 initial_history: Optional[List[Dict[str, Any]]] = None, storage: Optional[ChatStorage] = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.init_template()
        self.title = _('New Chat') if mode == 'chat' else _('New Response')
        self.request = None
        self.closing = False
        self._close_callback = None
        self._disposed = False
        self._tool_busy = False
        self._tool_views = []
        
        if not storage:
            storage = ChatStorage()
        self.storage = storage

        if mode == 'chat':
            self.strategy = ChatStrategy(storage, chat_id=chat_id, initial_history=initial_history)
        else:
            self.strategy = GenerationStrategy()
            
        self.mode = mode
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
                chat_data = storage.get_chat(chat_id)
                if chat_data:
                    self.load_chat_settings(chat_data)
            
            if initial_history:
                 self.load_initial_history(self.strategy.history)
        self.on_host_changed()
        self._sync_tools()

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

    def on_host_changed(self, *args):
        host = self.options_panel.get_selected_host()
        self.options_panel.set_cloud(ollama.is_cloud(host))
        self.chat_input.fetch_models(self.storage.connection(host) if host else None)

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
        if self.request or self.closing or self._disposed or self._tool_busy:
            return
        pending = self.strategy.pending_round if isinstance(self.strategy, ChatStrategy) else None
        if pending is not None and not continuation:
            return
        if continuation and (pending is None or any(r is None for r in pending['response_metadata']['tool_round']['results'])):
            return
        prompt = '' if continuation else self.chat_input.entry.get_text().strip()
        if not prompt and not continuation:
            return
        try:
            host = self.options_panel.get_selected_host()
            if not host:
                raise ValueError(_('No host configured.'))
            hostname = ollama.validate_host(host['hostname'])
            model = self.chat_input.get_selected_model()
            if not model:
                raise ValueError(_('Select an available model before sending.'))
            from .model_manager import model_key, unloading_models
            if model_key(hostname, model) in unloading_models:
                raise ValueError(_('Wait for this model to finish unloading before sending.'))
            options = self.options_panel.get_options_from_ui()
            request_settings = self.options_panel.get_request_settings()
            draft_images = [] if continuation else self.chat_input.selected_image_paths
            if self.chat_input.capabilities_loading and (draft_images or self.chat_input.has_history_images):
                raise ValueError(_('Wait for image support to be checked before sending.'))
            if self.chat_input.image_support is False and draft_images:
                raise ValueError(_('Remove draft images or select a vision model to send.'))
            logprobs = self.options_panel.logprobs_check.get_active()
            top = self.options_panel.get_logprobs()
            images = []
            for path in draft_images:
                with open(path, 'rb') as image_file:
                    raw = image_file.read()
                from gi.repository import Gdk
                Gdk.Texture.new_from_bytes(GLib.Bytes.new(raw))
                images.append(base64.b64encode(raw).decode('ascii'))
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
        if continuation:
            self.strategy.commit_results()
        self.request = state
        self.emit('request-changed')
        self.chat_input.set_running(True)
        if retrieve:
            self.chat_input.entry.set_sensitive(False)
            self.knowledge_control.set_sensitive(False)
            self.knowledge_control.notice.set_text(_('Retrieving sources…'))
            worker.submit(self._retrieve_sources, state)
            return
        self._begin_generation(state)

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
            self._begin_generation(state)
        return False

    def _begin_generation(self, state):
        continuation = state.continuation
        if not continuation:
            self.chat_input.entry.set_text('')
            self.chat_input.on_clear_image_clicked(None)
            self.knowledge_control.query.set_text('')
            self.message_list.add_user_message(state.prompt, images=state.images)
        from .bubbles import AiBubble
        bubble = AiBubble(model_name=state.settings['model'], output_format=state.settings.get('format'))
        bubble.set_api_details(state.api_details())
        self.message_list.add_ai_bubble(bubble)
        self._sync_tools()
        if continuation or state.retrieval:
            # Results are committed once before any follow-up HTTP request.
            future = self.strategy.begin(state, on_done=lambda: worker.submit(self.process_request, state, bubble))
            if future is None:
                worker.submit(self.process_request, state, bubble)
        else:
            self.strategy.begin(state)
            worker.submit(self.process_request, state, bubble)
        self._add_sources(state.metadata, bubble)
        self.chat_input.has_history_images = any(m.get('images') for m in getattr(self.strategy, 'history', []))
        self.chat_input.update_capability_controls()

    def _add_sources(self, metadata, bubble):
        if metadata.get('retrieval'):
            bubble.bubble_box.append(SourcesView(metadata['retrieval']))

    def process_request(self, state, bubble):
        status, error = 'failed', None
        try:
            if state.cancellable.is_cancelled():
                raise ollama.RequestCancelled()
            for chunk in self.strategy.process(state):
                if state.cancellable.is_cancelled():
                    raise ollama.RequestCancelled()
                content, thinking, logprobs = state.consume(chunk)
                GLib.idle_add(self._display_chunk, state, bubble, content, thinking, logprobs)
                if chunk.get('done'):
                    status = 'complete'
                    break
            else:
                raise ollama.OllamaError(_('Response ended before completion.'))
        except ollama.RequestCancelled:
            status = 'stopped'
        except Exception as exc:
            error = str(exc)
        # Validate on the worker so schema evaluation does not block GTK.
        if state.tool_calls:
            state.metadata['tool_round'] = inspect_calls(state.tool_calls, state.settings.get('tools'), status)
        if not state.tool_calls and state.settings.get('format') is not None:
            try:
                state.metadata['validation'] = validate_response(state.content, state.settings['format'], status)
            except Exception as exc:
                state.metadata['validation'] = {'status': 'validation_error', 'message': str(exc)}
        GLib.idle_add(self._finish_request, state, bubble, status, error)

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
                data = self.storage.get_chat(self.strategy.chat_id)
                if data and not self._disposed:
                    self.title = display_chat_title(data['title'])
                    self.emit('chat-updated', self.strategy.chat_id, data['title'])
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
        self.closing = True
        self._close_callback = on_done
        self.chat_input.cancel_fetches()
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
        if self.closing and self.request is None and self._close_callback:
            callback, self._close_callback = self._close_callback, None
            self._disposed = True
            callback()
