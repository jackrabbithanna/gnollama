"""LM Studio: the OpenAI-compatible chat API, and model details from its own API."""
from dataclasses import dataclass
from gettext import gettext as _

from . import systemone
from .base import Capabilities
from .openai import OpenAICompatibleProvider, effort, find, fingerprint, image_url, table
from .transport import ProviderError, validate_url

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
    if model.get('type') == 'embedding':
        # Said in the list, so a chat model list can leave it out before asking for details.
        entry['capabilities'] = ['embedding']
    return dict(entry, size=size) if type(size) is int else entry


def sees(model):
    """Whether a model takes images, or None where the server does not say."""
    for value in (table(model.get('capabilities')).get('vision'), model.get('has_vision_adapter')):
        if isinstance(value, bool):
            return value
    return None


def lengths(model):
    """The context a model is loaded with, if it is loaded, and the most it can be given."""
    loaded = model.get('loaded_instances')
    found = dict(context_length=table(table(loaded[0]).get('config')).get('context_length')
                 if isinstance(loaded, list) and loaded else None,
                 max_context_length=model.get('max_context_length'))
    return {key: value for key, value in found.items() if type(value) is int and value > 0}


def limit(model):
    """The tokens one input may hold: a model loads with less than its maximum, and then that limits it."""
    found = lengths(model)
    return found.get('context_length') or found.get('max_context_length')


def describe(model):
    """The details of one listed model, saying only what the server said."""
    result: dict = {'details': details(model)}
    if model.get('type') == 'embedding':
        result['capabilities'] = ['embedding']
    if model.get('type') == 'decision':
        # A whole list: a decision model that does not say it takes images is not sent any.
        return dict(result, capabilities=systemone.capabilities(sees(model)), **lengths(model))
    reported = table(model.get('capabilities'))
    # Training for tool use is advice for the user; a model without it still takes tools.
    known = {name: reported[field] for name, field in (('vision', 'vision'), ('tools', 'trained_for_tool_use'))
             if isinstance(reported.get(field), bool)}
    if known.get('tools') is False:
        result['advisory'] = ['tools']
    options = table(reported.get('reasoning')).get('allowed_options')
    if isinstance(options, list):
        result['thinking_levels'] = [LEVELS[option] for option in options
                                     if isinstance(option, str) and option in LEVELS]
        # A model whose only option is off does not think.
        known['thinking'] = any(level is not False for level in result['thinking_levels'])
    if known:
        result['supports'] = known
    result.update(lengths(model))
    return result


@dataclass(frozen=True)
class LMStudioProvider(OpenAICompatibleProvider):
    kind = 'lmstudio'
    label = default_name = name_example = 'LM Studio'
    default_url = url_example = 'http://localhost:1234'
    capabilities = Capabilities(embeddings=True, thinking_levels=True, structured_output=True, tools=True,
                                logprobs=True, sampling_extras=True, thinking_text=True,
                                # Box questions wait for a decision model there that takes images, to test them on.
                                decisions=True, decision_images=True)
    # The server refuses the `json_object` type.
    json_mode = {'type': 'json_schema', 'json_schema': {'name': 'response', 'schema': {'type': 'object'}}}

    def thinking_fields(self, thinking):
        return {'reasoning_effort': effort(thinking, EFFORTS)}

    def _every(self, timeout, cancellable):
        def address():
            return validate_url(self.url).removesuffix('/v1') + '/api/v1/models'
        return self._listed(address, 'models', 'key', timeout, cancellable)

    def _models(self, timeout, cancellable):
        # A decision model is another kind of model, which no chat or embedding request can use.
        return [model for model in self._every(timeout, cancellable) if model.get('type') != 'decision']

    def list_decision_models(self, timeout=10, cancellable=None):
        return [dict(tag(model), capabilities=systemone.capabilities(sees(model)))
                for model in self._every(timeout, cancellable) if model.get('type') == 'decision']

    def decide(self, model, state, questions, images=None, keep_alive=None, timeout=120, cancellable=None):
        """Ask a decision model the named `questions` about `state`; returns the server's answer as it came."""
        if keep_alive is not None:
            raise ProviderError(_('This server does not accept: {0}.').format('keep_alive'))
        # The server takes an image only as a data URL.
        data = systemone.flat_body(systemone.request(model, state, questions), images, image_url)
        return systemone.post(self, lambda: self._address('/systemone'), data, timeout, cancellable)

    def list_models(self, timeout=10, cancellable=None):
        return [tag(model) for model in self._models(timeout, cancellable)]

    def list_embedding_models(self, timeout=10, cancellable=None):
        # By type alone: the server embeds with a chat model too, and its vectors are of no use.
        return [dict(tag(model), **({'context_length': limit(model)} if limit(model) else {}))
                for model in self._models(timeout, cancellable) if model.get('type') == 'embedding']

    def embedding_context(self, name, timeout=10, cancellable=None):
        model = find(self._models(timeout, cancellable), 'key', name)
        if 'context_length' not in lengths(model) and model.get('type') == 'embedding':
            # Not loaded, so only the maximum is known, and a model loads with far less. One word
            # loads it, and then the server says what it loaded with.
            try:
                self.embed(name, 'context', cancellable=cancellable)
                model = find(self._models(timeout, cancellable), 'key', name)
            except ProviderError:
                pass  # The maximum is the best there is; embedding will report what is wrong.
        return limit(model)

    def show_model(self, name, timeout=10, cancellable=None):
        # The list is the only source, so it is read again; loading a model changes what it says.
        return describe(find(self._every(timeout, cancellable), 'key', name))
