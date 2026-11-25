"""OpenRouter: the OpenAI-compatible chat API at one address, with a catalog that describes each model."""
from dataclasses import dataclass
from gettext import gettext as _

from . import imaging, systemone
from .base import Capabilities, named
from .openai import OpenAICompatibleProvider, effort, find, fingerprint, narrowed, table
from .transport import ProviderError

URL = 'https://openrouter.ai/api'

# The thinking settings as `reasoning.effort` takes them. On has no effort of its own.
EFFORTS = {False: 'none', 'low': 'low', 'medium': 'medium', 'high': 'high', 'max': 'max'}


def _extracted():
    """Marks text that is translated where it is displayed, not here."""
    _('OpenRouter')


def thinking_levels(reasoning, parameters):
    """The thinking settings to offer, or None for a model that does not think."""
    if not isinstance(reasoning, dict):
        return [False, True] if {'reasoning', 'reasoning_effort'} & set(parameters) else None
    efforts = reasoning.get('supported_efforts')
    levels: list
    if 'supported_efforts' not in reasoning:
        levels = [False, True]  # No choice of effort.
    elif not isinstance(efforts, list):
        levels = list(EFFORTS)  # Null: every effort is accepted.
    else:
        levels = [level for level, name in EFFORTS.items() if name in efforts]
        if 'xhigh' in efforts and 'max' not in levels:
            levels.append('max')  # The highest this model has; the service takes the nearest effort.
    # A model that must think rejects being told not to.
    return [level for level in levels if level is not False or reasoning.get('mandatory') is not True]


def describe(model):
    parameters = [name for name in model.get('supported_parameters') or [] if isinstance(name, str)]
    reasoning = model.get('reasoning')
    levels = thinking_levels(reasoning, parameters)
    offered = (('vision', sees(model)),
               ('tools', 'tools' in parameters), ('thinking', levels is not None))
    # The catalog answers for every model, so a capability not listed here is one it lacks.
    result = {'capabilities': ['completion'] + [name for name, has in offered if has],
              'supported_parameters': parameters, 'reasoning': reasoning}
    if levels is not None:
        result['thinking_levels'] = levels
    limits = dict(context_length=model.get('context_length'),
                  max_completion_tokens=table(model.get('top_provider')).get('max_completion_tokens'))
    result.update({key: value for key, value in limits.items() if type(value) is int})
    # US dollars per token, as the decimal strings the catalog gives. Its other entries are not prices.
    prices = {key: value for key, value in table(model.get('pricing')).items() if isinstance(value, str)}
    if prices:
        result['pricing'] = prices
    return result


def sees(model):
    return 'image' in (table(model.get('architecture')).get('input_modalities') or [])


def tag(model):
    slug = model.get('canonical_slug')
    return {'name': model['id'], **({'digest': fingerprint(slug)} if isinstance(slug, str) and slug else {})}


def context(model):
    length = model.get('context_length')
    return length if type(length) is int and length > 0 else None


def describe_embedding(model):
    result: dict = {'capabilities': ['embedding', 'vision'] if sees(model) else ['embedding']}
    if context(model):
        result['context_length'] = context(model)
    prices = {key: value for key, value in table(model.get('pricing')).items() if isinstance(value, str)}
    return dict(result, pricing=prices) if prices else result


def describe_decision(model):
    return dict(describe_embedding(model), capabilities=systemone.capabilities(sees(model)))


def image_settings(model):
    """The settings an image model takes, in the shape `image_generation.parameters_from_schema` reads."""
    return {name: spec for name, spec in table(model.get('supported_parameters')).items() if isinstance(spec, dict)}


def describe_image(model):
    settings = image_settings(model)
    most = table(settings.get('input_references')).get('max')
    result = {'capabilities': imaging.capabilities(type(most) is int and most > 0), 'image_parameters': settings}
    return dict(result, image_streaming=True) if model.get('supports_streaming') is True else result


@dataclass(frozen=True)
class OpenRouterProvider(OpenAICompatibleProvider):
    kind = 'openrouter'
    label = default_name = name_example = 'OpenRouter'
    url_example = ''  # The address is fixed.
    key_url = 'https://openrouter.ai/keys'
    fixed_url = URL
    requires_key = True
    key_optional = False
    # Not every model takes every option; the service leaves out what a model's provider lacks.
    capabilities = Capabilities(embeddings=True, thinking_levels=True, structured_output=True, tools=True,
                                logprobs=True, sampling_extras=True, thinking_text=True, image_embeddings=True,
                                # Few models answer a box question; the service says so for one that does not.
                                decisions=True, decision_images=True, decision_boxes=True,
                                decision_image_formats=systemone.ASSET_FORMATS,
                                image_generation=True, image_references=True)

    @classmethod
    def check_url(cls, url):
        if url != cls.fixed_url:
            raise ValueError(_('OpenRouter requires {0}.').format(cls.fixed_url))

    def _address(self, endpoint):
        if self.url != self.fixed_url:
            raise ProviderError(_('OpenRouter requires {0}.').format(self.fixed_url))
        return super()._address(endpoint)

    def model_capabilities(self, details):
        return narrowed(self.capabilities, details)

    def request_body(self, *settings, **named):
        # Left to itself the service may route to an upstream provider that lacks an option sent, which
        # ignores it and does not say so. This makes it choose one that takes them all, or refuse.
        return {**super().request_body(*settings, **named), 'provider': {'require_parameters': True}}

    def thinking_fields(self, thinking):
        return {'reasoning': {'enabled': True} if thinking is True else {'effort': effort(thinking, EFFORTS)}}

    def _models(self, timeout, cancellable):
        def chats(model):
            outputs = table(model.get('architecture')).get('output_modalities')
            return not isinstance(outputs, list) or 'text' in outputs
        return [model for model in self._listed(lambda: self._address('/models'), 'data', 'id', timeout, cancellable)
                if chats(model)]

    def _embedders(self, timeout, cancellable):
        # Models that embed have a list of their own. It has come whole; a further page would not be read.
        return self._listed(lambda: self._address('/embeddings/models'), 'data', 'id', timeout, cancellable)

    def _deciders(self, timeout, cancellable):
        # Decision models have a list of their own as well.
        return self._listed(lambda: self._address('/models') + '?output_modalities=decisions', 'data', 'id',
                            timeout, cancellable)

    def list_decision_models(self, timeout=10, cancellable=None):
        return [dict(tag(model), capabilities=systemone.capabilities(sees(model)),
                     **({'context_length': context(model)} if context(model) else {}))
                for model in self._deciders(timeout, cancellable)]

    def decide(self, model, state, questions, images=None, keep_alive=None, timeout=120, cancellable=None):
        """Ask a decision model the named `questions` about `state`; returns the server's answer as it came."""
        if keep_alive is not None:
            raise ProviderError(_('This server does not accept: {0}.').format('keep_alive'))
        data = systemone.assets_body(systemone.request(model, state, questions), images)
        return systemone.post(self, lambda: self._address('/systemone'), data, timeout, cancellable)

    def _imagers(self, timeout, cancellable):
        # Image models have one too, the models among them that also write text included.
        return self._listed(lambda: self._address('/images/models'), 'data', 'id', timeout, cancellable)

    def list_image_models(self, timeout=10, cancellable=None):
        # The list gives no slug; when the model was added stands in for it.
        return [dict({'digest': fingerprint(model['id'], model.get('created'))}, **tag(model), **describe_image(model))
                for model in self._imagers(timeout, cancellable)]

    def generate_image(self, model, prompt, options, references=None, timeout=300, cancellable=None):
        """Ask an image model for images of `prompt`; returns the server's answer as it came."""
        fields = {'response_format': 'b64_json', 'provider': {'require_parameters': True}}
        body = imaging.images_body(model, prompt, options, references, **fields)
        return imaging.post(self, lambda: self._address('/images'), body, timeout, cancellable)

    def list_models(self, timeout=10, cancellable=None):
        return [tag(model) for model in self._models(timeout, cancellable)]

    def list_embedding_models(self, timeout=10, cancellable=None):
        return [dict(tag(model), capabilities=describe_embedding(model)['capabilities'],
                     **({'context_length': context(model)} if context(model) else {}))
                for model in self._embedders(timeout, cancellable)]

    def model_tag(self, name, timeout=10, cancellable=None):
        # The embedding list first: it is the short one, and a collection asks about a model on it.
        return (named(self.list_embedding_models(timeout, cancellable), name)
                or named(self.list_models(timeout, cancellable), name))

    def embedding_context(self, name, timeout=10, cancellable=None):
        return context(find(self._embedders(timeout, cancellable), 'id', name))

    def show_model(self, name, timeout=10, cancellable=None):
        # The catalog has no entry for one model, so the whole list is read again.
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
