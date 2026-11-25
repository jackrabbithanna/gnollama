"""What every model provider offers, and what each one can do."""
import math
from dataclasses import dataclass, field, fields, replace
from gettext import gettext as _
from typing import Any, ClassVar
from urllib.parse import urlsplit

from .transport import ProviderError, RequestCancelled, Transport


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
    refuses_long_input: bool = False  # refuses text to embed that is longer than the model takes; others shorten it unseen
    decisions: bool = False        # decision models, asked through `decide`
    decision_images: bool = False  # images beside the state of a decision
    decision_boxes: bool = False   # box questions
    decision_question_limit: int = 0  # the most questions one decision takes; 0 where the server sets none
    # The image formats a decision takes, by name; empty where the server reads whatever it is sent.
    decision_image_formats: tuple = ()
    # Sampling options one model does not take, by the names in a WireOptions. No provider lacks them.
    left_out: tuple = ()
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
        ] + [(False, name in options, _(label)) for name, label in OPTION_LABELS.items() if name in self.left_out]
        return [name for offered, used, name in checks if used and not offered]

    def require(self, wire, host, model=None):
        """Raise naming what `wire` asks of the host record `host`, or of `model` on it, that it cannot do."""
        missing = self.unsupported(wire)
        if missing:
            name = host.get('name') or host.get('hostname', '')
            raise ValueError(_('{host} does not support: {options}.').format(
                host=f'{name} ({model})' if model else name, options=', '.join(missing)))

    def both(self, other):
        """What this record and `other` both offer, for one request sent to a model of each."""
        flags = {f.name: getattr(self, f.name) and getattr(other, f.name) for f in fields(self) if f.type is bool}
        limits = [limit for limit in (self.decision_question_limit, other.decision_question_limit) if limit]
        mine, theirs = self.decision_image_formats, other.decision_image_formats
        formats = tuple(name for name in mine if name in theirs) if mine and theirs else mine or theirs
        return replace(self, **flags, left_out=tuple(dict.fromkeys(self.left_out + other.left_out)),
                       options_notice='', decision_question_limit=min(limits, default=0),
                       decision_image_formats=formats)


# The sampling options without a flag, and the names the options panel gives them.
OPTION_LABELS = {'temperature': 'Temperature', 'top_p': 'Top P', 'seed': 'Seed', 'stop': 'Stop sequences',
                 'num_predict': 'Maximum output tokens'}


def supports(details, name):
    """Whether a model's details say it has a capability: True, False, or None where they are silent.

    `capabilities` is a complete list, as Ollama gives it: a name it lacks is
    not supported. `supports` holds what a server reported one by one, and a
    name it lacks is unknown.
    """
    if not isinstance(details, dict):
        return None
    known = details.get('supports')
    if isinstance(known, dict) and isinstance(known.get(name), bool):
        return known[name]
    listed = details.get('capabilities')
    return name in listed if isinstance(listed, list) else None


def generates(details):
    """Whether a model can be chatted with. False only where its capabilities are listed, and say it
    embeds or decides and does not complete; a model that says nothing may chat."""
    listed = details.get('capabilities') if isinstance(details, dict) else None
    return not (isinstance(listed, list) and 'completion' not in listed
                and ('embedding' in listed or 'decision' in listed))


def advisory(details, name):
    """Whether the False that `supports` gives for `name` is the server's advice, not a refusal.

    A provider lists such names under `advisory`. The model still accepts what
    they name, so nothing is blocked; only the wording differs.
    """
    listed = details.get('advisory') if isinstance(details, dict) else None
    return isinstance(listed, list) and name in listed


def _address(url, suffix):
    parts = urlsplit(url.strip())
    port = parts.port if parts.port is not None else {'http': 80, 'https': 443}[parts.scheme]
    if not parts.hostname or parts.query or parts.fragment:
        raise ValueError(url)
    path = parts.path.rstrip('/')
    return parts.scheme, parts.username, parts.password, parts.hostname, port, path.removesuffix(suffix)


def same_address(first, second, suffix=''):
    """Whether two URLs certainly name one server; any doubt means they differ.

    The scheme and host name compare without case, a default port equals none,
    and trailing slashes are ignored. The path counts, so a `/v1` suffix differs
    unless it is given as a `suffix` that both ends leave out of the address.
    """
    try:
        return _address(first, suffix) == _address(second, suffix)
    except (ValueError, KeyError, AttributeError, TypeError):
        return False


def canonical(name):
    """A model name with the tag Ollama assumes for one that has none."""
    return name if ':' in name.rsplit('/', 1)[-1] else name + ':latest'


def named(entries, name):
    """The list entry for a model name as given, else as Ollama completes it, or None."""
    for same in (lambda entry: entry == name, lambda entry: canonical(entry) == canonical(name)):
        for entry in entries:
            if isinstance(entry.get('name'), str) and same(entry['name']):
                return entry
    return None


def embedding_texts(input, dimensions=None):
    """The texts of an embedding request as a list, once they and the dimensions asked for are valid."""
    texts = [input] if isinstance(input, str) else list(input)
    if not texts or any(not isinstance(text, str) or not text.strip() for text in texts):
        raise ProviderError(_('Enter nonempty text to embed.'))
    if dimensions is not None and (type(dimensions) is not int or dimensions <= 0):
        raise ProviderError(_('Embedding dimensions must be a positive integer.'))
    return texts


def check_vectors(vectors, count, dimensions=None):
    """Raise unless `vectors` holds one usable vector for each of `count` texts, all of one size."""
    if not isinstance(vectors, list) or len(vectors) != count:
        raise ProviderError(_('The server returned an incorrect number of embeddings.'))
    for vector in vectors:
        if (not isinstance(vector, list) or not vector or
                any(type(n) not in (int, float) or not math.isfinite(n) for n in vector)):
            raise ProviderError(_('The server returned an invalid embedding vector.'))
    sizes = {len(vector) for vector in vectors}
    if len(sizes) != 1 or not all(any(vector) for vector in vectors):
        raise ProviderError(_('Embedding dimensions do not match, or a vector is zero.'))
    if dimensions is not None and sizes != {dimensions}:
        # Some servers ignore the size asked for and answer in the model's own.
        raise ProviderError(_('The server returned {0} dimensions where {1} were requested. '
                              'It may not support choosing dimensions for this model.').format(
                                  sizes.pop(), dimensions))


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
    optional_suffix = ''  # A path ending this type adds itself, so an address may hold it or not.

    @classmethod
    def keeps_key(cls, host, url):
        """Whether the key of the host record `host` stays when it is saved as this type at `url`.

        Storage and the host editor both ask here. A key follows a host to
        another type only at the same address, so it reaches no other server.
        """
        if not (cls.requires_key or cls.key_optional):
            return False
        kind = host.get('provider') or 'ollama'
        if kind == cls.kind:
            return True
        from .registry import PROVIDERS
        # Only where the type left behind ignores the ending too; to any other it is part of the address.
        shared = cls.optional_suffix == getattr(PROVIDERS.get(kind), 'optional_suffix', None)
        return same_address(host.get('hostname'), url, cls.optional_suffix if shared else '')

    @classmethod
    def check_url(cls, url):
        """Validate a URL entered for a host of this kind."""

    @property
    def key(self):
        """Identifies the destination for discovery caches."""
        return (self.kind, self.url, self.host_id, self.credential_id)

    def _unavailable(self, *args, **kwargs):
        raise NotImplementedError(f'{type(self).__name__} does not offer this operation')

    def model_capabilities(self, details):
        """What one model can be sent, given what `show_model` returned for it.

        Never more than the provider's own record, which also stands while the
        details are loading or could not be read (None).
        """
        return self.capabilities

    def decision_capabilities(self, details):
        """What one decision model can be asked, given its entry in `list_decision_models` or what
        `show_model` returned for it: without images and boxes where they say it does not see images.

        Never more than the provider's own record, which also stands where the details are silent.
        """
        if supports(details, 'vision') is False:
            return replace(self.capabilities, decision_images=False, decision_boxes=False)
        return self.capabilities

    def check(self, wire, host, model, details):
        """Raise naming what `wire` asks that the host record `host`, or `model` on it, cannot do.

        Every path that sends a request asks here, with the details the options panel was shown.
        """
        self.capabilities.require(wire, host)
        self.model_capabilities(details).require(wire, host, model)

    def list_embedding_models(self, timeout=10, cancellable=None):
        """The listed models that can embed, each with its `context_length` where the server says.

        A model whose details cannot be read stays, since it may embed.
        """
        models = []
        for model in self.list_models(timeout=timeout, cancellable=cancellable):
            try:
                details = self.show_model(model['name'], timeout=timeout, cancellable=cancellable)
            except RequestCancelled:
                raise
            except ProviderError:
                details = None
            capabilities = details.get('capabilities') if isinstance(details, dict) else None
            if capabilities is None or 'embedding' in capabilities:
                length = details.get('context_length') if isinstance(details, dict) else None
                models.append(dict(model, context_length=length) if type(length) is int else model)
        return models

    def model_tag(self, name, timeout=10, cancellable=None):
        """The list entry, with its `digest`, of a chat or embedding model, or None."""
        return named(self.list_models(timeout=timeout, cancellable=cancellable), name)

    def embedding_context(self, name, timeout=10, cancellable=None):
        """The tokens an embedding model accepts in one input, or None where the server does not say."""
        details = self.show_model(name, timeout=timeout, cancellable=cancellable)
        length = details.get('context_length') if isinstance(details, dict) else None
        return length if type(length) is int and length > 0 else None

    list_models = show_model = chat = generate = embed = _unavailable
    # decide(model, state, questions, images=None, keep_alive=None, timeout=120, cancellable=None) returns
    # the server's answer as it came; list_decision_models(timeout=10, cancellable=None) returns entries as
    # list_models does, each with `capabilities` holding `decision`, and `vision` where it sees images.
    decide = list_decision_models = _unavailable
    pull = delete = running = unload = probe = _unavailable
