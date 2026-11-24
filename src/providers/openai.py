"""The OpenAI-compatible chat API, as LM Studio, llama.cpp, vLLM, and others serve it."""
import base64
import copy
import hashlib
import json
from dataclasses import dataclass
from gettext import gettext as _

from gi.repository import Soup

from ..credentials import CredentialError
from .base import Capabilities, Provider, check_vectors, embedding_texts
from .transport import ProviderError, RequestCancelled, error_text, validate_url

LABEL = 'OpenAI-Compatible Server'

NOTICE = ('This server type does not report what its models support, '
          'so thinking, image, and tool support are not checked.')

THINKING_LEVELS = ('low', 'medium', 'high')

# Sampling options this API takes, by the name the application uses.
OPTION_NAMES = {'temperature': 'temperature', 'top_p': 'top_p', 'seed': 'seed', 'stop': 'stop',
                'num_predict': 'max_tokens'}
# More that some servers take, where `Capabilities.sampling_extras` says so.
EXTRA_NAMES = {'top_k': 'top_k', 'min_p': 'min_p'}
JSON_OBJECT: dict = {'type': 'json_object'}

IMAGE_TYPES = ((b'\x89PNG', 'image/png'), (b'\xff\xd8\xff', 'image/jpeg'), (b'GIF8', 'image/gif'))

DONE = object()


def _extracted():
    """Marks text that is translated where it is displayed, not here."""
    _('OpenAI-Compatible Server')
    _('For example: Local Server')
    _('This server type does not report what its models support, '
      'so thinking, image, and tool support are not checked.')


def image_url(image):
    if image.startswith('data:'):
        return image
    try:
        head = base64.b64decode(image[:24])
    except ValueError:
        head = b''
    mime = next((kind for magic, kind in IMAGE_TYPES if head.startswith(magic)), 'image/png')
    if head[:4] == b'RIFF' and head[8:12] == b'WEBP':
        mime = 'image/webp'
    return f'data:{mime};base64,{image}'


def wire_messages(messages, images=None):
    """Translate history in Ollama's shape; `images` go on a final user message."""
    made = 0

    def new_id():
        # Numbered in order, so a turn keeps its ids as the conversation grows.
        # Nine letters and digits is the strictest form a known server demands.
        nonlocal made
        made += 1
        return f'call{made:05d}'

    result: list = []
    pending: list = []  # (id, function name) of calls still waiting for a result
    for index, message in enumerate(messages):
        role = message.get('role')
        content = message.get('content') or ''
        pictures = message.get('images')
        if images and role == 'user' and index == len(messages) - 1:
            pictures = images
        item = {'role': role, 'content': content}
        if pictures:
            parts: list = [{'type': 'text', 'text': content}] if content else []
            item['content'] = parts + [{'type': 'image_url', 'image_url': {'url': image_url(picture)}}
                                       for picture in pictures]
        if role == 'assistant':
            pending = []
            calls = []
            for call in message.get('tool_calls') or []:
                function = call.get('function') or {}
                arguments = function.get('arguments')
                id = call.get('id') or new_id()
                pending.append((id, function.get('name')))
                calls.append({'id': id, 'type': 'function', 'function': {
                    'name': function.get('name'),
                    'arguments': arguments if isinstance(arguments, str) else json.dumps(arguments or {})}})
            if calls:
                item['tool_calls'] = calls
        elif role == 'tool':
            # Results follow their calls in order; a stored result names its function.
            id = message.get('tool_call_id')
            if id:
                match = next((call for call in pending if call[0] == id), None)
            else:
                match = next((call for call in pending if call[1] == message.get('tool_name')),
                             pending[0] if pending else None)
            if match:
                pending.remove(match)
            item['tool_call_id'] = id or (match[0] if match else new_id())
        result.append(item)
    return result


def level_fields(thinking):
    if thinking not in THINKING_LEVELS:
        raise ProviderError(_('This server accepts only the Low, Medium, and High thinking levels.'))
    return {'reasoning_effort': thinking}


def effort(thinking, efforts):
    """A server's name for a thinking setting, from the table of every setting it takes."""
    # By type as well: 1 and 0 are not the settings True and False.
    if type(thinking) not in (bool, str) or thinking not in efforts:
        raise ProviderError(_('Invalid thinking setting'))
    return efforts[thinking]


def fingerprint(*fields):
    """Stands where Ollama has a content digest; it changes when what the server reports does."""
    return hashlib.sha256(json.dumps(fields).encode()).hexdigest()


def find(entries, field, name):
    for entry in entries:
        if entry.get(field) == name:
            return entry
    raise ProviderError(_('Selected model is no longer available.'))


def chat_body(model, messages, options=None, thinking=None, logprobs=False, top_logprobs=None,
              images=None, format=None, keep_alive=None, tools=None, *, names=OPTION_NAMES,
              thinking_fields=level_fields, json_mode=JSON_OBJECT):
    """The request for the plain API; the keyword-only arguments are what a server type changes."""
    data = {'model': model, 'messages': wire_messages(messages, images), 'stream': True,
            'stream_options': {'include_usage': True}}
    # Capability checks stop these earlier; nothing is ever dropped without saying so.
    refused = sorted(set(options or {}) - set(names)) + (['keep_alive'] if keep_alive is not None else [])
    if refused:
        raise ProviderError(_('This server does not accept: {0}.').format(', '.join(refused)))
    for name, value in (options or {}).items():
        if name == 'num_predict' and isinstance(value, (int, float)) and value < 0:
            continue
        data[names[name]] = copy.deepcopy(value)
    if format is not None:
        if format == 'json':
            data['response_format'] = copy.deepcopy(json_mode)
        elif isinstance(format, dict):
            data['response_format'] = {'type': 'json_schema', 'json_schema': {
                'name': 'response', 'schema': copy.deepcopy(format)}}
        else:
            raise ProviderError(_('Invalid output format'))
    if thinking is not None:
        data.update(thinking_fields(thinking))
    if logprobs:
        data['logprobs'] = True
        if top_logprobs is not None:
            if not isinstance(top_logprobs, int) or not 0 <= top_logprobs <= 20:
                raise ProviderError(_('Top logprobs must be between 0 and 20'))
            data['top_logprobs'] = top_logprobs
    if tools:
        data['tools'] = copy.deepcopy(tools)
    return data


def decode_event(line):
    """One line of a server-sent event stream: an object, DONE, or None to skip."""
    line = line.strip()
    if not line.startswith(b'data:'):
        return None  # Comments that keep the connection open, and event names.
    payload = line[5:].strip()
    if payload == b'[DONE]':
        return DONE
    try:
        event = json.loads(payload)
    except (ValueError, UnicodeError):
        event = None
    if not isinstance(event, dict):
        raise ProviderError(_('Invalid JSON received from the server'))
    if event.get('error'):
        raise ProviderError(error_text(event['error']))
    return event


def whole_calls(parts):
    """Tool calls in the shape the application stores, from accumulated deltas."""
    calls = []
    for index in sorted(parts):
        part = parts[index]
        try:
            arguments = json.loads(part['arguments']) if part['arguments'].strip() else {}
        except ValueError:
            arguments = None
        # Arguments that are not an object stay as text, to be reported as malformed.
        call = {'function': {'name': part['name'],
                             'arguments': arguments if isinstance(arguments, dict) else part['arguments']}}
        if part['id']:
            call['id'] = part['id']
        calls.append(call)
    return calls


def text(value):
    return value if isinstance(value, str) else ''


def table(value) -> dict:
    return value if isinstance(value, dict) else {}


@dataclass(frozen=True)
class OpenAICompatibleProvider(Provider):
    """Chat, embeddings, and model listing under /v1, with an optional bearer key."""
    kind = 'openai'
    label = LABEL
    default_url = ''
    default_name = ''
    key_url = ''
    name_example = 'For example: Local Server'
    url_example = 'http://localhost:8080'
    key_optional = True
    capabilities = Capabilities(embeddings=True, thinking_levels=True, structured_output=True, tools=True,
                                logprobs=True, thinking_text=True, options_notice=NOTICE)
    optional_suffix = '/v1'
    json_mode = JSON_OBJECT  # What asks for JSON without a schema.

    def thinking_fields(self, thinking):
        """Request fields for a thinking setting; raises for one this type cannot send."""
        return level_fields(thinking)

    def request_body(self, model, messages, *settings, **named):
        names = OPTION_NAMES | (EXTRA_NAMES if self.capabilities.sampling_extras else {})
        return chat_body(model, messages, *settings, **named, names=names,
                         thinking_fields=self.thinking_fields, json_mode=self.json_mode)

    def _address(self, endpoint):
        return validate_url(self.url).removesuffix('/v1') + '/v1' + endpoint

    def _prepare(self, message, cancellable):
        if not self.credential_id and not (self.credentials and self.credentials.has_session(self.host_id)):
            return None
        # A redirect must not carry the key to another origin.
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
            return _('The server rejected the API key, or requires one. Edit this host to check its API key.')
        if status == 429:
            return _('The server reported a usage or rate limit. Try again later.')
        return error_text(detail)

    def _hooks(self):
        return dict(prepare=self._prepare, explain=self._explain)

    def _listed(self, address, field, name, timeout, cancellable):
        """The model objects under `field` of the list at `address()` that have a name under `name`."""
        result = self.transport.request(address, None, 'GET', timeout, cancellable, **self._hooks())
        models = result.get(field)
        if not isinstance(models, list):
            raise ProviderError(_('The server did not return a model list.'))
        return [model for model in models
                if isinstance(model, dict) and isinstance(model.get(name), str) and model[name]]

    def list_models(self, timeout=10, cancellable=None):
        # The server gives nothing to tell one build of a model from another, so the name alone identifies it.
        return [{'name': model['id'], 'digest': fingerprint(model['id'])}
                for model in self._listed(lambda: self._address('/models'), 'data', 'id', timeout, cancellable)]

    def list_embedding_models(self, timeout=10, cancellable=None):
        # The list does not say which models embed.
        return self.list_models(timeout=timeout, cancellable=cancellable)

    def show_model(self, name, timeout=10, cancellable=None):
        # The server reports nothing about a model, so its capabilities stay unknown.
        return {'thinking_levels': list(THINKING_LEVELS)}

    def embed(self, model, input, dimensions=None, keep_alive=None, timeout=300, cancellable=None):
        """Return validated embeddings under `embeddings`, in the order of the texts.

        The API cannot be told to refuse an input longer than the model's context, and a
        server may shorten one without saying so; the caller checks the length first.
        """
        texts = embedding_texts(input, dimensions)
        if keep_alive is not None:
            raise ProviderError(_('This server does not accept: {0}.').format('keep_alive'))
        data = {'model': model, 'input': texts}
        if dimensions is not None:
            data['dimensions'] = dimensions
        result = self.transport.request(lambda: self._address('/embeddings'), lambda: data, 'POST',
                                        timeout, cancellable, **self._hooks())
        rows = result.get('data')
        # Each vector says which text it is for; the order they arrive in is not promised.
        places = {row.get('index'): row.get('embedding') for row in rows if isinstance(row, dict)
                  and type(row.get('index')) is int} if isinstance(rows, list) else {}
        if not isinstance(rows, list) or len(rows) != len(texts) or set(places) != set(range(len(texts))):
            raise ProviderError(_('The server returned an incorrect number of embeddings.'))
        vectors = [places[index] for index in range(len(texts))]
        check_vectors(vectors, len(texts), dimensions)
        return {'embeddings': vectors}

    def probe(self, cancellable=None):
        return _('Connected\nAvailable models: {0}').format(len(self.list_models(cancellable=cancellable)))

    def chat(self, model, messages, options=None, thinking=None, logprobs=False,
             top_logprobs=None, images=None, timeout=300, cancellable=None, format=None,
             keep_alive=None, tools=None):
        data = self.request_body(model, messages, options, thinking, logprobs, top_logprobs, images,
                                 format, keep_alive, tools)
        parts: dict = {}
        finish = None
        usage: dict = {}
        lines = self.transport.stream_lines(lambda: self._address('/chat/completions'), lambda: data,
                                            timeout, cancellable, decode=decode_event, **self._hooks())
        try:
            for event in lines:
                if event is DONE:
                    break
                if event is None:
                    continue
                usage = table(event.get('usage')) or usage
                choices = event.get('choices')
                choice = table(choices[0] if isinstance(choices, list) and choices else None)
                finish = choice.get('finish_reason') or finish
                delta = table(choice.get('delta'))
                for item in delta.get('tool_calls') or []:
                    item = table(item)
                    index = item.get('index')
                    if not isinstance(index, int):
                        # Servers that omit the index send each call whole, with its id.
                        index = len(parts) if item.get('id') or not parts else max(parts)
                    part = parts.setdefault(index, {'id': '', 'name': '', 'arguments': ''})
                    function = table(item.get('function'))
                    part['id'] = text(item.get('id')) or part['id']
                    part['name'] += text(function.get('name'))
                    part['arguments'] += text(function.get('arguments'))
                chunk: dict = {'message': {
                    'content': text(delta.get('content')),
                    'thinking': text(delta.get('reasoning_content')) or text(delta.get('reasoning'))}}
                tokens = table(choice.get('logprobs')).get('content')
                if isinstance(tokens, list) and tokens:
                    chunk['logprobs'] = tokens
                if chunk['message']['content'] or chunk['message']['thinking'] or 'logprobs' in chunk:
                    yield chunk
        finally:
            lines.close()
        last: dict = {'message': {'content': '', 'thinking': ''}}
        if parts:
            last['message']['tool_calls'] = whole_calls(parts)
        if finish is None:
            # Ended early: hand over what arrived and let the caller report the missing end.
            if parts:
                yield last
            return
        last.update(done=True, done_reason=finish)
        counts = (('prompt_eval_count', usage.get('prompt_tokens')), ('eval_count', usage.get('completion_tokens')),
                  ('prompt_eval_cached_count', table(usage.get('prompt_tokens_details')).get('cached_tokens')))
        last.update({key: value for key, value in counts if type(value) is int})
        yield last
