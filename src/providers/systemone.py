"""The System One decision API: one request of state and named questions, as each server carries it."""
import base64
import copy
import json
import re
import uuid
from gettext import gettext as _

from .transport import ProviderError, error_detail, error_text

# The images a server takes as assets, by how their bytes begin.
FORMATS = ((b'\x89PNG', 'png'), (b'\xff\xd8\xff', 'jpeg'))
# Their names, as a provider that sends assets declares them.
ASSET_FORMATS = tuple(name for _magic, name in FORMATS)
# The header a gateway demands before it passes on an image or a box question.
IMAGES_HEADER = 'TypeSafe-Danger-Zone-Images'
# An error passed on from another server: its JSON body as text, after the status it came with.
UPSTREAM = re.compile(r'\s*(?:HTTP \d+:\s*)?(\{.*\})\s*', re.S)


def request(model, state, questions):
    """The request every server takes; `questions` is the object `decision.questions_object` returns."""
    return {'model': model, 'state': copy.deepcopy(state), 'questions': copy.deepcopy(questions)}


def bare(image):
    """An image's base64 alone, where it came as a data URL."""
    return image.split(',', 1)[1] if image.startswith('data:') and ',' in image else image


def asks_boxes(questions):
    return any(isinstance(question, dict) and question.get('type') == 'box' for question in questions.values())


def capabilities(vision):
    """What a list entry or the details of a decision model say of it."""
    return ['decision', 'vision'] if vision else ['decision']


def flat_body(request, images, encode=bare):
    """The request with its images in a list of their own, each as `encode` gives it."""
    return dict(request, images=[encode(image) for image in images]) if images else request


def image_kind(image):
    """The format of an image a server takes as an asset, or None for any other."""
    try:
        head = base64.b64decode(bare(image)[:24])
    except ValueError:
        head = b''
    return next((name for magic, name in FORMATS if head.startswith(magic)), None)


def image_format(image):
    name = image_kind(image)
    if name is None:
        raise ProviderError(_('Images sent to this server must be PNG or JPEG.'))
    return name


def assets_body(request, images):
    """The request with each image as an asset, and the state naming every one of them.

    An asset the state does not name is refused, so each placeholder is added to the state: to
    text on lines of their own, to a list as further items, and beside anything else in a list.
    """
    if not images:
        return request
    assets = {f'img-{uuid.uuid4()}': {'type': 'image', 'format': image_format(image), 'data_base64': bare(image)}
              for image in images}
    state = request.get('state')
    if isinstance(state, str) or state is None:
        state = '\n'.join(([state] if state else []) + list(assets))
    elif isinstance(state, list):
        state = state + list(assets)
    else:
        state = [state, *assets]
    return dict(request, state=state, assets=assets)


def explain(status, detail):
    """The words of an error, from inside the JSON of another server's error where it holds one."""
    text = error_text(detail)
    for _depth in range(4):
        match = UPSTREAM.fullmatch(text or '')
        try:
            inner = error_text(error_detail(json.loads(match.group(1)))) if match else None
        except (ValueError, RecursionError):
            inner = None
        if not inner or inner == text:
            break
        text = inner
    return text


def checked(raw):
    """A server's answer, once it is one that `decision.parse_result` can read."""
    if not isinstance(raw, dict) or not isinstance(raw.get('answers'), dict):
        raise ProviderError(_('The response has no answers.'))
    return raw


def post(provider, address, body, timeout, cancellable, prepare=None):
    """Send one decision as `provider` sends its other requests, and return the checked answer."""
    def explained(status, detail):
        return explain(status, provider._explain(status, detail))
    return checked(provider.transport.request(address, lambda: body, 'POST', timeout, cancellable,
                                              prepare=prepare or provider._prepare, explain=explained))
