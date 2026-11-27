"""Model providers and the transport they share."""
from .base import Capabilities, Provider, generates, supports
from .inference_manager import InferenceManagerProvider
from .lmstudio import LMStudioProvider
from .ollama import CLOUD_URL, OllamaCloudProvider, OllamaProvider
from .openai import OpenAICompatibleProvider
from .openai_platform import OpenAIProvider
from .openrouter import OpenRouterProvider
from .registry import PROVIDERS, ProviderRegistry, capabilities, provider_class
from .transport import ProviderError, RequestCancelled, Transport, validate_url
from .vercel import VercelProvider

__all__ = [
    'CLOUD_URL', 'Capabilities', 'InferenceManagerProvider', 'LMStudioProvider', 'OllamaCloudProvider', 'OllamaProvider',
    'OpenAICompatibleProvider', 'OpenAIProvider', 'OpenRouterProvider', 'PROVIDERS', 'Provider', 'ProviderError',
    'ProviderRegistry', 'RequestCancelled', 'Transport', 'VercelProvider', 'capabilities', 'generates',
    'provider_class', 'supports', 'validate_url',
]
