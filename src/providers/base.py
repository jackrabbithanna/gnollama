"""What every model provider offers, and what each one can do."""
from dataclasses import dataclass, field
from typing import Any

from .transport import Transport


@dataclass(frozen=True)
class Capabilities:
    """Read by widgets and validation instead of asking which provider this is."""
    model_management: bool = False   # pull and delete
    running_models: bool = False     # list and unload loaded models
    embeddings: bool = False
    thinking_levels: bool = False
    structured_output: bool = False
    tools: bool = False
    logprobs: bool = False
    keep_alive: bool = False
    raw_generate: bool = False
    # Untranslated; shown beside the options this provider lacks.
    options_notice: str = ''


@dataclass(frozen=True)
class Provider:
    """A request destination bound to its transport; never holds an API key.

    Chat and generate yield the chunks RequestState.consume reads: `message`
    (content, thinking, tool_calls) or `response`, optional `logprobs`, and a
    final chunk with `done` and metrics.
    """
    url: str
    host_id: str | None = None
    credential_id: str | None = None
    credentials: Any = field(default=None, repr=False, compare=False)
    transport: Transport = field(default_factory=Transport, repr=False, compare=False)

    kind = ''
    capabilities = Capabilities()
    fixed_url: str | None = None  # The only URL this provider accepts, if any.
    requires_key = False

    @classmethod
    def check_url(cls, url):
        """Validate a URL entered for a host of this kind."""

    @property
    def key(self):
        """Identifies the destination for discovery caches."""
        return (self.kind, self.url, self.host_id, self.credential_id)

    def _unavailable(self, *args, **kwargs):
        raise NotImplementedError(f'{type(self).__name__} does not offer this operation')

    list_models = show_model = chat = generate = embed = _unavailable
    pull = delete = running = unload = probe = _unavailable
