"""Replace a provider operation for every connection, and providers that differ from Ollama."""
from unittest.mock import MagicMock, patch

from src.providers import Capabilities, OllamaProvider


class LimitedProvider(OllamaProvider):
    """Chat with tools and nothing else, as a provider that lacks most options."""
    kind = 'limited'
    capabilities = Capabilities(tools=True)


def limited_host(storage):
    """A saved host of LimitedProvider. Call inside `patch.dict(PROVIDERS, LIMITED)`."""
    return storage.save_host('Limited', 'http://localhost:11434', provider='limited')


LIMITED = {'limited': LimitedProvider}


class OptionalKeyProvider(OllamaProvider):
    """Takes an API key but works without one, at a suggested address."""
    kind = 'optional_key'
    label = 'Optional Key Server'
    key_optional = True
    default_url = 'http://localhost:1234'


OPTIONAL_KEY = {'optional_key': OptionalKeyProvider}


class provider_patch:
    """Like patch.object, but the mock is called as mock(provider, *args, **kwargs).

    A call made only with keywords, as the chat strategies make, passes the
    provider as `host=`. Patches of the same operation may nest; the innermost
    one applies.
    """
    def __init__(self, name, **kwargs):
        self.mock = mock = MagicMock(name=name, **kwargs)

        def method(self, *args, **kw):
            return mock(self, *args, **kw) if args else mock(host=self, **kw)
        self._patch = patch.object(OllamaProvider, name, method)

    def start(self):
        self._patch.start()
        return self.mock

    def stop(self):
        self._patch.stop()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
        return False
