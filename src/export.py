"""Portable, credential-free conversation snapshots."""
from gettext import gettext as _
import copy
import json

from .decision import EDGES, parse_questions, parse_result
from .image_generation import LABELS, media_type


def portable(value):
    if isinstance(value, dict):
        return {k: portable(v) for k, v in value.items()
                if k.lower() not in ('credential_id', 'credential_ref', 'credential', 'credentials',
                                     'api_key', 'api-key', 'authorization', 'access_token', 'password')}
    if isinstance(value, list):
        return [portable(v) for v in value]
    return value


def _portable(snapshot):
    """A decision's questions and answers, and an image's settings, are named by the user and hold no
    credential, so they are kept whole."""
    whole = ('options', 'runs') if snapshot.get('kind') in ('decision', 'image') else ()
    kept = portable({key: value for key, value in snapshot.items() if key not in whole})
    kept.update((key, copy.deepcopy(snapshot[key])) for key in whole if key in snapshot)
    return kept


def export_json(snapshot):
    return json.dumps({'format': 'tureen-conversation', 'version': 1,
                       'conversation': _portable(copy.deepcopy(snapshot))}, ensure_ascii=False, indent=2) + '\n'


def _image(index, encoded, kind=None, label='Attachment'):
    """An image as a data URL, of its recorded type or, without one, the type its bytes show."""
    head, _sep, data = encoded.rpartition(',')
    if not kind and head.startswith('data:'):
        kind = head[5:].split(';', 1)[0]
    return f'![{label} {index + 1}](data:{kind or media_type(data)};base64,{data})'


def _text(value):
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _cell(value):
    return _text(value).replace('\\', '\\\\').replace('|', '\\|').replace('\n', '<br>')


def _percent(value):
    return f'{value * 100:.1f}%'


def _shares(values):
    return ', '.join(f'{key} {_percent(value)}' for key, value in sorted(values.items(), key=lambda item: -item[1]))


def _answer(answer):
    """One row of the answers table: the answer, how sure the model is, and what else it said."""
    confidence = '' if answer.confidence is None else _percent(answer.confidence)
    details = []
    if answer.type == 'noul':
        value = _percent(answer.value) + ' yes'
    elif answer.type == 'choice':
        value = answer.value
        details.append(answer.probabilities and 'Probabilities: ' + _shares(answer.probabilities))
    elif answer.type == 'score':
        value = f'{answer.value:.2f}'
        details.append(answer.legend and 'Legend: ' + '; '.join(
            f'{key} = {_text(text)}' for key, text in answer.legend.items()))
        details.append(answer.probabilities and 'Probabilities: ' + _shares(answer.probabilities))
    elif answer.type == 'box':
        value = ', '.join(f'{edge} {answer.box[edge]:.3f}' for edge in EDGES)
    else:
        value = {'refusal': 'Refused', 'missing': 'No answer'}.get(answer.type, 'Invalid answer')
    if answer.extra:
        details.append(json.dumps(answer.extra, ensure_ascii=False))
    return '| ' + ' | '.join(_cell(item) for item in (
        answer.name, answer.type, value, confidence, '\n'.join(filter(None, details)))) + ' |'


def _decision_run(run):
    target = run.get('target') or {}
    heading = filter(None, ('Run {}'.format(run['position'] + 1), target.get('model'),
                            target.get('host_name') or target.get('host')))
    parts = ['## ' + ' · '.join(heading), '', 'Status: ' + run['status'], '', '### State', '']
    parts += ['```json', run['state'], '```', ''] if run['state_mode'] == 'json' else [run['state'], '']
    for index, encoded in enumerate(run.get('images', [])):
        parts += [_image(index, encoded), '']
    parts += ['### Questions', '']
    for name, question in run['questions'].items():
        parts += [f'**{name}** ({question.get("type")})', '']
        if question.get('instructions') is not None:
            parts += [_text(question['instructions']), '']
        criteria = question.get('criteria')
        if criteria:
            pairs = enumerate(criteria) if isinstance(criteria, list) else criteria.items()
            parts += [f'- {key}: {_text(value)}' for key, value in pairs] + ['']
    if run['status'] == 'done':
        try:
            result = parse_result(run['result'], parse_questions(run['questions']))
        except ValueError as exc:
            return parts + ['Error: ' + str(exc), '']
        parts += ['### Answers', '', '| Question | Type | Answer | Confidence | Details |', '|---|---|---|---|---|']
        parts += [_answer(answer) for answer in result.answers] + ['']
        usage = [f'{label}: {value}' for label, value in (
            ('Model', result.model), ('Input tokens', result.input_tokens), ('Output tokens', result.output_tokens),
            ('Cost', None if result.cost is None else f'${result.cost:.6f}')) if value not in (None, '')]
        if usage:
            parts += [' · '.join(usage), '']
    elif run.get('error'):
        parts += ['Error: ' + run['error'], '']
    return parts


def _image_run(run):
    target = run.get('target') or {}
    heading = filter(None, ('Run {}'.format(run['position'] + 1), target.get('model'),
                            target.get('host_name') or target.get('host')))
    parts = ['## ' + ' · '.join(heading), '', 'Status: ' + run['status'], '', '### Prompt', '', run['prompt'], '']
    for index, encoded in enumerate(run.get('references', [])):
        parts += [_image(index, encoded, label='Reference'), '']
    if run.get('options'):
        parts += ['### Settings', '']
        parts += [f'- {LABELS.get(name, name)}: {_text(value)}' for name, value in run['options'].items()] + ['']
    if run.get('images') or run.get('text'):
        parts += ['### Result', '']
        if run.get('text'):
            parts += [run['text'], '']
        for index, image in enumerate(run.get('images', [])):
            parts += [_image(index, image['data'], image.get('media_type'), 'Image'), '']
    cost = (run.get('result') or {}).get('cost')
    if cost is not None:
        parts += [f'Cost: ${cost:.6f}', '']
    if run.get('error'):
        parts += ['Error: ' + run['error'], '']
    return parts


def export_markdown(snapshot):
    snapshot = _portable(snapshot)
    parts = ['# ' + snapshot['title'], '']
    for run in snapshot.get('runs', []):
        parts += _image_run(run) if snapshot.get('kind') == 'image' else _decision_run(run)
    model_conversation = snapshot.get('kind') == 'model_conversation'
    if model_conversation:
        settings = snapshot['options']
        parts += ['Rounds: ' + str(settings['rounds']), '', 'Status: ' + snapshot['run']['status'], '']
        for index, participant in enumerate(settings['participants']):
            parts += ['## Model ' + ('A' if index == 0 else 'B'), '',
                      participant['model'] + ' — ' + participant['host'], '',
                      participant.get('system') or '', '', '```json',
                      json.dumps(participant, ensure_ascii=False, indent=2), '```', '']
    if snapshot.get('system'):
        parts += ['## System', '', snapshot['system'], '']
    for message in snapshot.get('messages', []):
        heading = message.get('model', message['role'].title())
        if model_conversation and 'turn' in message:
            heading = 'Round {} · Model {} · Attempt {} · {}'.format(
                message['turn'] // 2 + 1, 'A' if message['participant'] == 0 else 'B', message['attempt'], heading)
        parts += ['## ' + heading, '', message.get('content', ''), '']
        for index, encoded in enumerate(message.get('images', [])):
            parts += [_image(index, encoded), '']
        metadata = message.get('response_metadata') or {}
        if metadata.get('status'):
            parts += ['Status: ' + metadata['status'], '']
        for hit in (metadata.get('retrieval') or {}).get('hits', []):
            parts += ['### ' + str(hit.get('label', hit.get('id', 'Source'))), '',
                      str(hit.get('title', '')), '', hit.get('text', ''), '']
    return '\n'.join(parts)


def choose_export(parent, context, chat_id, format):
    from gi.repository import Gtk, Gio, GLib
    from .widgets.feedback import toast
    dialog = Gtk.FileDialog(title=_('Export Conversation'), initial_name='conversation.' + ('json' if format == 'json' else 'md'))
    def chosen(dialog, result):
        try:
            file = dialog.save_finish(result)
        except GLib.Error:
            return
        # The writer barrier and read transaction define a saved snapshot.
        def ready():
            def write():
                try:
                    snapshot = context.storage.export_snapshot(chat_id)
                    if snapshot is None:
                        raise ValueError(_('Conversation no longer exists.'))
                    text = export_json(snapshot) if format == 'json' else export_markdown(snapshot)
                    file.replace_contents(text.encode('utf-8'), None, False, Gio.FileCreateFlags.REPLACE_DESTINATION, None)
                    GLib.idle_add(toast, parent, _('Conversation exported'))
                except Exception as exc:
                    GLib.idle_add(toast, parent, str(exc))
            context.services.control.submit(write)
        context.storage.after_pending_writes(ready)
    dialog.save(parent, None, chosen)
