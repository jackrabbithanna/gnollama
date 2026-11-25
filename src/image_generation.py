"""Image generation settings, requests, and answers; no GTK and no network."""
from gettext import gettext as _, ngettext
import base64
import binascii
import copy
import math
from dataclasses import dataclass, field

from .structured import parse_json

# The settings the application offers, in the order they are shown. A model may list others; they are not sent.
OPTIONS = ('aspect_ratio', 'resolution', 'quality', 'output_format', 'background', 'n', 'seed')
# Their names as shown; untranslated here, translated where they are displayed.
LABELS = {'aspect_ratio': 'Aspect ratio', 'resolution': 'Resolution', 'quality': 'Quality',
          'output_format': 'Output format', 'background': 'Background', 'n': 'Number of images', 'seed': 'Seed'}
# The largest seed offered; every known service takes an unsigned 32-bit one.
SEED_LIMIT = 2 ** 32 - 1

# How the bytes of each image format begin, for an image that comes without its type.
MAGIC = ((b'\x89PNG', 'image/png'), (b'\xff\xd8\xff', 'image/jpeg'), (b'GIF8', 'image/gif'))
EXTENSIONS = {'image/png': 'png', 'image/jpeg': 'jpg', 'image/webp': 'webp', 'image/gif': 'gif',
              'image/svg+xml': 'svg'}


def _extracted():
    """Marks text that is translated where it is displayed, not here."""
    _('Aspect ratio')
    _('Resolution')
    _('Quality')
    _('Output format')
    _('Background')
    _('Number of images')
    _('Seed')


@dataclass(frozen=True)
class Parameter:
    """One setting a model takes."""
    name: str
    # `choice` among `values`, an integer `range` from `low` to `high`, or any `integer` from 0 up.
    kind: str
    values: tuple = ()
    low: int = 0
    high: int = 0

    def accepts(self, value):
        if self.kind == 'choice':
            return isinstance(value, str) and value in self.values
        if type(value) is not int:
            return False
        if self.kind == 'range':
            return self.low <= value <= self.high
        return 0 <= value <= SEED_LIMIT


@dataclass(frozen=True)
class ImageParameters:
    """What one image model takes: its settings in the order of OPTIONS, and how many reference images."""
    options: tuple = ()
    references: int = 0
    streaming: bool = False

    def get(self, name):
        return next((parameter for parameter in self.options if parameter.name == name), None)

    def count(self):
        """The fewest and most images one request may ask for."""
        n = self.get('n')
        return (n.low, n.high) if n else (1, 1)


@dataclass(frozen=True)
class GeneratedImage:
    data: str  # base64, without a data URL's prefix
    media_type: str


@dataclass(frozen=True)
class ImageResult:
    images: tuple
    # What a model that also writes text said beside its images.
    text: str = ''
    cost: float | None = None
    # Fields this version does not read, including the addresses of images that came as links.
    extra: dict = field(default_factory=dict)


def _bound(value):
    return value if type(value) is int else None


def parameters_from_schema(schema, references=True, streaming=False):
    """The settings of a model from a map of names to specs: `{type: enum, values}`,
    `{type: range, min, max}`, or `{type: boolean}` where a value is taken without limits.

    A spec that cannot be read leaves its setting out; nothing is offered that may be refused.
    """
    schema = schema if isinstance(schema, dict) else {}
    options = []
    for name in OPTIONS:
        spec = schema.get(name)
        if not isinstance(spec, dict):
            continue
        kind = spec.get('type')
        if kind == 'enum' and name != 'n':
            values = tuple(dict.fromkeys(value for value in spec.get('values') or [] if isinstance(value, str) and value))
            if values:
                options.append(Parameter(name, 'choice', values))
        elif kind == 'range' and name in ('n', 'seed'):
            low, high = _bound(spec.get('min')), _bound(spec.get('max'))
            if low is not None and high is not None and 0 <= low <= high and (name != 'n' or low >= 1):
                options.append(Parameter(name, 'range', low=low, high=high))
        elif kind == 'boolean' and name == 'seed':
            options.append(Parameter(name, 'integer'))
    spec = schema.get('input_references')
    most = _bound(spec.get('max')) if isinstance(spec, dict) and spec.get('type') == 'range' else None
    return ImageParameters(tuple(options), most if references and most and most > 0 else 0, bool(streaming))


def unavailable(capabilities):
    """Why a host offers no image models, or ''."""
    if capabilities is None or not capabilities.image_generation:
        return _('This server does not offer image generation.')
    return ''


def check_request(prompt, references, options, capabilities, parameters):
    """The reason a request cannot be sent to a host and model, or ''.

    `options` holds the settings chosen, by name; a setting left at the model's default is absent.
    """
    reason = unavailable(capabilities)
    if reason:
        return reason
    if not isinstance(prompt, str) or not prompt.strip():
        return _('Enter a prompt.')
    references = list(references or ())
    if references and (not capabilities.image_references or not parameters.references):
        return _('This model does not take reference images.')
    if len(references) > parameters.references:
        return ngettext('This model takes at most {0} reference image.',
                        'This model takes at most {0} reference images.', parameters.references).format(
                            parameters.references)
    for name, value in (options or {}).items():
        parameter = parameters.get(name)
        label = _(LABELS[name]) if name in LABELS else name
        if parameter is None:
            return _('This model does not take the setting {0}.').format(label)
        if not parameter.accepts(value):
            return _('This model does not take that value for {0}.').format(label)
    return ''


def with_defaults(options, parameters):
    """The settings to send: those chosen, and PNG output where the model offers it and none was chosen.

    A model's own default may be SVG or WebP, which not every system can show.
    """
    options = dict(options or {})
    offered = parameters.get('output_format')
    if 'output_format' not in options and offered and 'png' in offered.values:
        options['output_format'] = 'png'
    return options


def media_type(data):
    """The type of an image from how its base64 begins; PNG where it cannot be told."""
    try:
        head = base64.b64decode(data[:32] + '=' * (-len(data[:32]) % 4))
    except (ValueError, binascii.Error):
        head = b''
    if head[:4] == b'RIFF' and head[8:12] == b'WEBP':
        return 'image/webp'
    if head.lstrip()[:5] in (b'<?xml', b'<svg ') or head.lstrip()[:4] == b'<svg':
        return 'image/svg+xml'
    return next((kind for magic, kind in MAGIC if head.startswith(magic)), 'image/png')


def extension(kind):
    """The file name ending for an image of a media type."""
    return EXTENSIONS.get(kind, 'png')


def _image(url, kind=None):
    """An image from base64 or a data URL, or None for a link to fetch or anything else."""
    if not isinstance(url, str) or not url:
        return None
    if url.startswith('data:'):
        head, _sep, data = url.partition(',')
        if not data or ';base64' not in head:
            return None
        kind = kind or head[5:].split(';', 1)[0] or None
        url = data
    elif url.startswith(('http://', 'https://')):
        return None
    return GeneratedImage(url, kind if isinstance(kind, str) and kind.startswith('image/') else media_type(url))


def _cost(value):
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return None
    if type(value) not in (int, float):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None


def parse_result(raw):
    """A host's answer, from an images API (`data`) or a chat answer (`choices[0].message.images`).

    Raises ValueError for an answer that is not an object. An answer without images
    is returned as one, for the caller to report with whatever text it holds.
    """
    if isinstance(raw, str):
        try:
            raw = parse_json(raw)
        except RecursionError:
            raw = None
    if not isinstance(raw, dict):
        raise ValueError(_('The server did not return an image.'))
    images, links, text = [], [], ''
    known = {'data', 'choices', 'usage'}
    for item in raw.get('data') if isinstance(raw.get('data'), list) else []:
        if not isinstance(item, dict):
            continue
        image = _image(item.get('b64_json'), item.get('media_type')) or _image(item.get('url'), item.get('media_type'))
        if image:
            images.append(image)
        elif isinstance(item.get('url'), str):
            links.append(item['url'])
    choices = raw.get('choices')
    message = choices[0].get('message') if isinstance(choices, list) and choices and isinstance(choices[0], dict) else None
    if isinstance(message, dict):
        text = message.get('content') if isinstance(message.get('content'), str) else ''
        for item in message.get('images') or []:
            url = item.get('image_url') if isinstance(item, dict) else None
            url = url.get('url') if isinstance(url, dict) else url
            image = _image(url)
            if image:
                images.append(image)
            elif isinstance(url, str):
                links.append(url)
    usage = raw.get('usage') if isinstance(raw.get('usage'), dict) else {}
    # Vercel's images API names it in camel case.
    metadata = raw.get('provider_metadata') or raw.get('providerMetadata')
    gateway = metadata.get('gateway') if isinstance(metadata, dict) else None
    cost = _cost(usage.get('cost'))
    if cost is None and isinstance(gateway, dict):
        cost = _cost(gateway.get('cost'))
    extra = {key: copy.deepcopy(item) for key, item in raw.items() if key not in known}
    if usage:
        extra['usage'] = copy.deepcopy(usage)
    if links:
        extra['links'] = links
    return ImageResult(tuple(images), text.strip(), cost, extra)
