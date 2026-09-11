"""Fresh-prompt comparison preparation and independent target lifecycles."""
import copy
import uuid
from dataclasses import dataclass, field
from gi.repository import Gio, GLib
from . import ollama
from .knowledge import augmented_messages, validate_rag
from .context import estimate_context
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


def prepare_run(storage, active, prompt, images, settings, targets, draft_id, revision):
    settings, images, targets = copy.deepcopy((settings, images, targets))
    if not 2 <= len(targets) <= 4:
        raise ValueError(_('Choose two to four targets.'))
    identities = [(t['host_id'], t['model']) for t in targets]
    if len(set(identities)) != len(identities):
        raise ValueError(_('Each target must use a different host and model pair.'))
    resolved = []
    for target in targets:
        if active.cancellable.is_cancelled():
            raise ollama.RequestCancelled()
        host = storage.get_host(target['host_id'])
        if host is None or not target['model']:
            raise ValueError(_('Select an available model before sending.'))
        connection = storage.connection(host)
        models = dict(storage.services.catalog.models(connection, active.cancellable))
        if target['model'] not in models:
            raise ValueError(_('Selected model is no longer available.'))
        details = models[target['model']]
        if details is None:
            try:
                details = storage.services.catalog.details(connection, target['model'], active.cancellable)
            except ollama.RequestCancelled:
                raise
            except ollama.OllamaError:
                details = None
        caps = details.get('capabilities') if details else None
        if caps is not None:
            if 'embedding' in caps and 'completion' not in caps:
                raise ValueError(_('Embedding models cannot generate comparisons.'))
            if images and 'vision' not in caps:
                raise ValueError(_('Every target must support the shared images.'))
            if settings.get('thinking') not in (None, False) and 'thinking' not in caps:
                raise ValueError(_('Every target must support the selected thinking option.'))
        if ollama.is_cloud(host) and (settings.get('format') is not None or settings.get('keep_alive') is not None):
            raise ValueError(_('Cloud targets do not support shared structured output or model retention.'))
        family = (details or {}).get('details', {}).get('family', '').replace('-', '').lower()
        if family == 'gptoss' and settings.get('thinking') not in (None, 'low', 'medium', 'high'):
            raise ValueError(_('The selected thinking level is incompatible with a target.'))
        resolved.append((target, host, connection))
    retrieval = None
    if settings.get('knowledge', {}).get('enabled'):
        validate_rag(settings['knowledge'])
        retrieval = copy.deepcopy(storage.knowledge.retrieve(settings['knowledge'], settings.get('query_override') or prompt, active.cancellable))
    messages = [{'role': 'user', 'content': prompt}]
    if images:
        messages[0]['images'] = copy.deepcopy(images)
    if settings.get('system'):
        messages.insert(0, {'role': 'system', 'content': settings['system']})
    wire = augmented_messages(messages, retrieval)
    run = dict(id=active.id, prompt=prompt, images=images, settings=settings, retrieval=retrieval,
               message_uid=str(uuid.uuid4()), targets=[], draft_id=draft_id, draft_revision=revision)
    for target, host, connection in resolved:
        id = str(uuid.uuid4())
        effective = dict(settings, host=ollama.validate_host(host['hostname']), host_id=host['id'],
                         model=target['model'], endpoint='chat')
        state = RequestState(effective, prompt, images, messages=copy.deepcopy(wire), connection=connection, retrieval=copy.deepcopy(retrieval))
        state.metadata['retrieval'] = copy.deepcopy(retrieval)
        state.metadata['context_estimate'] = estimate_context(messages, effective, retrieval)
        active.states[id] = state
        run['targets'].append(dict(id=id, settings=effective, request=dict(messages=copy.deepcopy(wire),
            **{k: effective.get(k) for k in ('model', 'options', 'thinking', 'format', 'keep_alive', 'logprobs', 'top_logprobs')})))
    if active.cancellable.is_cancelled():
        raise ollama.RequestCancelled()
    active.pending = set(active.states)
    return run


class ComparisonController:
    def __init__(self, storage, active):
        self.storage, self.active = storage, active

    def dispatch(self, chunk, done):
        for id, state in self.active.states.items():
            def work(id=id, state=state):
                self.storage._submit(self.storage.db.start_comparison_target, id)
                RequestRunner(self.storage.services).run(ChatStrategy(self.storage), state,
                    lambda *parts: chunk(id, *parts), lambda status, error: finished(id, state, status, error))
            self.storage.services.inference.submit(work)

        def finished(id, state, status, error):
            state.finish(status, error)
            message = dict(uid=id, role='assistant', model=state.settings['model'], content=state.content,
                thinking_content=state.thinking, api_details=state.api_details(), response_metadata=state.metadata)
            def saved():
                self.active.pending.discard(id)
                done(id, state)
            self.storage._submit(self.storage.db.finish_comparison_target, id, copy.deepcopy(message), on_done=saved)
