"""OpenAI's own API at one address: the Responses API for chat, with embeddings, images, and decisions.

The model list names each model and nothing else, so what a model takes comes from the table below.
"""
import copy
import json
import re
from dataclasses import dataclass
from gettext import gettext as _

from . import imaging, systemone
from .base import Capabilities, named
from .openai import (OpenAICompatibleProvider, find, fingerprint, image_url, narrowed, table, text, whole_calls,
                     wire_messages)
from .transport import ProviderError, error_text

URL = 'https://api.openai.com'

# What a reasoning model may be asked, measured on 2026-11-24 by sending each effort and setting to each model.
# The efforts are OpenAI's names. How temperature, Top P, and logprobs are taken:
# ALWAYS by a model that does not reason; DEFAULT with effort `none`, which is the model's own default;
# OFF only with effort `none` sent; NEVER at all.
ALWAYS, DEFAULT, OFF, NEVER = 'always', 'default', 'off', 'never'
TO_HIGH = ('low', 'medium', 'high')
NONE_TO_XHIGH = ('none',) + TO_HIGH + ('xhigh',)
TO_MAX = TO_HIGH + ('xhigh', 'max')
MODELS = {
    'o3': (TO_HIGH, NEVER),
    'gpt-5': (('minimal',) + TO_HIGH, NEVER), 'gpt-5-mini': (('minimal',) + TO_HIGH, NEVER),
    'gpt-5-nano': (('minimal',) + TO_HIGH, NEVER), 'gpt-5-pro': (('high',), NEVER),
    'gpt-5.1': (('none',) + TO_HIGH, DEFAULT),
    'gpt-5.2': (NONE_TO_XHIGH, DEFAULT), 'gpt-5.3-codex': (NONE_TO_XHIGH, DEFAULT),
    'gpt-5.4': (NONE_TO_XHIGH, DEFAULT), 'gpt-5.4-mini': (NONE_TO_XHIGH, DEFAULT),
    'gpt-5.4-nano': (NONE_TO_XHIGH, DEFAULT),
    'gpt-5.2-pro': (('medium', 'high', 'xhigh'), NEVER), 'gpt-5.4-pro': (('medium', 'high', 'xhigh'), NEVER),
    'gpt-5.5-pro': (('medium', 'high', 'xhigh'), NEVER),
    'gpt-5.5': (NONE_TO_XHIGH, OFF),
    'gpt-5.6-luna': (('none',) + TO_MAX, OFF), 'gpt-5.6-sol': (('none',) + TO_MAX, OFF),
    'gpt-5.6-terra': (('none',) + TO_MAX, OFF), 'gpt-6-luna': (('none',) + TO_MAX, OFF),
    'gpt-6-sol': (('none',) + TO_MAX, OFF), 'gpt-6.1-terra': (('none',) + TO_MAX, OFF),
    'gpt-6-astra': (TO_MAX, NEVER), 'gpt-6.1-sol': (TO_MAX, NEVER), 'gpt-6.2-astra': (TO_MAX, NEVER),
    'gpt-6.2-sol': (TO_MAX, NEVER),
    'chat-latest': (('medium',), NEVER),  # It thinks, and takes no other effort.
    'gpt-4.1': ((), ALWAYS), 'gpt-4.1-mini': ((), ALWAYS), 'gpt-4.1-nano': ((), ALWAYS),
    'gpt-4o': ((), ALWAYS), 'gpt-4o-mini': ((), ALWAYS),
}
# Context and output limits where OpenAI documents them, by a model's base name; others are left out.
LIMITS = {'gpt-4.1': (1047576, 32768), 'gpt-4.1-mini': (1047576, 32768), 'gpt-4.1-nano': (1047576, 32768),
          'gpt-4o': (128000, 16384), 'gpt-4o-mini': (128000, 16384), 'o3': (200000, 100000),
          **{name: (400000, 128000) for name in ('gpt-5', 'gpt-5-mini', 'gpt-5-nano', 'gpt-5-pro', 'gpt-5.1',
                                                  'gpt-5.2', 'gpt-5.2-pro', 'gpt-5.3-codex')}}
EMBEDDING_CONTEXT = 8191

# Models that do something other than chat, embed, or make images: speech, transcription, realtime, moderation,
# and search, which needs a tool of its own.
LEFT_OUT = re.compile(r'(^|-)(tts|whisper|transcribe|audio|realtime|live|moderation|search)(-|$)|^(tts|dall-e)')
SNAPSHOT = re.compile(r'-\d{4}-\d{2}-\d{2}$')

# The thinking settings the application offers, from the efforts a model takes. Max is the highest it has.
LEVELS = ((False, 'none'), ('low', 'low'), ('medium', 'medium'), ('high', 'high'))

# The image settings every gpt-image model takes, measured on 2026-11-24; aspect ratios are sent as a size.
SIZES = {'1:1': '1024x1024', '3:2': '1536x1024', '2:3': '1024x1536'}
IMAGE_SETTINGS: dict = {
    'aspect_ratio': {'type': 'enum', 'values': list(SIZES)},
    'quality': {'type': 'enum', 'values': ['low', 'medium', 'high']},
    'output_format': {'type': 'enum', 'values': ['png', 'jpeg', 'webp']},
    'background': {'type': 'enum', 'values': ['transparent', 'opaque']},
    'n': {'type': 'range', 'min': 1, 'max': 10},
    'input_references': {'type': 'range', 'min': 0, 'max': 16},
}
# Models that refuse a transparent background, by base name.
OPAQUE = ('gpt-image-2',)

# The models the Decisions API takes, by base name: the one its reference lists, and the only one of the listed
# models it accepted on 2026-11-24. The model list does not say which decide.
DECIDERS = ('gpt-6-luna',)
# The application's question types as the Decisions API names them. It has no box questions.
QUESTION_TYPES = {'noul': 'predicate', 'choice': 'choice', 'score': 'score'}

# Sampling options the Responses API takes, by the name the application uses. It has no seed or stop sequences.
OPTION_NAMES = {'temperature': 'temperature', 'top_p': 'top_p', 'num_predict': 'max_output_tokens'}
SAMPLING = ('temperature', 'top_p')


def _extracted():
    """Marks text that is translated where it is displayed, not here."""
    _('OpenAI')


def base_name(name):
    """The name a model's settings are listed under: without a date, a fine-tune, or a healthcare variant."""
    if name.startswith('ft:'):
        name = name[3:].split(':', 1)[0]
    return SNAPSHOT.sub('', name).removesuffix('-healthcare')


def kind(name):
    """What a listed model does: `chat`, `embedding`, `image`, or None for one left out."""
    base = base_name(name)
    if base.startswith('text-embedding-'):
        return 'embedding'
    if base.startswith(('gpt-image-', 'chatgpt-image')):
        return 'image'
    if LEFT_OUT.search(base):
        return None
    return 'chat' if base in MODELS or base.startswith(('gpt-', 'o', 'chat')) else None


def model_settings(name):
    """The efforts and sampling rule of a chat model. One not in the table is taken to reason as most do."""
    base = base_name(name)
    if base in MODELS:
        return MODELS[base]
    if base.startswith(('gpt-4.1', 'gpt-4o')):
        return (), ALWAYS
    return (('high',) if base.endswith('-pro') else TO_HIGH), NEVER


def thinking_levels(efforts):
    """The application's thinking settings for a model's efforts; Max stands for its highest above High."""
    levels = [level for level, effort in LEVELS if effort in efforts]
    return levels + ['max'] if {'xhigh', 'max'} & set(efforts) else levels


def effort(thinking, efforts):
    """OpenAI's effort for a thinking setting, refused where the model has none for it."""
    names: dict = dict(LEVELS)
    names['max'] = 'max' if 'max' in efforts else 'xhigh'
    if type(thinking) not in (bool, str) or thinking not in names or names[thinking] not in efforts:
        raise ProviderError(_('Invalid thinking setting'))
    return names[thinking]


def limits(name):
    known = LIMITS.get(base_name(name))
    return {'context_length': known[0], 'max_completion_tokens': known[1]} if known else {}


def describe(model):
    """Details of a chat model, in the shape the other hosted types give."""
    efforts, sampling = model_settings(model['id'])
    parameters = ['max_tokens', 'response_format', 'tools']
    parameters += ['reasoning'] if efforts else []
    parameters += ['temperature', 'top_p', 'logprobs'] if sampling != NEVER else []
    result: dict = {'capabilities': ['completion', 'vision', 'tools'] + (['thinking'] if efforts else []),
                    'supported_parameters': parameters}
    if efforts:
        result['thinking_levels'] = thinking_levels(efforts)
    result.update(limits(model['id']))
    if isinstance(model.get('shutdown_date'), str):
        result['shutdown_date'] = model['shutdown_date']
    return result


def describe_embedding(model):
    return {'capabilities': ['embedding'], 'context_length': EMBEDDING_CONTEXT}


def describe_image(model):
    settings = copy.deepcopy(IMAGE_SETTINGS)
    if base_name(model['id']) in OPAQUE:
        settings['background']['values'] = ['opaque']
    return {'capabilities': imaging.capabilities(True), 'image_parameters': settings}


def tag(model):
    return {'name': model['id'], 'digest': fingerprint(model['id'], model.get('created'))}


def words(value):
    """Text for a field that takes only text: a description or label given as JSON is sent as JSON text."""
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def json_parts(value):
    """Content parts for a value given as JSON: an object, or a list of objects, as JSON; anything else as text."""
    if isinstance(value, dict) or (isinstance(value, list) and value and all(isinstance(item, dict) for item in value)):
        return [{'type': 'input_json', 'data': copy.deepcopy(value)}]
    return [{'type': 'input_text', 'text': words(value)}]


def decision_question(name, question):
    """One question of the application's canonical form, as the Decisions API takes it."""
    kind = question.get('type')
    if kind not in QUESTION_TYPES:
        raise ProviderError(_('This server does not accept: {0}.').format(f'{kind} questions'))
    instructions = question.get('instructions')
    criteria = question.get('criteria')
    # Instructions are required; a question without them asks what its name says.
    parts = json_parts(instructions) if instructions is not None and not isinstance(instructions, str) else []
    prompt = instructions if isinstance(instructions, str) else ('' if parts else name)
    if kind == 'noul' and criteria:
        # A predicate has no place of its own for what true and false mean, so the instructions say it.
        meaning = '\n'.join(f'{key}: {words(value)}' for key, value in criteria.items() if value is not None)
        prompt = '\n\n'.join(part for part in (prompt, meaning) if part)
    if parts:
        result_instructions: object = parts + ([{'type': 'input_text', 'text': prompt}] if prompt else [])
    else:
        result_instructions = prompt
    result: dict = {'type': QUESTION_TYPES[kind], 'name': name, 'instructions': result_instructions}
    if kind == 'choice':
        result['choices'] = [dict({'value': value}, **({'description': words(meaning)} if meaning is not None else {}))
                             for value, meaning in criteria.items()]
    elif kind == 'score':
        result['levels'] = [{'label': words(level)} for level in criteria]
    return result


def decision_input(state, images=None):
    """The state, with any images, as the input of a decision."""
    if not images and isinstance(state, str):
        return state
    parts = [] if state in (None, '') else (
        [{'type': 'input_text', 'text': state}] if isinstance(state, str) else json_parts(state))
    parts += [{'type': 'input_image', 'image_url': image_url(image)} for image in images or ()]
    return [{'role': 'user', 'content': parts}]


def decision_body(model, state, questions, images=None):
    return {'model': model, 'input': decision_input(state, images),
            'questions': [decision_question(name, question) for name, question in questions.items()]}


def system_one_answer(answer):
    """One answer of the Decisions API in the shape `decision.parse_result` reads; one it does not know stays."""
    kind = answer.get('type')
    rest = {key: copy.deepcopy(value) for key, value in answer.items()
            if key not in ('type', 'name', 'probability', 'choice', 'score', 'probabilities')}
    rows = [row for row in answer.get('probabilities') or [] if isinstance(row, dict)]
    if kind == 'predicate':
        return dict(rest, type='noul', noul=answer.get('probability'))
    if kind == 'choice':
        return dict(rest, type='choice', choice=answer.get('choice'),
                    probabilities={str(row.get('value')): row.get('probability') for row in rows})
    if kind == 'score':
        return dict(rest, type='score', score=answer.get('score'),
                    probabilities={str(row.get('value')): row.get('probability') for row in rows},
                    legend={str(row.get('value')): row.get('label') for row in rows})
    return copy.deepcopy(answer)


def system_one_result(raw, questions):
    """A decision as System One answers one: answers by question name, the rest as it came."""
    answers = raw.get('answers')
    if not isinstance(answers, list):
        raise ProviderError(_('The response has no answers.'))
    names = list(questions)
    named_answers = {}
    for index, answer in enumerate(answers):
        if not isinstance(answer, dict):
            continue
        # Each answer names its question; where it does not, its place does.
        name = answer.get('name') if answer.get('name') in questions else (
            names[index] if index < len(names) else None)
        if name is not None and name not in named_answers:
            named_answers[name] = system_one_answer(answer)
    return dict({key: copy.deepcopy(value) for key, value in raw.items() if key != 'answers'}, answers=named_answers)


def input_items(messages, images=None):
    """History in Ollama's shape as Responses input items; `images` go on a final user message."""
    items: list = []
    for message in wire_messages(messages, images):
        role, content = message['role'], message.get('content')
        if role == 'tool':
            items.append({'type': 'function_call_output', 'call_id': message['tool_call_id'],
                          'output': content if isinstance(content, str) else json.dumps(content)})
            continue
        if isinstance(content, list):
            kind = 'output_text' if role == 'assistant' else 'input_text'
            content = [{'type': kind, 'text': part['text']} if part['type'] == 'text'
                       else {'type': 'input_image', 'image_url': part['image_url']['url']} for part in content]
        if content or role != 'assistant':
            items.append({'role': role, 'content': content})
        for call in message.get('tool_calls') or []:
            function = call['function']
            items.append({'type': 'function_call', 'call_id': call['id'], 'name': function['name'],
                          'arguments': function['arguments']})
    return items


def flat_tools(tools):
    """Tools in the chat shape, as the Responses API takes them; strict mode would refuse loose schemas."""
    result = []
    for tool in tools:
        function = table(table(tool).get('function'))
        if table(tool).get('type') != 'function' or not function:
            raise ProviderError(_('This server does not accept: {0}.').format(table(tool).get('type')))
        result.append({'type': 'function', 'strict': False, **copy.deepcopy(function)})
    return result


def responses_body(model, messages, options=None, thinking=None, logprobs=False, top_logprobs=None,
                   images=None, format=None, keep_alive=None, tools=None):
    """A streamed Responses API request; nothing is kept on the server."""
    efforts, _sampling = model_settings(model)
    data: dict = {'model': model, 'input': input_items(messages, images), 'stream': True, 'store': False}
    refused = sorted(set(options or {}) - set(OPTION_NAMES)) + (['keep_alive'] if keep_alive is not None else [])
    if refused:
        raise ProviderError(_('This server does not accept: {0}.').format(', '.join(refused)))
    for name, value in (options or {}).items():
        if name == 'num_predict' and isinstance(value, (int, float)) and value < 0:
            continue
        data[OPTION_NAMES[name]] = copy.deepcopy(value)
    if format is not None:
        if format == 'json':
            data['text'] = {'format': {'type': 'json_object'}}
        elif isinstance(format, dict):
            data['text'] = {'format': {'type': 'json_schema', 'name': 'response', 'schema': copy.deepcopy(format),
                                       'strict': False}}
        else:
            raise ProviderError(_('Invalid output format'))
    if efforts:
        # Asked for whatever the effort, so that what the model thought is shown.
        data['reasoning'] = {'summary': 'auto'}
        if thinking is not None:
            data['reasoning']['effort'] = effort(thinking, efforts)
    elif thinking not in (None, False):
        raise ProviderError(_('Invalid thinking setting'))
    if logprobs:
        data['include'] = ['message.output_text.logprobs']
        if top_logprobs is not None:
            if not isinstance(top_logprobs, int) or not 0 <= top_logprobs <= 20:
                raise ProviderError(_('Top logprobs must be between 0 and 20'))
            data['top_logprobs'] = top_logprobs
    if tools:
        data['tools'] = flat_tools(tools)
    return data


def decode_event(line):
    """One line of the event stream: an event object, or None for event names and comments."""
    line = line.strip()
    if not line.startswith(b'data:'):
        return None
    try:
        event = json.loads(line[5:].strip())
    except (ValueError, UnicodeError):
        event = None
    if not isinstance(event, dict):
        raise ProviderError(_('Invalid JSON received from the server'))
    kind = event.get('type')
    if kind == 'error':
        raise ProviderError(error_text(event.get('error') or event) or _('The response failed.'))
    if kind == 'response.failed':
        raise ProviderError(error_text(table(event.get('response')).get('error')) or _('The response failed.'))
    return event


# Why an incomplete response stopped, in the words a finish reason uses elsewhere.
STOPPED = {'max_output_tokens': 'length', 'content_filter': 'content_filter'}


@dataclass(frozen=True)
class OpenAIProvider(OpenAICompatibleProvider):
    kind = 'openai_official'
    label = default_name = name_example = 'OpenAI'
    url_example = ''  # The address is fixed.
    key_url = 'https://platform.openai.com/api-keys'
    fixed_url = URL
    requires_key = True
    key_optional = False
    optional_suffix = ''
    # The Responses API has no seed and no stop sequences.
    capabilities = Capabilities(embeddings=True, thinking_levels=True, structured_output=True, tools=True,
                                logprobs=True, thinking_text=True, image_generation=True, image_references=True,
                                decisions=True, decision_images=True, decision_question_limit=400,
                                decision_image_formats=systemone.ASSET_FORMATS, left_out=('seed', 'stop'))

    @classmethod
    def check_url(cls, url):
        if url != cls.fixed_url:
            raise ValueError(_('OpenAI requires {0}.').format(cls.fixed_url))

    def _address(self, endpoint):
        if self.url != self.fixed_url:
            raise ProviderError(_('OpenAI requires {0}.').format(self.fixed_url))
        return super()._address(endpoint)

    def _explain(self, status, detail):
        # Only a 401 is about the key. A 403 is a region, a verification, or a project's limits on models, and
        # a 429 for want of credit is not a rate limit: OpenAI's words say which.
        if status == 403 or (status == 429 and table(detail).get('code') == 'insufficient_quota'):
            return error_text(detail)
        return super()._explain(status, detail)

    def model_capabilities(self, details):
        return narrowed(self.capabilities, details)

    def check(self, wire, host, model, details):
        super().check(wire, host, model, details)
        if kind(model) != 'chat':
            return
        _efforts, sampling = model_settings(model)
        sampled = [name for name in SAMPLING if name in (wire.options or {})] + (['logprobs'] if wire.logprobs else [])
        thinking_off = wire.thinking is False or (wire.thinking is None and sampling == DEFAULT)
        if sampled and sampling in (DEFAULT, OFF) and not thinking_off:
            name = host.get('name') or host.get('hostname', '')
            raise ValueError(_('{host} takes Temperature, Top P, and logprobs only with thinking Off.').format(
                host=f'{name} ({model})'))

    def request_body(self, model, messages, *settings, **named):
        return responses_body(model, messages, *settings, **named)

    def _catalog(self, wanted, timeout, cancellable):
        return [model for model in self._listed(lambda: self._address('/models'), 'data', 'id', timeout, cancellable)
                if kind(model['id']) == wanted]

    def list_models(self, timeout=10, cancellable=None):
        return [tag(model) for model in self._catalog('chat', timeout, cancellable)]

    def list_embedding_models(self, timeout=10, cancellable=None):
        return [dict(tag(model), context_length=EMBEDDING_CONTEXT)
                for model in self._catalog('embedding', timeout, cancellable)]

    def list_image_models(self, timeout=10, cancellable=None):
        return [dict(tag(model), **describe_image(model)) for model in self._catalog('image', timeout, cancellable)]

    def model_tag(self, name, timeout=10, cancellable=None):
        listed = self._listed(lambda: self._address('/models'), 'data', 'id', timeout, cancellable)
        return named([tag(model) for model in listed if kind(model['id']) in ('chat', 'embedding')], name)

    def embedding_context(self, name, timeout=10, cancellable=None):
        find(self._catalog('embedding', timeout, cancellable), 'id', name)
        return EMBEDDING_CONTEXT

    def show_model(self, name, timeout=10, cancellable=None):
        model = find(self._listed(lambda: self._address('/models'), 'data', 'id', timeout, cancellable), 'id', name)
        describer = {'chat': describe, 'embedding': describe_embedding, 'image': describe_image}.get(kind(name))
        if describer is None:
            raise ProviderError(_('Selected model is no longer available.'))
        return describer(model)

    def list_decision_models(self, timeout=10, cancellable=None):
        return [dict(tag(model), capabilities=systemone.capabilities(True))
                for model in self._listed(lambda: self._address('/models'), 'data', 'id', timeout, cancellable)
                if base_name(model['id']) in DECIDERS]

    def decide(self, model, state, questions, images=None, keep_alive=None, timeout=120, cancellable=None):
        """Ask a decision model the named `questions` about `state`; returns the answer in System One's shape."""
        if keep_alive is not None:
            raise ProviderError(_('This server does not accept: {0}.').format('keep_alive'))
        body = decision_body(model, state, questions, images)
        raw = self.transport.request(lambda: self._address('/decisions'), lambda: body, 'POST', timeout, cancellable,
                                     **self._hooks())
        return system_one_result(raw, questions)

    def generate_image(self, model, prompt, options, references=None, timeout=300, cancellable=None):
        """Ask an image model for images of `prompt`; returns the server's answer as it came."""
        options = imaging.chosen(options, tuple(name for name in IMAGE_SETTINGS if name != 'input_references'))
        ratio = options.pop('aspect_ratio', None)
        if ratio is not None:
            if ratio not in SIZES:
                raise ProviderError(_('This server does not accept: {0}.').format(f'aspect_ratio {ratio}'))
            options['size'] = SIZES[ratio]
        if references:
            # Edits take the images as JSON where each is a data URL.
            body = {'model': model, 'prompt': prompt, **options,
                    'images': [{'image_url': url} for url in imaging.urls(references)]}
            endpoint = '/images/edits'
        else:
            body, endpoint = {'model': model, 'prompt': prompt, **options}, '/images/generations'
        return imaging.post(self, lambda: self._address(endpoint), body, timeout, cancellable)

    def chat(self, model, messages, options=None, thinking=None, logprobs=False,
             top_logprobs=None, images=None, timeout=300, cancellable=None, format=None,
             keep_alive=None, tools=None):
        data = self.request_body(model, messages, options, thinking, logprobs, top_logprobs, images,
                                 format, keep_alive, tools)
        parts: dict = {}   # Calls by their place in the output.
        summaries: set = set()  # (item, index) of each summary part begun.
        finish: str | None = None
        usage: dict = {}
        lines = self.transport.stream_lines(lambda: self._address('/responses'), lambda: data,
                                            timeout, cancellable, decode=decode_event, **self._hooks())
        try:
            for event in lines:
                if event is None:
                    continue
                kind = event.get('type')
                chunk: dict = {'message': {'content': '', 'thinking': ''}}
                if kind == 'response.output_text.delta':
                    chunk['message']['content'] = text(event.get('delta'))
                    if isinstance(event.get('logprobs'), list) and event['logprobs']:
                        chunk['logprobs'] = event['logprobs']
                elif kind == 'response.reasoning_summary_part.added':
                    # Each part of a summary is a paragraph of its own.
                    if summaries:
                        chunk['message']['thinking'] = '\n\n'
                    summaries.add((event.get('item_id'), event.get('summary_index')))
                elif kind == 'response.reasoning_summary_text.delta':
                    chunk['message']['thinking'] = text(event.get('delta'))
                elif kind in ('response.output_item.added', 'response.output_item.done'):
                    item = table(event.get('item'))
                    if item.get('type') == 'function_call':
                        part = parts.setdefault(event.get('output_index'), {'id': '', 'name': '', 'arguments': ''})
                        part['id'] = text(item.get('call_id')) or part['id']
                        part['name'] = text(item.get('name')) or part['name']
                        part['arguments'] = text(item.get('arguments')) or part['arguments']
                elif kind == 'response.function_call_arguments.delta':
                    part = parts.setdefault(event.get('output_index'), {'id': '', 'name': '', 'arguments': ''})
                    part['arguments'] += text(event.get('delta'))
                elif kind in ('response.completed', 'response.incomplete'):
                    response = table(event.get('response'))
                    usage = table(response.get('usage'))
                    if kind == 'response.incomplete':
                        reason = text(table(response.get('incomplete_details')).get('reason'))
                        finish = STOPPED.get(reason) or reason or 'incomplete'
                    else:
                        finish = 'tool_calls' if parts else 'stop'
                    break
                if chunk['message']['content'] or chunk['message']['thinking'] or 'logprobs' in chunk:
                    yield chunk
        finally:
            lines.close()
        last: dict = {'message': {'content': '', 'thinking': ''}}
        if parts:
            last['message']['tool_calls'] = whole_calls(
                {index: part for index, part in parts.items() if isinstance(index, int)})
        if finish is None:
            # Ended early: hand over what arrived and let the caller report the missing end.
            if parts:
                yield last
            return
        last.update(done=True, done_reason=finish)
        counts = (('prompt_eval_count', usage.get('input_tokens')), ('eval_count', usage.get('output_tokens')),
                  ('prompt_eval_cached_count', table(usage.get('input_tokens_details')).get('cached_tokens')))
        last.update({key: value for key, value in counts if type(value) is int})
        yield last
