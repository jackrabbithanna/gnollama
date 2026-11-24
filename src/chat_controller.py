"""A chat or single-response session, independent of its GTK presentation.

The controller owns the conversation strategy, the active request, tool-round
state, history paging, and the close sequence. It reports to a view object and
never touches widgets. Every method is called on the main context; workers only
retrieve, read history, stream, and save.

The view provides:

    request_changed()                    a request started or ended
    retrieval_started() / retrieval_ended()
    retrieval_failed(error)              offer the recovery choices
    history_loading(active)              the full history is being read
    generation_started(state)            show the prompt and an empty response
    generation_prepared(state)           the request is saved and about to run
    chunk(state, content, thinking, logprobs)
    request_finished(state)              show final metadata
    message_saved(state)                 state.saved_message is in the history
    tools_changed()                      tool-round editability changed
    title_changed(chat_id, title)
    options_saved()
    older_loaded(messages)               an earlier page of history
    context_estimated(estimate, expand)
    local_options()                      tool and knowledge options to store
    attachments_pending()                images are still being imported
"""
import copy
from dataclasses import dataclass, field
from gettext import gettext as _
from typing import Any, Callable, Optional

from gi.repository import GLib

from .context import estimate_context
from .knowledge import validate_rag
from .providers import validate_url
from .records import ModelTarget, RequestSettings, ViewState, WireOptions
from .request_runner import RequestRunner
from .services import model_key
from .session import ChatStrategy, GenerationStrategy, RequestState

PAGE_SIZE = 50


def unavailable(capabilities, mode):
    """Why a session of this mode cannot use a host with these capabilities, or ''."""
    if mode != 'chat' and not capabilities.raw_generate:
        return _('This server does not offer single responses. Open a chat to use it.')
    return ''


@dataclass(frozen=True)
class ComposerInput:
    """What the composer and its options held when Send was pressed."""
    prompt: str = ''
    images: tuple = ()
    host: dict | None = None
    model: str | None = None
    thinking: bool | str | None = None
    options: dict = field(default_factory=dict)
    # Output format, tools, and keep-alive, with the text they were parsed from.
    request: dict = field(default_factory=dict)
    system: str | None = None
    logprobs: bool = False
    top_logprobs: int | None = None
    show_stats: bool = True
    knowledge: dict = field(default_factory=dict)
    query_override: str = ''
    # Values the panel holds for options the host or model lacks: stored, never sent.
    unsent: dict = field(default_factory=dict)
    # None while unknown; False when the model cannot read images.
    image_support: bool | None = None
    capabilities_loading: bool = False

    def settings(self, url=None, endpoint='chat', history_images_omitted=False,
                 draft_settings=None, extra=None):
        """The one place a composer's values are sorted into what is sent and what is kept.

        The result shares nothing with the widgets it was read from.
        """
        request = self.request
        target = ModelTarget(self.host['id'], url, self.model, self.host.get('name')) if self.host else None
        wire = WireOptions(options=self.options, system=self.system, thinking=self.thinking,
                           format=request.get('format'), keep_alive=request.get('keep_alive'),
                           tools=request.get('tools'), logprobs=self.logprobs,
                           top_logprobs=self.top_logprobs)
        view = ViewState(show_stats=self.show_stats, output_mode=request.get('output_mode', 'text'),
                         schema_text=request.get('schema_text', ''),
                         tools_enabled=request.get('tools_enabled', False),
                         tools_text=request.get('tools_text', ''), knowledge=self.knowledge,
                         query_override=self.query_override, draft_settings=draft_settings or {},
                         unsent=self.unsent,
                         extra=extra or {})
        return copy.deepcopy(RequestSettings(target, wire, view, endpoint, history_images_omitted))


class ModelLimits:
    """What a host's models can be sent, from the details the catalog holds.

    A view and the validation behind it ask the same instance. The catalog
    forgets details after a minute, so the last ones seen are kept here and a
    request is checked against what the options panel was showing.
    """
    def __init__(self, context):
        self.context = context
        self._seen: tuple = (None, None)

    def of(self, host, model):
        """The record for `model` on the host record `host`; the provider's own while details are unknown."""
        if not host or not model:
            return self.context.capabilities(host)
        connection = self.context.connection(host)
        try:
            key = model_key(host, model)
        except ValueError:  # An address that cannot be used; sending says so.
            return connection.capabilities
        details = self.context.services.catalog.cached_details(connection, model)
        if details is not None:
            self._seen = (key, details)
        elif self._seen[0] == key:
            details = self._seen[1]
        return connection.model_capabilities(details)


class ChatController:
    def __init__(self, context, view, mode='generate', chat_id=None, initial_history=None, chat_data=None):
        self.context, self.view, self.mode = context, view, mode
        self.storage, self.services = context.storage, context.services
        self.limits = ModelLimits(context)
        self.strategy: Any
        if mode == 'chat':
            self.strategy = ChatStrategy(self.storage, chat_id=chat_id, initial_history=initial_history)
        else:
            self.strategy = GenerationStrategy()
        chat_data = chat_data or {}
        self.paged = bool(chat_data.get('paged'))
        self.message_offset = chat_data.get('message_offset', 0)
        self.draft: Any = None  # Assigned by the view, which knows how to read the composer.
        # Stored options this version does not know; the view sets them when it loads a chat.
        self.extra = {}
        self.request: Any = None
        self.closing = False
        self.disposed = False
        self.tool_busy = False
        self._close_callback: Optional[Callable[[], None]] = None
        self._close_saving = False
        self._discard_on_close = False
        self._context_generation = 0

    # --- State ---

    @property
    def is_chat(self):
        return isinstance(self.strategy, ChatStrategy)

    @property
    def chat_id(self):
        return getattr(self.strategy, 'chat_id', None)

    @property
    def history(self):
        return getattr(self.strategy, 'history', [])

    @property
    def pending_round(self):
        return self.strategy.pending_round if self.is_chat else None

    @property
    def has_history_images(self):
        return any(m.get('images') for m in self.history)

    @property
    def tools_editable(self):
        return not (self.request or self.tool_busy or self.closing or self.disposed)

    # --- Sending ---

    def can_send(self, continuation=False):
        if self.request or self.closing or self.disposed or self.tool_busy:
            return False
        pending = self.pending_round
        if pending is not None and not continuation:
            return False
        if continuation and (pending is None or
                             any(r is None for r in pending['response_metadata']['tool_round']['results'])):
            return False
        return True

    def check_target(self, host, model):
        """Return the validated host URL, or raise why this target cannot be used."""
        if not host:
            raise ValueError(_('No host configured.'))
        hostname = validate_url(host['hostname'])
        if not model:
            raise ValueError(_('Select an available model before sending.'))
        if model_key(host, model) in self.services.models.reserved:
            raise ValueError(_('Wait for this model to finish unloading before sending.'))
        return hostname

    def send(self, input, continuation=False, without_knowledge=False):
        """Start a request, or raise the validation error that prevents it."""
        if not self.can_send(continuation):
            return False
        hostname = self.check_target(input.host, input.model)
        limits = self.context.capabilities(input.host)
        reason = unavailable(limits, self.mode)
        if reason:
            raise ValueError(reason)
        images = [] if continuation else list(input.images)
        if input.capabilities_loading and (images or self.has_history_images):
            raise ValueError(_('Wait for image support to be checked before sending.'))
        if input.image_support is False and images:
            raise ValueError(_('Remove draft images or select a vision model to send.'))
        settings = input.settings(
            hostname, self.mode, extra=self.extra,
            history_images_omitted=input.image_support is False and self.has_history_images)
        limits.require(settings.wire, input.host)
        self.limits.of(input.host, input.model).require(settings.wire, input.host, input.model)
        knowledge = settings.view.knowledge
        retrieve = (self.mode == 'chat' and knowledge.get('enabled') and
                    not continuation and not without_knowledge)
        if retrieve:
            validate_rag(knowledge)

        state = RequestState(settings, '' if continuation else input.prompt, images, continuation=continuation,
                             connection=self.context.connection(input.host))
        if continuation:
            self.strategy.commit_results()
        else:
            self.draft.flush()
            state.draft_id, state.draft_revision = self.draft.id, self.draft.revision
        self.request = state
        self.view.request_changed()
        if retrieve:
            self.view.retrieval_started()
            self.services.inference.submit(self._retrieve_sources, state)
        else:
            self._prepare_generation(state)
        return True

    def stop(self):
        if self.request:
            self.request.cancellable.cancel()

    def _retrieve_sources(self, state):
        error = None
        try:
            state.retrieval = self.context.knowledge.retrieve(
                state.settings.view.knowledge, state.settings.view.query_override or state.prompt,
                state.cancellable)
        except Exception as exc:
            error = exc
        GLib.idle_add(self._retrieval_finished, state, error)

    def _retrieval_finished(self, state, error):
        if self.request is not state:
            return False
        self.view.retrieval_ended()
        if error or state.cancellable.is_cancelled() or self.closing:
            self.request = None
            self.view.request_changed()
            if not self.closing and not state.cancellable.is_cancelled():
                self.view.retrieval_failed(error)
            self._finish_close()
        else:
            self._prepare_generation(state)
        return False

    def _prepare_generation(self, state):
        if not self.paged:
            self._begin_generation(state)
            return
        # Only a page was loaded for display; the request needs the whole history.
        self.view.history_loading(True)

        def read():
            try:
                history = self.storage.get_messages(self.strategy.chat_id, include_ids=True)
                GLib.idle_add(ready, history)
            except Exception as exc:
                GLib.idle_add(self._retrieval_finished, state, exc)

        def ready(history):
            self.strategy.history = history
            self.strategy._saved_count = len(history)
            self.paged = False
            self.view.history_loading(False)
            self._begin_generation(state)
            return False
        self.services.control.submit(read)

    def _begin_generation(self, state):
        if not state.continuation:
            self.draft.consumed()
        self.view.generation_started(state)
        def run():
            return self.services.inference.submit(self._run, state)
        if self.is_chat:
            # Results are committed once before any follow-up HTTP request.
            if self.strategy.begin(state, on_done=run) is None:
                run()
        else:
            self.strategy.begin(state)
            self.storage.delete_draft(state.draft_id, state.draft_revision, on_done=run)
        self._context_generation += 1
        messages = getattr(self.strategy, 'history',
                           [dict(role='user', content=state.prompt, images=state.images)])
        estimate = estimate_context(messages, state.settings.wire, state.retrieval)
        state.metadata['context_estimate'] = estimate
        self.view.context_estimated(estimate, True)
        self.view.generation_prepared(state)

    def _run(self, state):
        RequestRunner(self.services).run(
            self.strategy, state,
            lambda content, thinking, logprobs: self._chunk(state, content, thinking, logprobs),
            lambda status, error: self._finish_request(state, status, error))

    def _chunk(self, state, content, thinking, logprobs):
        if not self.disposed and self.request is state:
            self.view.chunk(state, content, thinking, logprobs)
        return False

    def _finish_request(self, state, status, error):
        if not state.finish(status, error):
            return False
        if not self.disposed:
            self.view.request_finished(state)
        self.tool_busy = bool(state.tool_calls)
        self.request = None
        self.view.request_changed()

        def saved():
            self.tool_busy = False
            self.view.tools_changed()
            if self.is_chat and not self.strategy.deleted:
                def read_title():
                    title = self.storage.chat_title(self.strategy.chat_id)

                    def deliver():
                        if title is not None and not self.disposed:
                            self.view.title_changed(self.strategy.chat_id, title)
                        self._finish_close()
                        return False
                    GLib.idle_add(deliver)
                self.services.control.submit(read_title)
            else:
                self._finish_close()

        if self.strategy.save(state) is None:
            saved()
        else:
            self.view.message_saved(state)
            self.view.tools_changed()
            # Retain definitions edited while the previous request was running.
            self.storage.save_tool_state(self.strategy.chat_id, options=self.view.local_options(), on_done=saved)
        return False

    # --- Tool rounds and stored options ---

    def save_tool_result(self, message, index, text):
        if not self.tools_editable or self.pending_round is not message:
            return
        message['response_metadata']['tool_round']['results'][index] = text
        self._save_tool_history()

    def cancel_tool_round(self):
        if not self.tools_editable or self.pending_round is None:
            return
        self.strategy.commit_results(cancel=True)
        self._save_tool_history()

    def _save_tool_history(self):
        self.tool_busy = True
        self.view.tools_changed()

        def saved():
            self.tool_busy = False
            self.view.tools_changed()
        self.storage.save_tool_state(self.strategy.chat_id, messages=self.strategy.history, on_done=saved)

    def persist_options(self):
        """Store the view's tool and knowledge options with the conversation."""
        if not self.is_chat or self.closing or self.strategy.deleted:
            return

        def saved():
            if not self.disposed and not self.strategy.deleted:
                self.view.options_saved()
        self.storage.save_tool_state(self.strategy.chat_id, options=self.view.local_options(), on_done=saved)

    # --- History paging and context ---

    def load_older(self):
        if self.message_offset <= 0:
            return False
        offset = max(0, self.message_offset - PAGE_SIZE)
        count = self.message_offset - offset

        def read():
            messages = self.storage.get_messages(self.strategy.chat_id, count, offset, True)
            GLib.idle_add(deliver, messages)

        def deliver(messages):
            if not self.closing:
                self.message_offset = offset
                self.view.older_loaded(messages)
            return False
        self.services.control.submit(read)
        return True

    def estimate_draft(self, prompt, wire):
        """Estimate the context a prompt would use with the current history."""
        latest = dict(role='user', content=prompt)
        if not self.paged:
            self.view.context_estimated(estimate_context(list(self.history) + [latest], wire), True)
            return
        self._context_generation += 1
        generation = self._context_generation

        def read():
            history = self.storage.get_messages(self.strategy.chat_id)
            estimate = estimate_context(history + [latest], wire)

            def deliver():
                if generation == self._context_generation and not self.closing:
                    self.view.context_estimated(estimate, False)
                return False
            GLib.idle_add(deliver)
        self.services.control.submit(read)

    # --- Closing ---

    def close(self, on_done, delete=False):
        self._discard_on_close = delete
        self.closing = True
        self._close_callback = on_done
        if delete and self.is_chat:
            self.strategy.deleted = True
        if self.request:
            self.request.cancellable.cancel()
        else:
            # Wait until earlier saves have completed, so reopening cannot
            # start another request from an older database snapshot.
            self.storage.after_pending_writes(self._finish_close)

    def view_ready(self):
        """The view finished something that was holding a close."""
        self._finish_close()

    def _finish_close(self):
        if (self.closing and self.request is None and self._close_callback
                and not self.view.attachments_pending() and not self._close_saving):
            self._close_saving = True
            self.draft.close(discard=self._discard_on_close)

            def complete():
                callback, self._close_callback = self._close_callback, None
                self.disposed = True
                if callback is not None:
                    callback()
            self.storage.after_pending_writes(complete)
