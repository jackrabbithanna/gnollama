"""Image generation as the OpenAI-shaped image APIs and chat carry it: request bodies and errors."""
import json
from gettext import gettext as _

from ..image_generation import OPTIONS
from . import systemone
from .openai import image_url
from .transport import ProviderError


def capabilities(references):
    """What a list entry or the details of an image model say of it."""
    return ['image', 'vision'] if references else ['image']


def chosen(options, offered=OPTIONS):
    """The settings to send, refusing one that cannot be: a model's defaults stand for those left out."""
    options = dict(options or {})
    for name in options:
        if name not in offered:
            raise ProviderError(_('This server does not accept: {0}.').format(name))
    return options


def urls(references):
    """Reference images as data URLs, whether they came bare or as data URLs."""
    return [image_url(image) for image in references or ()]


def images_body(model, prompt, options, references=None, **fields):
    """An images API request; `fields` go beside the settings, and references as image parts."""
    body = {'model': model, 'prompt': prompt, **fields, **chosen(options)}
    if references:
        body['input_references'] = [{'type': 'image_url', 'image_url': {'url': url}} for url in urls(references)]
    return body


def chat_body(model, prompt, references=None):
    """A chat request that asks for images, the references going with the prompt as image parts."""
    content = ([{'type': 'text', 'text': prompt}] + [{'type': 'image_url', 'image_url': {'url': url}}
                                                     for url in urls(references)]) if references else prompt
    return {'model': model, 'messages': [{'role': 'user', 'content': content}], 'modalities': ['text', 'image'],
            'stream': False}


def _issue(issue):
    path = '.'.join(str(part) for part in issue.get('path') or [] if isinstance(part, (str, int)))
    return f'{path}: {issue["message"]}' if path else issue['message']


def readable(detail):
    """An error detail with its message naming the setting it is about.

    Validation errors come as a list of issues in JSON text, or as one message beside the `param` it is about.
    """
    if not isinstance(detail, dict) or not isinstance(detail.get('message'), str):
        return detail
    message = detail['message']
    try:
        issues = json.loads(message)
    except (ValueError, RecursionError):
        issues = None
    if isinstance(issues, list) and issues and all(
            isinstance(issue, dict) and isinstance(issue.get('message'), str) for issue in issues):
        message = '\n'.join(_issue(issue) for issue in issues)
    elif isinstance(detail.get('param'), str) and detail['param']:
        message = f'{detail["param"]}: {message}'
    return dict(detail, message=message)


def post(provider, address, body, timeout, cancellable):
    """Send one image request as `provider` sends its other requests; returns the answer as it came."""
    def explained(status, detail):
        return systemone.explain(status, provider._explain(status, readable(detail)))
    return provider.transport.request(address, lambda: body, 'POST', timeout, cancellable,
                                      prepare=provider._prepare, explain=explained)
