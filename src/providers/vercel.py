"""Vercel AI Gateway: the OpenAI-compatible chat API at one address, with a public list that describes each model."""
from dataclasses import dataclass
from gettext import gettext as _

from . import imaging, systemone
from .base import Capabilities, named
from .openai import OpenAICompatibleProvider, effort, find, fingerprint, narrowed, table
from .openrouter import EFFORTS
from .transport import ProviderError, error_text

URL = 'https://ai-gateway.vercel.sh'

# The efforts a model may list that the application has a level for, in the order offered.
LEVELS = ('low', 'medium', 'high', 'max')

# The prices kept, by the name the application uses. The units of the others are not known.
PRICES = {'input': 'prompt', 'output': 'completion', 'input_cache_read': 'input_cache_read',
          'input_cache_write': 'input_cache_write'}

# The most images one request to the images API asks for. The gateway takes ten; few models make that many.
IMAGE_COUNT = 4
# The most reference images sent, by route: the documented limit of the edits API, and Gemini's.
REFERENCES = {'images': 16, 'chat': 14}
# The aspect ratios offered; every image model the gateway lists takes these.
ASPECT_RATIOS = ('1:1', '16:9', '9:16', '4:3', '3:4')


def _extracted():
    """Marks text that is translated where it is displayed, not here."""
    _('Vercel AI Gateway')


def thinking_levels(model):
    """The thinking settings to offer, or None for a model that does not think."""
    options = [option for option in model.get('reasoning_options') or [] if isinstance(option, dict)]
    toggle = any(option.get('type') == 'toggle' for option in options)
    efforts = {value for option in options if option.get('type') == 'effort'
               for value in option.get('values') or [] if isinstance(value, str)}
    # Effort `none` is ignored by a model that does not list it, so only a listed way of stopping is Off.
    levels: list = [False] if toggle or 'none' in efforts else []
    levels += [True] if toggle else []
    levels += [level for level in LEVELS if level in efforts]
    if 'xhigh' in efforts and 'max' not in efforts:
        levels.append('max')  # The highest this model has.
    if levels:
        return levels
    parameters = {name for name in model.get('supported_parameters') or [] if isinstance(name, str)}
    return [False, True] if {'reasoning', 'reasoning_effort'} & parameters else None


def prices(model):
    """US dollars per token, as the decimal strings the list gives."""
    return {PRICES[key]: value for key, value in table(model.get('pricing')).items()
            if key in PRICES and isinstance(value, str)}


def tag(model):
    return {'name': model['id'], 'digest': fingerprint(model['id'], model.get('released'))}


def context(model):
    length = model.get('context_window')
    return length if type(length) is int and length > 0 else None


def describe(model):
    parameters = [name for name in model.get('supported_parameters') or [] if isinstance(name, str)]
    levels = thinking_levels(model)
    tags = model.get('tags')
    offered = {'tools': 'tools' in parameters, 'thinking': levels is not None}
    result: dict = {'supported_parameters': parameters}
    if isinstance(tags, list):
        # The list answers for every model with tags, so a capability not listed here is one it lacks.
        offered = {'vision': 'vision' in tags, **offered}
        result['capabilities'] = ['completion'] + [name for name, has in offered.items() if has]
    else:
        result['supports'] = offered  # Without tags, whether it sees images is unknown.
    if levels is not None:
        result['thinking_levels'] = levels
    limits = dict(context_length=model.get('context_window'), max_completion_tokens=model.get('max_tokens'))
    result.update({key: value for key, value in limits.items() if type(value) is int and value > 0})
    if prices(model):
        result['pricing'] = prices(model)
    return result


def describe_embedding(model):
    result: dict = {'capabilities': ['embedding']}
    if context(model):
        result['context_length'] = context(model)
    return dict(result, pricing=prices(model)) if prices(model) else result


def sees(model):
    tags = model.get('tags')
    return isinstance(tags, list) and 'vision' in tags


def describe_decision(model):
    return dict(describe_embedding(model), capabilities=systemone.capabilities(sees(model)))


def tagged(model, name):
    tags = model.get('tags')
    return isinstance(tags, list) and name in tags


def makes_images(model):
    """Image-only models, and chat models that make images too."""
    return model.get('type') == 'image' or (model.get('type') == 'language' and tagged(model, 'image-generation'))


def route(model):
    """Where an image model is asked: the images API, or chat for a model that also writes text."""
    return 'images' if model.get('type') == 'image' else 'chat'


def image_settings(model):
    """The settings an image model takes. The list describes none, so these are the ones every model of a route
    takes: aspect ratio, as `aspect_ratio` on the images API and `image_config` in chat (both seen honoured on
    2026-11-24), and a count on the images API."""
    way = route(model)
    settings: dict = {'aspect_ratio': {'type': 'enum', 'values': list(ASPECT_RATIOS)}}
    if way == 'images':
        settings['n'] = {'type': 'range', 'min': 1, 'max': IMAGE_COUNT}
    inputs = table(model.get('modalities')).get('input')
    # Only images listed as input: `file-input` may mean PDF alone, as it does for Seedream 5.5.
    sees_images = isinstance(inputs, list) and 'image' in inputs
    if tagged(model, 'vision') if way == 'chat' else sees_images:
        settings['input_references'] = {'type': 'range', 'min': 0, 'max': REFERENCES[way]}
    return settings


def describe_image(model):
    settings = image_settings(model)
    result = {'capabilities': imaging.capabilities('input_references' in settings), 'image_parameters': settings,
              'route': route(model)}
    # Image-only models are priced per image, in US dollars.
    image = table(model.get('pricing')).get('image')
    costs = dict(prices(model), **({'image': image} if isinstance(image, str) else {}))
    return dict(result, pricing=costs) if costs else result


@dataclass(frozen=True)
class VercelProvider(OpenAICompatibleProvider):
    kind = 'vercel'
    label = default_name = name_example = 'Vercel AI Gateway'
    url_example = ''  # The address is fixed.
    # The dashboard's own link: it fills in the team of whoever follows it.
    key_url = 'https://vercel.com/d?to=%2F%5Bteam%5D%2F%7E%2Fai-gateway%2Fapi-keys'
    fixed_url = URL
    requires_key = True
    key_optional = False
    # No model lists the sampling extras or logprobs; the gateway takes them and returns nothing.
    capabilities = Capabilities(embeddings=True, thinking_levels=True, structured_output=True, tools=True,
                                thinking_text=True,
                                # Few models answer a box question; the gateway says so for one that does not.
                                decisions=True, decision_images=True, decision_boxes=True,
                                decision_image_formats=systemone.ASSET_FORMATS,
                                image_generation=True, image_references=True)

    @classmethod
    def check_url(cls, url):
        if url != cls.fixed_url:
            raise ValueError(_('Vercel AI Gateway requires {0}.').format(cls.fixed_url))

    def _address(self, endpoint):
        if self.url != self.fixed_url:
            raise ProviderError(_('Vercel AI Gateway requires {0}.').format(self.fixed_url))
        return super()._address(endpoint)

    def _explain(self, status, detail):
        # About credit, a payment method, or the free tier, not the key: the gateway's words say which.
        if status in (402, 403):
            return error_text(detail)
        return super()._explain(status, detail)

    def model_capabilities(self, details):
        return narrowed(self.capabilities, details)

    def thinking_fields(self, thinking):
        return {'reasoning': {'enabled': True} if thinking is True else {'effort': effort(thinking, EFFORTS)}}

    def _catalog(self, kind, timeout, cancellable):
        # Chat and embedding models share one list, with images, video, speech, and others.
        return [model for model in self._listed(lambda: self._address('/models'), 'data', 'id', timeout, cancellable)
                if model.get('type') == kind]

    def _models(self, timeout, cancellable):
        return self._catalog('language', timeout, cancellable)

    def _embedders(self, timeout, cancellable):
        return self._catalog('embedding', timeout, cancellable)

    def _deciders(self, timeout, cancellable):
        return self._catalog('evaluation', timeout, cancellable)

    def list_decision_models(self, timeout=10, cancellable=None):
        return [dict(tag(model), capabilities=systemone.capabilities(sees(model)),
                     **({'context_length': context(model)} if context(model) else {}))
                for model in self._deciders(timeout, cancellable)]

    def decide(self, model, state, questions, images=None, keep_alive=None, timeout=120, cancellable=None):
        """Ask a decision model the named `questions` about `state`; returns the server's answer as it came."""
        if keep_alive is not None:
            raise ProviderError(_('This server does not accept: {0}.').format('keep_alive'))
        data = systemone.assets_body(systemone.request(model, state, questions), images)
        # The gateway refuses an image or a box question without this header, which is sent with nothing else.
        marked = bool(images) or systemone.asks_boxes(questions)

        def prepare(message, cancellable):
            if marked:
                message.get_request_headers().replace(systemone.IMAGES_HEADER, 'true')
            return self._prepare(message, cancellable)

        def address():
            # Beside the API the other requests use, not under it.
            return self._address('').removesuffix('/v1') + '/typesafe/v1/systemone'
        return systemone.post(self, address, data, timeout, cancellable, prepare)

    def _imagers(self, timeout, cancellable):
        return [model for model in self._listed(lambda: self._address('/models'), 'data', 'id', timeout, cancellable)
                if makes_images(model)]

    def list_image_models(self, timeout=10, cancellable=None):
        return [dict(tag(model), **describe_image(model)) for model in self._imagers(timeout, cancellable)]

    def generate_image(self, model, prompt, options, references=None, timeout=300, cancellable=None):
        """Ask an image model for images of `prompt`; returns the server's answer as it came."""
        # Which API a model is asked through is known only from the list.
        if route(find(self._imagers(timeout, cancellable), 'id', model)) == 'chat':
            config = imaging.chosen(options, ('aspect_ratio',))
            body, endpoint = imaging.chat_body(model, prompt, references), '/chat/completions'
            if config:
                body['image_config'] = config
        elif references:
            # Edits take the images as JSON where each is a data URL, and always answer in base64.
            body = {'model': model, 'prompt': prompt, **imaging.chosen(options, ('n', 'aspect_ratio')),
                    'images': [{'image_url': url} for url in imaging.urls(references)]}
            endpoint = '/images/edits'
        else:
            body = {'model': model, 'prompt': prompt, 'response_format': 'b64_json',
                    **imaging.chosen(options, ('n', 'aspect_ratio'))}
            endpoint = '/images/generations'
        return imaging.post(self, lambda: self._address(endpoint), body, timeout, cancellable)

    def list_models(self, timeout=10, cancellable=None):
        return [tag(model) for model in self._models(timeout, cancellable)]

    def list_embedding_models(self, timeout=10, cancellable=None):
        return [dict(tag(model), **({'context_length': context(model)} if context(model) else {}))
                for model in self._embedders(timeout, cancellable)]

    def model_tag(self, name, timeout=10, cancellable=None):
        return (named(self.list_embedding_models(timeout, cancellable), name)
                or named(self.list_models(timeout, cancellable), name))

    def embedding_context(self, name, timeout=10, cancellable=None):
        return context(find(self._embedders(timeout, cancellable), 'id', name))

    def show_model(self, name, timeout=10, cancellable=None):
        # The list has no entry for one model, so the whole list is read again.
        for model in self._models(timeout, cancellable):
            if model['id'] == name:
                return describe(model)
        for model in self._embedders(timeout, cancellable):
            if model['id'] == name:
                return describe_embedding(model)
        for model in self._deciders(timeout, cancellable):
            if model['id'] == name:
                return describe_decision(model)
        return describe_image(find(self._imagers(timeout, cancellable), 'id', name))

    def probe(self, cancellable=None):
        # The model list is public, so only this shows the key was accepted.
        self.transport.request(lambda: self._address('/credits'), None, 'GET', 10, cancellable, **self._hooks())
        return super().probe(cancellable)
