"""Storage records and policy shared by controllers and persistence."""
from dataclasses import dataclass, field
from typing import TypedDict


class Message(TypedDict, total=False):
    uid: str
    role: str
    content: str
    images: list[str]
    response_metadata: dict


class RequestSettings(TypedDict, total=False):
    host: str
    host_id: str
    model: str
    options: dict
    system: str | None
    thinking: bool | str | None
    format: dict | str | None
    keep_alive: int | str | None
    tools: list[dict] | None
    logprobs: bool
    top_logprobs: int | None
    show_stats: bool
    knowledge: dict


class ComparisonTarget(TypedDict):
    id: str
    settings: RequestSettings
    request: dict


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
                or knowledge.get('selection') or knowledge.get('collection_ids')
                or options.get('tools_text', '').strip() or options.get('schema_text', '').strip()
                or options.get('_configured') or system or pinned or title not in ('', 'New Chat'))
