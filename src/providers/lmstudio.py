"""LM Studio: the OpenAI-compatible chat API, and model details from its own API."""
from dataclasses import dataclass
from gettext import gettext as _

from .base import Capabilities
from .openai import OpenAICompatibleProvider, effort, find, fingerprint, table
from .transport import validate_url

# The thinking settings as `reasoning_effort` takes them; it refuses "on" and "off".
EFFORTS = {False: 'none', True: 'medium', 'low': 'low', 'medium': 'medium', 'high': 'high', 'max': 'xhigh'}
# A model's `allowed_options`, as thinking settings. Any other is not offered.
LEVELS = {'off': False, 'on': True, 'low': 'low', 'medium': 'medium', 'high': 'high'}


def _extracted():
    """Marks text that is translated where it is displayed, not here."""
    _('LM Studio')


def details(model):
    """What the model manager shows, under the names Ollama gives them."""
    found = {'format': model.get('format'), 'family': model.get('architecture'),
             'parameter_size': model.get('params_string'),
             'quantization_level': table(model.get('quantization')).get('name')}
    return {key: value for key, value in found.items() if isinstance(value, str) and value}


def tag(model):
    size = model.get('size_bytes')
    entry = {'name': model['key'], 'details': details(model),
             'digest': fingerprint(model['key'], model.get('selected_variant'),
                                   table(model.get('quantization')).get('name'), size)}
    return dict(entry, size=size) if type(size) is int else entry


def describe(model):
    """The details of one listed model, saying only what the server said."""
    result: dict = {'details': details(model)}
    if model.get('type') == 'embedding':
        result['capabilities'] = ['embedding']
    reported = table(model.get('capabilities'))
    # Training for tool use is advice for the user; a model without it still takes tools.
    known = {name: reported[field] for name, field in (('vision', 'vision'), ('tools', 'trained_for_tool_use'))
             if isinstance(reported.get(field), bool)}
    if known:
        result['supports'] = known
    options = table(reported.get('reasoning')).get('allowed_options')
    if isinstance(options, list):
        result['thinking_levels'] = [LEVELS[option] for option in options
                                     if isinstance(option, str) and option in LEVELS]
    loaded = model.get('loaded_instances')
    lengths = dict(context_length=table(table(loaded[0]).get('config')).get('context_length')
                   if isinstance(loaded, list) and loaded else None,
                   max_context_length=model.get('max_context_length'))
    # A model loads with less than its maximum, and the loaded size is what limits a request.
    result.update({key: value for key, value in lengths.items() if type(value) is int})
    return result


@dataclass(frozen=True)
class LMStudioProvider(OpenAICompatibleProvider):
    kind = 'lmstudio'
    label = default_name = name_example = 'LM Studio'
    default_url = url_example = 'http://localhost:1234'
    capabilities = Capabilities(thinking_levels=True, structured_output=True, tools=True, logprobs=True,
                                sampling_extras=True, thinking_text=True)
    # The server refuses the `json_object` type.
    json_mode = {'type': 'json_schema', 'json_schema': {'name': 'response', 'schema': {'type': 'object'}}}

    def thinking_fields(self, thinking):
        return {'reasoning_effort': effort(thinking, EFFORTS)}

    def _models(self, timeout, cancellable):
        def address():
            return validate_url(self.url).removesuffix('/v1') + '/api/v1/models'
        # A decision model is another kind of model, which nothing here can use.
        return [model for model in self._listed(address, 'models', 'key', timeout, cancellable)
                if model.get('type') != 'decision']

    def list_models(self, timeout=10, cancellable=None):
        return [tag(model) for model in self._models(timeout, cancellable)]

    def show_model(self, name, timeout=10, cancellable=None):
        # The list is the only source, so it is read again; loading a model changes what it says.
        return describe(find(self._models(timeout, cancellable), 'key', name))
