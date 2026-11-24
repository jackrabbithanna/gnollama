"""OpenRouter: the OpenAI-compatible chat API at one address, with a catalog that describes each model."""
from dataclasses import dataclass, replace
from gettext import gettext as _

from .base import Capabilities
from .openai import OpenAICompatibleProvider, effort, find, fingerprint, table
from .transport import ProviderError

URL = 'https://openrouter.ai/api'

# The thinking settings as `reasoning.effort` takes them. On has no effort of its own.
EFFORTS = {False: 'none', 'low': 'low', 'medium': 'medium', 'high': 'high', 'max': 'max'}

# What a model's `supported_parameters` must list for each capability to stand, and for each
# sampling option to be sent: every name of a tuple, or any name of a set.
FLAG_PARAMETERS = dict(sampling_extras=('top_k', 'min_p'), logprobs=('logprobs',),
                       structured_output={'structured_outputs', 'response_format'}, tools=('tools',),
                       thinking_levels={'reasoning', 'reasoning_effort'},
                       thinking_text={'include_reasoning', 'reasoning'})
OPTION_PARAMETERS = dict(temperature=('temperature',), top_p=('top_p',), seed=('seed',), stop=('stop',),
                         num_predict={'max_tokens', 'max_completion_tokens'})


def _listed(names, parameters):
    return bool(names & parameters) if isinstance(names, set) else parameters.issuperset(names)


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
    offered = (('vision', 'image' in (table(model.get('architecture')).get('input_modalities') or [])),
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
    capabilities = Capabilities(thinking_levels=True, structured_output=True, tools=True, logprobs=True,
                                sampling_extras=True, thinking_text=True)

    @classmethod
    def check_url(cls, url):
        if url != cls.fixed_url:
            raise ValueError(_('OpenRouter requires {0}.').format(cls.fixed_url))

    def _address(self, endpoint):
        if self.url != self.fixed_url:
            raise ProviderError(_('OpenRouter requires {0}.').format(self.fixed_url))
        return super()._address(endpoint)

    def model_capabilities(self, details):
        parameters = details.get('supported_parameters') if isinstance(details, dict) else None
        if not isinstance(parameters, list) or not parameters:
            return self.capabilities  # The catalog said nothing about this model.
        parameters = {name for name in parameters if isinstance(name, str)}
        own = self.capabilities
        # Only ever narrower: a flag the type lacks stays off whatever the model lists.
        flags = {flag: getattr(own, flag) and _listed(names, parameters) for flag, names in FLAG_PARAMETERS.items()}
        return replace(own, **flags, left_out=tuple(
            option for option, names in OPTION_PARAMETERS.items() if not _listed(names, parameters)))

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

    def list_models(self, timeout=10, cancellable=None):
        def tag(model):
            slug = model.get('canonical_slug')
            return {'name': model['id'], **({'digest': fingerprint(slug)} if isinstance(slug, str) and slug else {})}
        return [tag(model) for model in self._models(timeout, cancellable)]

    def show_model(self, name, timeout=10, cancellable=None):
        # The catalog has no entry for one model, so the whole list is read again.
        return describe(find(self._models(timeout, cancellable), 'id', name))
