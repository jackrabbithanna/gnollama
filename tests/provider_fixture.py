"""Replace a provider operation for every connection, including Ollama Cloud."""
from unittest.mock import MagicMock, patch

from src.providers import OllamaProvider


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
