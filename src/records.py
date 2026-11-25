"""Storage records, request settings, and policy shared by controllers and persistence."""
import copy
from dataclasses import dataclass, field, fields
from typing import TypedDict


class Message(TypedDict, total=False):
    uid: str
    role: str
    content: str
    images: list[str]
    response_metadata: dict


# Sampling options, stored at the top level of a chat's saved options.
MODEL_OPTIONS = ('seed', 'temperature', 'top_k', 'top_p', 'min_p', 'num_ctx', 'num_predict', 'stop')


def _values(record):
    return {f.name: copy.deepcopy(getattr(record, f.name)) for f in fields(record)}


def _known(cls, data, names=None):
    """Take a dataclass's fields out of `data`; a stored null means the default."""
    values = {}
    for f in fields(cls):
        if f.name in (names or [f.name]) and f.name in data:
            value = data.pop(f.name)
            if value is not None:
                values[f.name] = value
    return values


@dataclass(frozen=True)
class ModelTarget:
    """Where a request goes. `url` is None until the host record is resolved."""
    host_id: str | None = None
    url: str | None = None
    model: str | None = None
    # The host's name when the request was made; a response keeps it after a rename.
    host_name: str | None = None


@dataclass(frozen=True)
class WireOptions:
    """Everything a provider may be sent besides the messages. Nothing else is."""
    options: dict = field(default_factory=dict)
    system: str | None = None
    thinking: bool | str | None = None
    format: dict | str | None = None
    keep_alive: int | str | None = None
    tools: list | None = None
    logprobs: bool = False
    top_logprobs: int | None = None

    def provider_args(self):
        """Keyword arguments shared by Provider.chat and Provider.generate."""
        return dict(options=self.options, thinking=self.thinking, logprobs=self.logprobs,
                    top_logprobs=self.top_logprobs, format=self.format, keep_alive=self.keep_alive)


@dataclass(frozen=True)
class ViewState:
    """Panel and knowledge state: stored and displayed, never sent to a provider."""
    show_stats: bool = True
    output_mode: str = 'text'
    schema_text: str = ''
    tools_enabled: bool = False
    tools_text: str = ''
    knowledge: dict = field(default_factory=dict)
    query_override: str = ''
    draft_settings: dict = field(default_factory=dict)
    # What the panel holds for options the host or model lacks, as typed and under the names a chat's
    # options use. Kept so it applies again on a host and model that take it.
    unsent: dict = field(default_factory=dict)
    # Stored keys this version does not know, kept so they survive a save.
    extra: dict = field(default_factory=dict)


@dataclass(frozen=True)
class RequestSettings:
    """The settings of one request. Replace parts with dataclasses.replace."""
    target: ModelTarget | None = None
    wire: WireOptions = field(default_factory=WireOptions)
    view: ViewState = field(default_factory=ViewState)
    endpoint: str = 'chat'
    history_images_omitted: bool = False

    def api_details(self):
        """What a response records about its request: the destination and wire options."""
        wire = _values(self.wire)
        target = self.target or ModelTarget()
        return dict(host=target.url, host_id=target.host_id, host_name=target.host_name,
                    model=target.model, options=wire['options'],
                    thinking=wire['thinking'], system=wire['system'], logprobs=wire['logprobs'],
                    top_logprobs=wire['top_logprobs'], endpoint=self.endpoint, format=wire['format'],
                    keep_alive=wire['keep_alive'], tools=wire['tools'])

    # Comparison targets and model-conversation participants are stored flat.

    def to_stored(self, sparse=False):
        """The flat stored form. `sparse` leaves out panel state still at its default."""
        data = copy.deepcopy(self.view.extra)
        if self.target is not None:
            data.update(host=self.target.url, host_id=self.target.host_id,
                        model=self.target.model, endpoint=self.endpoint)
            if self.target.host_name is not None:
                data['host_name'] = self.target.host_name
        data.update(_values(self.wire))
        view = _values(self.view)
        del view['extra']
        if not view['unsent']:
            del view['unsent']  # A record from before these were kept is written back unchanged.
        if sparse:
            defaults = _values(ViewState())
            view = {k: v for k, v in view.items() if k == 'show_stats' or v != defaults[k]}
        data.update(view)
        if self.history_images_omitted:
            data['history_images_omitted'] = True
        return data

    @classmethod
    def from_stored(cls, data):
        data = copy.deepcopy(data)
        target = None
        if any(key in data for key in ('host', 'host_id', 'model', 'host_name')):
            target = ModelTarget(data.pop('host_id', None), data.pop('host', None), data.pop('model', None),
                                 data.pop('host_name', None))
        endpoint = data.pop('endpoint', None) or 'chat'
        omitted = bool(data.pop('history_images_omitted', False))
        wire = WireOptions(**_known(WireOptions, data))
        view = _known(ViewState, data, [f.name for f in fields(ViewState) if f.name != 'extra'])
        return cls(target, wire, ViewState(**view, extra=data), endpoint, omitted)

    # A chat stores sampling options at the top level beside the panel state.
    # The output format and tool definitions are not stored; the panel derives
    # them from the schema and tools text when a request is sent. Values that
    # were not sent are stored apart, under `unsent`.

    def to_chat_options(self):
        data = copy.deepcopy(self.view.extra)
        data.update(copy.deepcopy(self.wire.options))
        data.update(thinking_val=self.wire.thinking, logprobs=self.wire.logprobs,
                    top_logprobs=self.wire.top_logprobs, show_stats=self.view.show_stats,
                    output_mode=self.view.output_mode, schema_text=self.view.schema_text,
                    keep_alive=self.wire.keep_alive, tools_enabled=self.view.tools_enabled,
                    tools_text=self.view.tools_text, knowledge=copy.deepcopy(self.view.knowledge))
        if self.view.unsent:
            data['unsent'] = copy.deepcopy(self.view.unsent)
        return data

    @classmethod
    def from_chat_options(cls, data):
        data = copy.deepcopy(data or {})
        options = {name: data.pop(name) for name in MODEL_OPTIONS if name in data}
        thinking = data.pop('thinking_val', None)
        wire = _known(WireOptions, data, ('keep_alive', 'logprobs', 'top_logprobs'))
        view = _known(ViewState, data, ('show_stats', 'output_mode', 'schema_text', 'tools_enabled',
                                        'tools_text', 'knowledge', 'unsent'))
        return cls(None, WireOptions(options=options, thinking=thinking, **wire), ViewState(**view, extra=data))


@dataclass(frozen=True)
class DecisionSettings:
    """What a decision asks, and of which model: a session's options, a draft's settings, and each run."""
    target: ModelTarget | None = None
    # The object `decision.questions_object` returns; a draft may hold one still being written.
    questions: dict = field(default_factory=dict)
    # Whether the state is sent as written or parsed as JSON first.
    state_mode: str = 'text'

    def to_stored(self):
        data: dict = dict(questions=copy.deepcopy(self.questions), state_mode=self.state_mode)
        if self.target is not None:
            data['target'] = dict(host=self.target.url, host_id=self.target.host_id,
                                  host_name=self.target.host_name, model=self.target.model)
        return data

    @classmethod
    def from_stored(cls, data):
        data = data or {}
        target = data.get('target')
        questions = data.get('questions')
        return cls(ModelTarget(target.get('host_id'), target.get('host'), target.get('model'), target.get('host_name'))
                   if isinstance(target, dict) else None,
                   copy.deepcopy(questions) if isinstance(questions, dict) else {},
                   'json' if data.get('state_mode') == 'json' else 'text')


class ComparisonTarget(TypedDict):
    id: str
    settings: dict  # RequestSettings.to_stored()
    request: dict


class ConversationRun(TypedDict):
    id: str
    status: str
    next_turn: int


class ConversationAttempt(TypedDict):
    id: str
    run_id: str
    turn: int
    attempt: int
    input_uid: str
    status: str


class DecisionRun(TypedDict):
    id: str
    session_id: str
    position: int
    state: str
    state_mode: str
    images: list[str]
    questions: dict
    target: dict  # DecisionSettings.to_stored()['target']
    status: str  # running, done, failed, cancelled, or interrupted
    result: dict | None
    error: str | None
    created_at: float
    finished_at: float | None


@dataclass
class Draft:
    id: str
    mode: str = 'chat'
    chat_id: str | None = None
    text: str = ''
    images: list[str] = field(default_factory=list)
    settings: dict = field(default_factory=dict)
    targets: list[dict] = field(default_factory=list)
    revision: int = 0


def has_saved_work(messages=(), options=None, draft=None, system='', title='New Chat', pinned=False):
    """Defaults alone do not turn an unused editor into a saved conversation."""
    options = options or {}
    knowledge = options.get('knowledge') or {}
    draft = draft or {}
    return bool(messages or draft.get('text', '').strip() or draft.get('images')
                or draft.get('settings', {}).get('_configured') or draft.get('targets')
                or draft.get('settings', {}).get('questions')
                or knowledge.get('selection') or knowledge.get('collection_ids')
                or options.get('tools_text', '').strip() or options.get('schema_text', '').strip()
                or options.get('_configured') or system or pinned or title not in ('', 'New Chat'))
