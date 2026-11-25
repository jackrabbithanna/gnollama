"""Decision runs, one at a time: checked, saved, sent, and saved again; no widgets."""
from gettext import gettext as _
import copy
import uuid
from dataclasses import dataclass, field, replace
from typing import Any
from gi.repository import Gio, GLib
from .decision import check_request, parse_questions, parse_result, questions_object
from .decision_store import session_title
from .providers import RequestCancelled, validate_url
from .records import DecisionSettings, ModelTarget
from .structured import parse_json


@dataclass
class DecisionRequest:
    """A run being sent. The window reads `connection` and `settings.target.model` to see which model is busy."""
    connection: Any
    settings: DecisionSettings
    questions: list
    # What is sent: the text as entered, or the JSON it holds.
    state: Any
    text: str
    images: list
    # The image formats the host takes, by name; empty where it reads whatever it is sent.
    formats: tuple = ()
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    cancellable: Gio.Cancellable = field(default_factory=Gio.Cancellable)


def read_state(text, mode):
    """What is sent as the state: the text as entered, or the JSON it holds."""
    if mode != 'json':
        return text
    try:
        return parse_json(text)
    except (ValueError, RecursionError) as exc:
        raise ValueError(_('The state is not valid JSON: {0}').format(exc)) from exc


def prepare(context, host, settings, text, images=(), entry=None):
    """A request ready to send, or ValueError saying what has to change first.

    `host` is the host record `settings.target` names, and `entry` the model's
    entry in that host's list of decision models.
    """
    target = settings.target
    if host is None or target is None or target.host_id != host['id']:
        raise ValueError(_('Select an available model before sending.'))
    connection = context.connection(host)
    capabilities = connection.decision_capabilities(entry)
    reason = check_request((), (), capabilities)
    if reason:
        raise ValueError(reason)
    if not target.model:
        raise ValueError(_('Select an available model before sending.'))
    questions = parse_questions(settings.questions)
    reason = check_request(questions, images, capabilities)
    if reason:
        raise ValueError(reason)
    state = read_state(text, settings.state_mode)
    resolved = replace(settings, questions=questions_object(questions), target=ModelTarget(
        host['id'], validate_url(host['hostname']), target.model, host.get('name')))
    return DecisionRequest(connection, resolved, questions, state, text, copy.deepcopy(list(images)),
                           capabilities.decision_image_formats)


class DecisionController:
    """A session's runs. Transitions happen on the main context; workers send and save.

    `started` is called with a run once it is saved, and `finished` with the same
    run once its end is saved; `changed` after anything a view shows has changed.
    `convert(images, formats)` returns the images in formats a host takes. It is
    called on a worker before a run for such a host is saved, and comes from a
    view, which has the means to read images.
    """
    def __init__(self, context, changed=lambda: None, started=lambda run: None, finished=lambda run: None,
                 convert=None):
        self.context = context
        self.storage = context.storage
        self.changed, self.started, self.finished = changed, started, finished
        self.convert = convert
        self.session_id = None
        self.title = ''
        self.request = None
        self.error = ''

    def load(self, saved):
        self.session_id, self.title = saved['id'], saved['title']

    def _write(self, fn, *args, done):
        result: list = []
        def work():
            result[:] = [fn(*args)]
        self.storage.submit(work, on_done=lambda: done(result[0]))

    def start(self, host, settings, text, images=(), entry=None, draft_id=None, draft_revision=None):
        """Send one run and return its request, or None while another is in flight or there is no state.

        Raises ValueError where `prepare` does. The first run creates the session.
        """
        if self.request is not None or not (text.strip() or images):
            return None
        request = prepare(self.context, host, settings, text, images, entry)
        self.request, self.error = request, ''
        new = self.session_id is None
        self.changed()

        def created(saved):
            if saved is None:
                # The session was deleted while this was on its way; nothing brings it back.
                self.request, self.error = None, _('Conversation no longer exists.')
                self.changed()
                return
            if new:
                self.session_id = saved['session_id']
                self.title = session_title(text, request.settings.state_mode, request.settings.questions)
            self.started(saved)
            self.context.services.inference.submit(self._send, request)

        def create():
            run = dict(request.settings.to_stored(), id=request.id, session_id=self.session_id or str(uuid.uuid4()),
                       new_session=new, state=text, images=copy.deepcopy(request.images),
                       draft_id=draft_id, draft_revision=draft_revision)
            self._write(self.storage.library.create_decision_run, run, done=created)

        def converted(images):
            if request.cancellable.is_cancelled():
                # Stopped before anything was saved or sent.
                self.request = None
                self.changed()
            else:
                request.images = images
                create()
            return False

        def work():
            try:
                images = self.convert(request.images, request.formats)
            except Exception:
                # Sent as they are; the server says what it does not take.
                images = request.images
            GLib.idle_add(converted, images)
        if self.convert and request.formats and request.images:
            self.context.services.control.submit(work)
        else:
            create()
        return request

    def _send(self, request):
        result, error = None, None
        try:
            if request.cancellable.is_cancelled():
                raise RequestCancelled()
            target = request.settings.target
            with self.context.services.models.using(request.connection, target.model):
                result = request.connection.decide(target.model, request.state, questions_object(request.questions),
                                                   images=request.images or None, cancellable=request.cancellable)
            parse_result(result, request.questions)
        except Exception as exc:
            result, error = None, exc
        GLib.idle_add(self._sent, request, result, error)

    def _sent(self, request, result, error):
        # Read here, on the main context: a cancellation made while the answer was on its way still counts.
        if request.cancellable.is_cancelled() or isinstance(error, RequestCancelled):
            status, result, error = 'cancelled', None, None
        elif error is not None:
            status = 'failed'
        else:
            status = 'done'

        def saved(run):
            self.request = None
            # None where the session was deleted in the meantime.
            if run is not None:
                self.finished(run)
            self.changed()
        self._write(self.storage.library.finish_decision_run, request.id, status, result,
                    None if error is None else str(error), done=saved)
        return False

    def stop(self):
        if self.request is not None:
            self.request.cancellable.cancel()
