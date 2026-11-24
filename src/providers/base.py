"""What every model provider offers, and what each one can do."""
from dataclasses import dataclass, field
from gettext import gettext as _
from typing import Any, ClassVar
from urllib.parse import urlsplit

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
    raw_generate: bool = False     # single responses outside a chat
    sampling_extras: bool = False  # top_k and min_p
    context_size: bool = False     # num_ctx
    thinking_text: bool = False    # returns what the model thought
    timing_stats: bool = False     # load, prompt, and generation durations
    model_verification: bool = False  # a content digest, not a fingerprint
    # Untranslated; shown beside the options this provider lacks.
    options_notice: str = ''

    def unsupported(self, wire):
        """Names of the settings in a WireOptions that this provider cannot be sent."""
        options = wire.options or {}
        checks = [
            (self.sampling_extras, 'top_k' in options, _('Top K')),
            (self.sampling_extras, 'min_p' in options, _('Min P')),
            (self.context_size, 'num_ctx' in options, _('Context size (tokens)')),
            (self.structured_output, wire.format is not None, _('Structured output')),
            (self.keep_alive, wire.keep_alive is not None, _('Model Retention')),
            (self.tools, bool(wire.tools), _('Tool Calling')),
            (self.logprobs, bool(wire.logprobs), _('Return token probabilities (logprobs)')),
        ]
        return [name for offered, used, name in checks if used and not offered]

    def require(self, wire, host):
        """Raise naming what `wire` asks of the host record `host` that it cannot do."""
        missing = self.unsupported(wire)
        if missing:
            raise ValueError(_('{host} does not support: {options}.').format(
                host=host.get('name') or host.get('hostname', ''), options=', '.join(missing)))


def _address(url):
    parts = urlsplit(url.strip())
    port = parts.port if parts.port is not None else {'http': 80, 'https': 443}[parts.scheme]
    if not parts.hostname or parts.query or parts.fragment:
        raise ValueError(url)
    return parts.scheme, parts.username, parts.password, parts.hostname, port, parts.path.rstrip('/')


def same_address(first, second):
    """Whether two URLs certainly name one server; any doubt means they differ.

    The scheme and host name compare without case, a default port equals none,
    and trailing slashes are ignored. The path counts, so a `/v1` suffix differs.
    """
    try:
        return _address(first) == _address(second)
    except (ValueError, KeyError, AttributeError, TypeError):
        return False


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
    fixed_url: ClassVar[str | None] = None  # The only URL this provider accepts, if any.
    requires_key = False
    key_optional = False  # Takes a key but works without one.
    # What the host editor shows for this type. Label, name, and name example
    # are untranslated.
    label = ''
    default_url = ''   # Suggested for a new host.
    default_name = ''  # Suggested while the name is empty.
    key_url = ''       # Where the user creates an API key.
    name_example = ''  # Placeholder of the name field, a whole sentence; none if empty.
    url_example = ''   # Placeholder of the address field; none if empty.

    @classmethod
    def keeps_key(cls, host, url):
        """Whether the key of the host record `host` stays when it is saved as this type at `url`.

        Storage and the host editor both ask here. A key follows a host to
        another type only at the same address, so it reaches no other server.
        """
        if not (cls.requires_key or cls.key_optional):
            return False
        return (host.get('provider') or 'ollama') == cls.kind or same_address(host.get('hostname'), url)

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
