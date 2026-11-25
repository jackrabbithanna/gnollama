"""Image runs, one at a time: checked, saved, sent, and saved again; no widgets."""
from gettext import gettext as _
import copy
import uuid
from dataclasses import dataclass, field, replace
from typing import Any
from gi.repository import Gio, GLib
from .image_generation import ImageParameters, check_request, parse_result, unavailable, with_defaults
from .image_store import session_title
from .providers import RequestCancelled, validate_url
from .records import ImageSettings, ModelTarget

# The formats references are sent in. Every image API takes PNG and JPEG; WebP, GIF and the rest that can be
# attached are not taken by all of them, so they are made PNG rather than refused by one model and not another.
REFERENCE_FORMATS = ('png', 'jpeg')


@dataclass
class ImageRequest:
    """A run being sent. The window reads `connection` and `settings.target.model` to see which model is busy."""
    connection: Any
    # Its options as they are sent: the model's PNG default applied.
    settings: ImageSettings
    prompt: str
    references: list
    parameters: ImageParameters
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    cancellable: Gio.Cancellable = field(default_factory=Gio.Cancellable)


def prepare(context, host, settings, prompt, references=(), entry=None):
    """A request ready to send, or ValueError saying what has to change first.

    `host` is the host record `settings.target` names, and `entry` the model's
    entry in that host's list of image models.
    """
    target = settings.target
    if host is None or target is None or target.host_id != host['id']:
        raise ValueError(_('Select an available model before sending.'))
    connection = context.connection(host)
    reason = unavailable(connection.capabilities)
    if reason:
        raise ValueError(reason)
    if not target.model:
        raise ValueError(_('Select an available model before sending.'))
    parameters = connection.image_parameters(entry)
    reason = check_request(prompt, references, settings.options, connection.capabilities, parameters)
    if reason:
        raise ValueError(reason)
    resolved = replace(settings, options=with_defaults(settings.options, parameters), target=ModelTarget(
        host['id'], validate_url(host['hostname']), target.model, host.get('name')))
    return ImageRequest(connection, resolved, prompt, copy.deepcopy(list(references)), parameters)


class ImageController:
    """A session's runs. Transitions happen on the main context; workers send and save.

    `started` is called with a run once it is saved, and `finished` with the same
    run once its end is saved; `changed` after anything a view shows has changed.
    `convert(images, formats)` returns the images in the named formats. It is called
    on a worker before a run with references is saved, and comes from a view, which
    has the means to read images.
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

    def start(self, host, settings, prompt, references=(), entry=None, draft_id=None, draft_revision=None):
        """Send one run and return its request, or None while another is in flight or there is no prompt.

        Raises ValueError where `prepare` does. The first run creates the session.
        """
        if self.request is not None or not prompt.strip():
            return None
        request = prepare(self.context, host, settings, prompt, references, entry)
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
                self.title = session_title(prompt)
            self.started(saved)
            self.context.services.inference.submit(self._send, request)

        def create():
            run = dict(request.settings.to_stored(), id=request.id, session_id=self.session_id or str(uuid.uuid4()),
                       new_session=new, prompt=prompt, references=copy.deepcopy(request.references),
                       draft_id=draft_id, draft_revision=draft_revision)
            self._write(self.storage.library.create_image_run, run, done=created)

        def converted(references):
            if request.cancellable.is_cancelled():
                # Stopped before anything was saved or sent.
                self.request = None
                self.changed()
            else:
                request.references = references
                create()
            return False

        def work():
            try:
                references = self.convert(request.references, REFERENCE_FORMATS)
            except Exception:
                # Sent as they are; the server says what it does not take.
                references = request.references
            GLib.idle_add(converted, references)
        if self.convert and request.references:
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
                raw = request.connection.generate_image(target.model, request.prompt, request.settings.options,
                                                        references=request.references or None,
                                                        cancellable=request.cancellable)
            result = parse_result(raw)
        except Exception as exc:
            result, error = None, exc
        GLib.idle_add(self._sent, request, result, error)

    def _sent(self, request, result, error):
        # Read here, on the main context: a cancellation made while the answer was on its way still counts,
        # unless images came. They were made and paid for, so a Stop pressed too late keeps them.
        cancelled = request.cancellable.is_cancelled() or isinstance(error, RequestCancelled)
        if cancelled and not (error is None and result is not None and result.images):
            status, result, error = 'cancelled', None, None
        elif error is not None:
            status = 'failed'
        elif not result.images:
            # Whatever the model wrote instead is kept with the run.
            status, error = 'failed', _('The model answered without an image.')
        else:
            status = 'done'
        library = self.storage.library

        def finish():
            try:
                return library.finish_image_run(request.id, status, result, None if error is None else str(error))
            except ValueError:
                # An image that is not base64 saves nothing; the run ends without what came.
                return library.finish_image_run(request.id, 'failed', None,
                                                _('The server returned an image that cannot be read.'))

        def saved(run):
            self.request = None
            # None where the session was deleted in the meantime.
            if run is not None:
                self.finished(run)
            self.changed()
        self._write(finish, done=saved)
        return False

    def stop(self):
        if self.request is not None:
            self.request.cancellable.cancel()
