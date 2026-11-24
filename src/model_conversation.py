"""Sequential model conversations, independent of their GTK presentation."""
from gettext import gettext as _
import copy
import uuid
from dataclasses import replace
from typing import Any
from gi.repository import Gio, GLib
from .providers import ProviderError, RequestCancelled, validate_url
from .context import estimate_context
from .records import RequestSettings
from .request_runner import RequestRunner
from .session import RequestState, ChatStrategy


def conversation_messages(snapshot, turn):
    """Translate successful turns into the current participant's perspective."""
    participant = turn % 2
    settings = snapshot['options']['participants'][participant]
    messages = []
    if settings.get('system'):
        messages.append(dict(role='system', content=settings['system']))
    by_uid = {m['uid']: m for m in snapshot['messages']}
    seed = next(m for m in snapshot['messages'] if m['role'] == 'user')
    input_uid = seed['uid']
    if participant == 0:
        messages.append(dict(role='user', content=seed['content']))
    successful = [a for a in snapshot['attempts'] if a['status'] == 'complete' and a['turn'] < turn]
    if [a['turn'] for a in successful] != list(range(turn)):
        raise ValueError(_('The saved conversation has an incomplete turn history.'))
    for attempt in successful:
        message = by_uid[attempt['id']]
        messages.append(dict(role='assistant' if attempt['turn'] % 2 == participant else 'user', content=message['content']))
        input_uid = attempt['id']
    return messages, input_uid


def validate_participants(context, participants, cancel):
    """Resolve two RequestSettings to their hosts; return them with their connections."""
    if len(participants) != 2:
        raise ValueError(_('Choose two participants.'))
    resolved, connections = [], []
    for settings in participants:
        if cancel.is_cancelled():
            raise RequestCancelled()
        target, wire = settings.target, settings.wire
        host = context.storage.get_host(target.host_id) if target else None
        if host is None or not target.model:
            raise ValueError(_('Select an available model before sending.'))
        destination = validate_url(host['hostname'])
        if target.url and target.url != destination:
            raise ValueError(_('A participant’s server address changed. Restore the host or use Run Again.'))
        connection = context.connection(host)
        models = dict(context.services.catalog.models(connection, cancel))
        if target.model not in models:
            raise ValueError(_('Selected model is no longer available.'))
        details = models[target.model]
        if details is None:
            try:
                details = context.services.catalog.details(connection, target.model, cancel)
            except RequestCancelled:
                raise
            except ProviderError:
                details = None
        caps = (details or {}).get('capabilities')
        if caps is not None:
            if 'embedding' in caps and 'completion' not in caps:
                raise ValueError(_('Embedding models cannot join a conversation.'))
            if wire.thinking not in (None, False) and 'thinking' not in caps:
                raise ValueError(_('A participant does not support the selected thinking option.'))
        family = (details or {}).get('details', {}).get('family', '').replace('-', '').lower()
        if family == 'gptoss' and wire.thinking not in (None, 'low', 'medium', 'high'):
            raise ValueError(_('The selected thinking level is incompatible with a target.'))
        levels = (details or {}).get('thinking_levels')
        if levels is not None and wire.thinking is not None and wire.thinking not in levels:
            raise ValueError(_('The selected thinking level is incompatible with a target.'))
        connection.capabilities.require(replace(wire, format=None, tools=None), host)
        resolved.append(replace(settings, endpoint='chat',
                                target=replace(target, url=destination, host_name=host.get('name')),
                                wire=replace(wire, format=None, tools=None)))
        connections.append(connection)
    return resolved, connections


class ModelConversationController:
    """All transitions run on the main context; workers only prepare/stream/save."""
    def __init__(self, context, changed=lambda: None, started=lambda *a: None,
                 chunk=lambda *a: None, finished=lambda *a: None):
        self.context = context
        self.storage = context.storage
        self.changed, self.started, self.chunk, self.finished = changed, started, chunk, finished
        self.id = None
        self.run: Any = None
        self.settings = None      # As stored: rounds, participants, and the setup draft.
        self.participants = []    # The same participants as RequestSettings.
        self.busy = False
        self.error = None
        self.states = {}
        self.cancellable = Gio.Cancellable()
        self.cancellable.connect(self._cancelled)
        self._pause = False
        self._end = None
        self._generation = 0

    def _cancelled(self, *args):
        for state in list(self.states.values()):
            state.cancellable.cancel()

    def _write(self, fn, *args, done):
        result: list = []
        def work():
            value = fn(*args)
            result[:] = [value]
        self.storage.submit(work, on_done=lambda: done(result[0]))

    def _work(self, fn, done):
        generation = self._generation
        def work():
            try:
                result, error = fn(), None
            except Exception as exc:
                result, error = None, exc
            def deliver():
                if generation == self._generation:
                    done(result, error)
                return False
            GLib.idle_add(deliver)
        self.context.services.inference.submit(work)

    def start(self, prompt, rounds, participants, draft_id=None, draft_revision=None, draft_settings=None):
        if self.busy or self.id:
            return
        if not prompt.strip() or type(rounds) is not int or not 1 <= rounds <= 100:
            raise ValueError(_('Enter an opening prompt and choose 1–100 rounds.'))
        participants, draft_settings = copy.deepcopy((participants, draft_settings))
        self._begin()
        def prepared(result, error):
            if error or self._end:
                self._preparation_failed(error)
                return
            self.participants, self.connections = result
            self.id = str(uuid.uuid4())
            self.settings = dict(rounds=rounds, participants=[p.to_stored(sparse=True) for p in self.participants],
                                 draft_settings=draft_settings or {})
            record = dict(id=self.id, prompt=prompt, prompt_uid=str(uuid.uuid4()), settings=self.settings,
                          draft_id=draft_id, draft_revision=draft_revision)
            def saved(_):
                self.run = dict(id=self.id, status='running', next_turn=0)
                self.changed()
                self._advance()
            self._write(self.storage.library.create_model_conversation, record, done=saved)
        self._work(lambda: validate_participants(self.context, participants, self.cancellable), prepared)

    def load(self, snapshot):
        self.id = snapshot['id']
        self.run = copy.deepcopy(snapshot['run'])
        self.settings = copy.deepcopy(snapshot['options'])
        self.participants = [RequestSettings.from_stored(p) for p in self.settings['participants']]

    def resume(self):
        if self.busy or not self.run or self.run['status'] not in ('paused', 'interrupted', 'failed'):
            return
        self._begin()
        def prepared(result, error):
            if error or self._end:
                self._preparation_failed(error)
                return
            self.connections = result[1]
            self._write(self.storage.library.set_model_conversation_status, self.id, 'running', done=self._resumed)
        self._work(lambda: validate_participants(self.context, self.participants, self.cancellable), prepared)

    def _resumed(self, run):
        self.run = run
        self._advance()

    def _begin(self):
        self._generation += 1
        self.busy, self.error, self._pause, self._end = True, None, False, None
        self.cancellable = Gio.Cancellable()
        self.cancellable.connect(self._cancelled)
        self.changed()

    def _preparation_failed(self, error):
        if error and not isinstance(error, RequestCancelled):
            self.error = str(error)
        if self.id and self._end:
            self._settle(self._end)
        else:
            self.busy = False
            self.changed()

    def pause(self):
        if self.busy and not self._end:
            self._pause = True
            self.changed()

    def stop(self, interrupted=False):
        if not self.busy:
            if not interrupted and self.run and self.run['status'] in ('paused', 'failed', 'interrupted'):
                self.busy, self._end = True, 'stopped'
                self.changed()
                self._settle('stopped')
            return
        self._end = 'interrupted' if interrupted else 'stopped'
        self.cancellable.cancel()
        self.changed()

    def _settle(self, status):
        def saved(run):
            if run and self._end and status != self._end and run['status'] not in ('stopped', 'complete'):
                self._settle(self._end)
                return
            self.run, self.busy = run, False
            self.changed()
        self._write(self.storage.library.set_model_conversation_status, self.id, status, done=saved)

    def _advance(self):
        if not self.run or self.run['status'] in ('complete', 'stopped'):
            self.busy = False
            self.changed()
            return
        if self._end:
            self._settle(self._end)
            return
        if self.run['status'] == 'failed':
            self.busy = False
            self.changed()
            return
        if self._pause:
            self._settle('paused')
            return
        if self.run['status'] != 'running':
            self.busy = False
            self.changed()
            return
        def ready(snapshot, error):
            if self._end or self._pause:
                self._settle(self._end or 'paused')
                return
            if error or snapshot is None:
                self.error = str(error) if error else _('Conversation no longer exists.')
                self.busy = False
                self.changed()
                return
            turn = self.run['next_turn']
            try:
                messages, input_uid = conversation_messages(snapshot, turn)
            except Exception as exc:
                self.error = str(exc)
                self._settle('failed')
                return
            settings = self.participants[turn % 2]
            state = RequestState(settings, messages[-1]['content'], messages=messages,
                                 connection=self.connections[turn % 2])
            state.metadata['context_estimate'] = estimate_context(messages, settings.wire)
            uid = str(uuid.uuid4())
            self.states = {uid: state}
            message = self._message(uid, state)
            def saved(attempt):
                if not attempt:
                    self.states.clear()
                    self.busy = False
                    self.changed()
                    return
                if self._pause and not self._end:
                    self._discard_pending(uid)
                    return
                if self._end:
                    self.started(attempt, state)
                    self._finish(uid, state, 'stopped', None)
                    return
                def work():
                    if self._pause and not self._end:
                        GLib.idle_add(self._discard_pending, uid)
                        return
                    GLib.idle_add(self.started, attempt, state)
                    RequestRunner(self.context.services).run(ChatStrategy(self.storage), state,
                        lambda *parts: self.chunk(uid, *parts),
                        lambda status, error: self._finish(uid, state, status, error))
                self.context.services.inference.submit(work)
                self.changed()
            self._write(self.storage.library.start_model_conversation_attempt, self.id, turn, uid, input_uid, message, done=saved)
        self._work(lambda: self.storage.export_snapshot(self.id), ready)

    def _discard_pending(self, uid):
        def saved(_):
            self.states.clear()
            self._settle(self._end or 'paused')
        self._write(self.storage.library.discard_pending_model_conversation_attempt, uid, done=saved)
        return False

    @staticmethod
    def _message(uid, state):
        return dict(uid=uid, role='assistant', model=state.settings.target.model, content=state.content,
                    thinking_content=state.thinking, api_details=state.api_details(), response_metadata=copy.deepcopy(state.metadata))

    def _finish(self, uid, state, status, error):
        if state.finalized:
            return
        if self._end:
            status = self._end
        elif status == 'complete' and (not state.content.strip() or state.tool_calls):
            status, error = 'failed', _('The model returned no usable text answer. Retry this turn.')
        state.finish(status, error)
        message = self._message(uid, state)
        def saved(run):
            self.run = run
            self.states.clear()
            self.finished(uid, state)
            self.changed()
            self._advance()
        self._write(self.storage.library.finish_model_conversation_attempt, uid, message,
                    self._end or ('paused' if self._pause and status == 'complete' else None), done=saved)
