"""Fresh-prompt comparison preparation and independent target lifecycles."""
import copy
import uuid
from dataclasses import dataclass, field
from gi.repository import Gio, GLib
from .providers import ProviderError, RequestCancelled, validate_url
from .knowledge import augmented_messages, validate_rag
from dataclasses import replace
from .context import estimate_context
from .records import ModelTarget
from .session import RequestState, ChatStrategy
from .request_runner import RequestRunner


@dataclass
class ComparisonRun:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    cancellable: object = field(default_factory=Gio.Cancellable)
    states: dict = field(default_factory=dict)
    pending: set = field(default_factory=set)

    def __post_init__(self):
        self.cancellable.connect(lambda *args: [state.cancellable.cancel() for state in list(self.states.values())])


def prepare_run(context, active, prompt, images, settings, targets, draft_id, revision):
    """`settings` is the RequestSettings every target shares; each gets its own target."""
    settings, images, targets = copy.deepcopy((settings, images, targets))
    wire, knowledge = settings.wire, settings.view.knowledge
    if not 2 <= len(targets) <= 4:
        raise ValueError(_('Choose two to four targets.'))
    identities = [(t['host_id'], t['model']) for t in targets]
    if len(set(identities)) != len(identities):
        raise ValueError(_('Each target must use a different host and model pair.'))
    resolved = []
    for target in targets:
        if active.cancellable.is_cancelled():
            raise RequestCancelled()
        host = context.storage.get_host(target['host_id'])
        if host is None or not target['model']:
            raise ValueError(_('Select an available model before sending.'))
        connection = context.connection(host)
        models = dict(context.services.catalog.models(connection, active.cancellable))
        if target['model'] not in models:
            raise ValueError(_('Selected model is no longer available.'))
        details = models[target['model']]
        if details is None:
            try:
                details = context.services.catalog.details(connection, target['model'], active.cancellable)
            except RequestCancelled:
                raise
            except ProviderError:
                details = None
        caps = details.get('capabilities') if details else None
        if caps is not None:
            if 'embedding' in caps and 'completion' not in caps:
                raise ValueError(_('Embedding models cannot generate comparisons.'))
            if images and 'vision' not in caps:
                raise ValueError(_('Every target must support the shared images.'))
            if wire.thinking not in (None, False) and 'thinking' not in caps:
                raise ValueError(_('Every target must support the selected thinking option.'))
        limits = connection.capabilities
        if ((wire.format is not None and not limits.structured_output)
                or (wire.keep_alive is not None and not limits.keep_alive)):
            raise ValueError(_('Cloud targets do not support shared structured output or model retention.'))
        family = (details or {}).get('details', {}).get('family', '').replace('-', '').lower()
        if family == 'gptoss' and wire.thinking not in (None, 'low', 'medium', 'high'):
            raise ValueError(_('The selected thinking level is incompatible with a target.'))
        resolved.append((target, host, connection))
    retrieval = None
    if knowledge.get('enabled'):
        validate_rag(knowledge)
        retrieval = copy.deepcopy(context.knowledge.retrieve(knowledge, settings.view.query_override or prompt, active.cancellable))
    messages = [{'role': 'user', 'content': prompt}]
    if images:
        messages[0]['images'] = copy.deepcopy(images)
    if wire.system:
        messages.insert(0, {'role': 'system', 'content': wire.system})
    sent = augmented_messages(messages, retrieval)
    run = dict(id=active.id, prompt=prompt, images=images, settings=settings.to_stored(), retrieval=retrieval,
               message_uid=str(uuid.uuid4()), targets=[], draft_id=draft_id, draft_revision=revision)
    for target, host, connection in resolved:
        id = str(uuid.uuid4())
        effective = replace(settings, endpoint='chat', target=ModelTarget(
            host['id'], validate_url(host['hostname']), target['model']))
        state = RequestState(effective, prompt, images, messages=copy.deepcopy(sent), connection=connection, retrieval=copy.deepcopy(retrieval))
        state.metadata['retrieval'] = copy.deepcopy(retrieval)
        state.metadata['context_estimate'] = estimate_context(messages, wire, retrieval)
        active.states[id] = state
        run['targets'].append(dict(id=id, settings=effective.to_stored(), request=dict(
            messages=copy.deepcopy(sent), model=target['model'], **copy.deepcopy(wire.provider_args()))))
    if active.cancellable.is_cancelled():
        raise RequestCancelled()
    active.pending = set(active.states)
    return run


class ComparisonController:
    def __init__(self, context, active):
        self.context, self.active = context, active
        self.storage = context.storage

    def dispatch(self, chunk, done):
        for id, state in self.active.states.items():
            def work(id=id, state=state):
                self.storage.submit(self.storage.library.start_comparison_target, id)
                RequestRunner(self.context.services).run(ChatStrategy(self.storage), state,
                    lambda *parts: chunk(id, *parts), lambda status, error: finished(id, state, status, error))
            self.context.services.inference.submit(work)

        def finished(id, state, status, error):
            state.finish(status, error)
            message = dict(uid=id, role='assistant', model=state.settings.target.model, content=state.content,
                thinking_content=state.thinking, api_details=state.api_details(), response_metadata=state.metadata)
            def saved():
                self.active.pending.discard(id)
                done(id, state)
            self.storage.submit(self.storage.library.finish_comparison_target, id, copy.deepcopy(message), on_done=saved)
