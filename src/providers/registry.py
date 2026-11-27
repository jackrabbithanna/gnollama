"""Builds the provider for a host record."""
from gettext import gettext as _

from .ollama import OllamaCloudProvider, OllamaProvider
from .inference_manager import InferenceManagerProvider
from .lmstudio import LMStudioProvider
from .openai import OpenAICompatibleProvider
from .openai_platform import OpenAIProvider
from .openrouter import OpenRouterProvider
from .vercel import VercelProvider

PROVIDERS = {cls.kind: cls for cls in (OllamaProvider, OllamaCloudProvider, OpenAICompatibleProvider,
                                       LMStudioProvider, InferenceManagerProvider, OpenRouterProvider, VercelProvider,
                                       OpenAIProvider)}


def provider_class(kind):
    try:
        return PROVIDERS[kind or 'ollama']
    except KeyError:
        raise ValueError(_('Invalid host type.')) from None


def capabilities(host):
    """Capabilities for a host record; with no host, those of a local server."""
    return provider_class(host.get('provider') if host else None).capabilities


class ProviderRegistry:
    def __init__(self, transport, credentials):
        self.transport, self.credentials = transport, credentials

    def connection(self, host):
        """Snapshot a host's destination without retrieving its secret."""
        return provider_class(host.get('provider'))(host['hostname'], host['id'], host.get('credential_id'),
                                                    credentials=self.credentials, transport=self.transport)

    def ollama_at(self, url):
        """An Ollama server known only by its URL, such as a stored embedding host."""
        return OllamaProvider(url, transport=self.transport)

    capabilities = staticmethod(capabilities)
