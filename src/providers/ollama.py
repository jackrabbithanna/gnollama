"""Ollama's native HTTP API: a local or self-hosted server, and Ollama Cloud."""
import copy
import math
from dataclasses import dataclass
from gettext import gettext as _

from gi.repository import Soup

from ..credentials import CredentialError
from .base import Capabilities, Provider
from .transport import ProviderError, RequestCancelled, validate_url

CLOUD_URL = 'https://ollama.com'

CLOUD_NOTICE = 'Ollama Cloud uses text output and manages model retention automatically.'


def _extracted():
    """Marks text that is translated where it is displayed, not here."""
    _('Ollama Cloud uses text output and manages model retention automatically.')
    _('Ollama Server')
    _('Ollama Cloud')


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


@dataclass(frozen=True)
class OllamaProvider(Provider):
    kind = 'ollama'
    label = 'Ollama Server'
    capabilities = Capabilities(model_management=True, running_models=True, embeddings=True,
                                thinking_levels=True, structured_output=True, tools=True,
                                logprobs=True, keep_alive=True, raw_generate=True,
                                sampling_extras=True, context_size=True, thinking_text=True,
                                timing_stats=True, model_verification=True)

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
        return self._request('/api/show', {'model': name, 'verbose': False}, 'POST', timeout, cancellable)

    def version(self, timeout=5, cancellable=None):
        return self._request('/api/version', timeout=timeout, cancellable=cancellable).get('version', 'Unknown')

    def probe(self, cancellable=None):
        return _("Connected\nOllama Version: {0}").format(self.version(cancellable=cancellable))

    def embed(self, model, input, dimensions=None, keep_alive=None, timeout=300, cancellable=None):
        """Return validated native embeddings without allowing silent input truncation."""
        texts = [input] if isinstance(input, str) else list(input)
        if not texts or any(not isinstance(text, str) or not text.strip() for text in texts):
            raise ProviderError(_('Enter nonempty text to embed.'))
        data = {'model': model, 'input': texts, 'truncate': False}
        if dimensions is not None:
            if type(dimensions) is not int or dimensions <= 0:
                raise ProviderError(_('Embedding dimensions must be a positive integer.'))
            data['dimensions'] = dimensions
        if keep_alive is not None:
            data['keep_alive'] = keep_alive
        result = self._request('/api/embed', data, 'POST', timeout, cancellable)
        vectors = result.get('embeddings') if isinstance(result, dict) else None
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise ProviderError(_('The server returned an incorrect number of embeddings.'))
        size = dimensions
        for vector in vectors:
            if (not isinstance(vector, list) or not vector or
                    any(type(n) not in (int, float) or not math.isfinite(n) for n in vector)):
                raise ProviderError(_('The server returned an invalid embedding vector.'))
            size = size or len(vector)
            if len(vector) != size or not any(vector):
                raise ProviderError(_('Embedding dimensions do not match, or a vector is zero.'))
        return result

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
    label = default_name = 'Ollama Cloud'
    key_url = 'https://ollama.com/settings/keys'
    capabilities = Capabilities(thinking_levels=True, tools=True, logprobs=True, raw_generate=True,
        sampling_extras=True, context_size=True, thinking_text=True, timing_stats=True,
        model_verification=True, options_notice=CLOUD_NOTICE)
    fixed_url = CLOUD_URL
    requires_key = True
    endpoints = ('/api/tags', '/api/show', '/api/chat', '/api/generate')

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
