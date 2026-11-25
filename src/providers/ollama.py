"""Ollama's native HTTP API: a local or self-hosted server, and Ollama Cloud."""
import copy
from dataclasses import dataclass
from gettext import gettext as _

from gi.repository import Soup

from ..credentials import CredentialError
from . import systemone
from .base import Capabilities, Provider, check_vectors, embedding_texts
from .transport import ProviderError, RequestCancelled, validate_url

CLOUD_URL = 'https://ollama.com'

CLOUD_NOTICE = 'Ollama Cloud uses text output and manages model retention automatically.'


def _extracted():
    """Marks text that is translated where it is displayed, not here."""
    _('Ollama Cloud uses text output and manages model retention automatically.')
    _('Ollama Server')
    _('Ollama Cloud')
    _('For example: Local Ollama')


def common_params(data, options, thinking, logprobs, top_logprobs, format=None, keep_alive=None):
    if format is not None:
        if format != 'json' and not isinstance(format, dict):
            raise ProviderError(_('Invalid output format'))
        data['format'] = copy.deepcopy(format)
    if keep_alive is not None:
        if isinstance(keep_alive, bool) or not isinstance(keep_alive, int) or keep_alive < -1:
            raise ProviderError(_('Keep-alive must be a duration in seconds, 0, or -1'))
        data['keep_alive'] = keep_alive
    if thinking is not None:
        if not isinstance(thinking, bool) and thinking not in ('low', 'medium', 'high', 'max'):
            raise ProviderError(_('Invalid thinking setting'))
        data['think'] = thinking
    if logprobs:
        data['logprobs'] = True
        if top_logprobs is not None:
            if not isinstance(top_logprobs, int) or not 0 <= top_logprobs <= 20:
                raise ProviderError(_('Top logprobs must be between 0 and 20'))
            data['top_logprobs'] = top_logprobs
    if options:
        data['options'] = copy.deepcopy(options)


def context_length(info):
    """The most context a model takes, which `/api/show` files under the name of its architecture."""
    if not isinstance(info, dict):
        return None
    endings = [key for key in info if isinstance(key, str) and key.endswith('.context_length')]
    named = f"{info.get('general.architecture')}.context_length"
    key = named if named in info else endings[0] if len(endings) == 1 else None
    return info[key] if key and type(info[key]) is int and info[key] > 0 else None


@dataclass(frozen=True)
class OllamaProvider(Provider):
    kind = 'ollama'
    label = 'Ollama Server'
    name_example = 'For example: Local Ollama'
    url_example = 'http://localhost:11434'
    capabilities = Capabilities(model_management=True, running_models=True, embeddings=True,
                                thinking_levels=True, structured_output=True, tools=True,
                                logprobs=True, keep_alive=True, raw_generate=True,
                                sampling_extras=True, context_size=True, thinking_text=True,
                                timing_stats=True, model_verification=True, refuses_long_input=True,
                                decisions=True, decision_images=True, decision_question_limit=64)

    @property
    def key(self):
        # Host records that name the same server share one discovery cache.
        return validate_url(self.url)

    # Subclasses adjust these four.
    def _address(self, endpoint):
        return validate_url(self.url) + endpoint

    def _body(self, data):
        return data

    def _prepare(self, message, cancellable):
        return None

    def _explain(self, status, detail):
        return detail

    def _hooks(self):
        return dict(prepare=self._prepare, explain=self._explain)

    def _request(self, endpoint, data=None, method='GET', timeout=10, cancellable=None):
        return self.transport.request(lambda: self._address(endpoint), lambda: self._body(data), method,
                                      timeout, cancellable, **self._hooks())

    def _stream(self, endpoint, data, timeout=300, cancellable=None):
        return self.transport.stream_json(lambda: self._address(endpoint), lambda: self._body(data),
                                          timeout, cancellable, **self._hooks())

    def list_models(self, timeout=10, cancellable=None):
        return self._request('/api/tags', timeout=timeout, cancellable=cancellable).get('models', [])

    def show_model(self, name, timeout=10, cancellable=None):
        details = self._request('/api/show', {'model': name, 'verbose': False}, 'POST', timeout, cancellable)
        length = context_length(details.get('model_info')) if isinstance(details, dict) else None
        # Under the key the other providers use; the server's own answer wins if it ever has one.
        return details if length is None else {'context_length': length, **details}

    def version(self, timeout=5, cancellable=None):
        return self._request('/api/version', timeout=timeout, cancellable=cancellable).get('version', 'Unknown')

    def probe(self, cancellable=None):
        return _("Connected\nOllama Version: {0}").format(self.version(cancellable=cancellable))

    def embed(self, model, input, dimensions=None, keep_alive=None, timeout=300, cancellable=None):
        """Return validated native embeddings without allowing silent input truncation."""
        texts = embedding_texts(input, dimensions)
        data = {'model': model, 'input': texts, 'truncate': False}
        if dimensions is not None:
            data['dimensions'] = dimensions
        if keep_alive is not None:
            data['keep_alive'] = keep_alive
        result = self._request('/api/embed', data, 'POST', timeout, cancellable)
        check_vectors(result.get('embeddings') if isinstance(result, dict) else None, len(texts), dimensions)
        return result

    def list_decision_models(self, timeout=10, cancellable=None):
        """The listed models that decide; the list says what each one can do."""
        return [model for model in self.list_models(timeout=timeout, cancellable=cancellable)
                if isinstance(model, dict) and isinstance(model.get('capabilities'), list)
                and 'decision' in model['capabilities']]

    def decide(self, model, state, questions, images=None, keep_alive=None, timeout=120, cancellable=None):
        """Ask a decision model the named `questions` about `state`; returns the server's answer as it came."""
        # Images go in a list of their own. The server ignores `assets` and answers without the image.
        data = systemone.flat_body(systemone.request(model, state, questions), images)
        common_params(data, None, None, False, None, keep_alive=keep_alive)
        return systemone.post(self, lambda: self._address('/v1/systemone'), self._body(data), timeout, cancellable)

    # Image generation: not offered, so `capabilities.image_generation` stays off and both operations are
    # the base's `_unavailable`. Ollama 0.14 to 0.32.5 generated images on macOS (and Linux with CUDA);
    # 0.32.6 removed it, and `/api/generate` on such a model now answers 400. Should it return in that shape:
    # - list_image_models: the `list_models` entries whose `capabilities` hold `image`, with
    #   `image_parameters` of `n` (1 to 1) and `seed` (`{"type": "boolean"}`); no `input_references`.
    # - generate_image: POST /api/generate {model, prompt, width, height, steps, options: {seed}, stream}.
    #   Streamed chunks carry `completed` and `total` steps; the last has `done` and `image`, a PNG in base64,
    #   to be handed on as `{"data": [{"b64_json": image, "media_type": "image/png"}]}`.
    # `generates` already keeps models listed with `image` and without `completion` out of every chat picker.

    def delete(self, model, timeout=10, cancellable=None):
        self._request('/api/delete', {'model': model}, 'DELETE', timeout, cancellable)
        return True

    def running(self, timeout=10, cancellable=None):
        return self._request('/api/ps', timeout=timeout, cancellable=cancellable).get('models', [])

    def unload(self, model, timeout=30, cancellable=None):
        return self._request('/api/generate', {'model': model, 'keep_alive': 0, 'stream': False},
                             'POST', timeout, cancellable)

    def pull(self, model, insecure=False, timeout=300, cancellable=None):
        data = {'model': model, 'insecure': insecure, 'stream': True}
        complete = False
        for chunk in self._stream('/api/pull', data, timeout, cancellable):
            yield chunk
            if chunk.get('status') == 'success':
                complete = True
                break
        if not complete:
            raise ProviderError(_('Model pull ended before completion'))

    def generate(self, model, prompt, system=None, options=None, thinking=None,
                 logprobs=False, top_logprobs=None, images=None, timeout=300, cancellable=None,
                 format=None, keep_alive=None):
        data = {'model': model, 'prompt': prompt, 'stream': True}
        if images:
            data['images'] = images
        if system:
            data['system'] = system
        common_params(data, options, thinking, logprobs, top_logprobs, format, keep_alive)
        yield from self._stream('/api/generate', data, timeout, cancellable)

    def chat(self, model, messages, options=None, thinking=None, logprobs=False,
             top_logprobs=None, images=None, timeout=300, cancellable=None, format=None,
             keep_alive=None, tools=None):
        data = {'model': model, 'messages': copy.deepcopy(messages), 'stream': True}
        if tools:
            data['tools'] = copy.deepcopy(tools)
        if images and data['messages'] and data['messages'][-1]['role'] == 'user':
            data['messages'][-1]['images'] = images
        common_params(data, options, thinking, logprobs, top_logprobs, format, keep_alive)
        yield from self._stream('/api/chat', data, timeout, cancellable)


@dataclass(frozen=True)
class OllamaCloudProvider(OllamaProvider):
    """One fixed origin, bearer authentication, and no model management."""
    kind = 'ollama_cloud'
    label = default_name = name_example = 'Ollama Cloud'
    url_example = ''  # The address is fixed.
    key_url = 'https://ollama.com/settings/keys'
    capabilities = Capabilities(thinking_levels=True, tools=True, logprobs=True, raw_generate=True,
        sampling_extras=True, context_size=True, thinking_text=True, timing_stats=True,
        model_verification=True, refuses_long_input=True, options_notice=CLOUD_NOTICE)
    fixed_url = CLOUD_URL
    requires_key = True
    endpoints = ('/api/tags', '/api/show', '/api/chat', '/api/generate')
    decide = list_decision_models = Provider._unavailable

    @classmethod
    def check_url(cls, url):
        if url != CLOUD_URL:
            raise ValueError(_('Ollama Cloud requires https://ollama.com.'))

    @property
    def key(self):
        return (self.host_id, self.url, self.credential_id)

    def _address(self, endpoint):
        if self.url != CLOUD_URL:
            raise ProviderError(_('Ollama Cloud requires https://ollama.com.'))
        if endpoint not in self.endpoints:
            raise ProviderError(_('This operation is unavailable for Ollama Cloud.'))
        return CLOUD_URL + endpoint

    def _body(self, data):
        if data is None:
            return None
        return {k: v for k, v in data.items() if k not in ('format', 'keep_alive')}

    def _prepare(self, message, cancellable):
        message.add_flags(Soup.MessageFlags.NO_REDIRECT)
        try:
            key = self.credentials.lookup(self.host_id, self.credential_id, cancellable)
        except CredentialError as exc:
            if cancellable.is_cancelled():
                raise RequestCancelled(_('Request stopped')) from None
            raise ProviderError(str(exc)) from None
        message.get_request_headers().replace('Authorization', 'Bearer ' + key)
        return key

    def _explain(self, status, detail):
        if status in (401, 403):
            return _('Ollama Cloud rejected the API key or account access. Edit this host to check its API key.')
        if status == 429:
            return _('Ollama Cloud usage or rate limit reached. Try again later.')
        return detail

    def version(self, timeout=5, cancellable=None):
        raise ProviderError(_('This operation is unavailable for Ollama Cloud.'))

    def probe(self, cancellable=None):
        # The public model list shows the service is reachable without spending the key.
        models = OllamaProvider(CLOUD_URL, transport=self.transport).list_models(cancellable=cancellable)
        return _('Ollama Cloud is reachable. Available models: {0}\nYour API key will be checked when you generate a response.').format(len(models))
