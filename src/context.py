"""Advisory text estimates; never modifies an inference request."""
import json
import math


def estimate_context(messages, settings, retrieval=None):
    def tokens(value):
        if not isinstance(value, str):
            value = json.dumps(value, ensure_ascii=False) if value else ''
        return math.ceil(len(value.encode('utf-8')) / 4)
    parts = dict(history=0, prompt=0, system=tokens(settings.get('system') or ''),
                 tools_schema=tokens(settings.get('tools')) + tokens(settings.get('format')),
                 retrieval=sum(tokens(h.get('text', '')) for h in (retrieval or {}).get('hits', [])))
    visible = [m for m in messages if m.get('role') != 'system']
    for index, message in enumerate(visible):
        parts['prompt' if index == len(visible) - 1 and message['role'] == 'user' else 'history'] += tokens(message.get('content', ''))
        parts['history'] += tokens(message.get('thinking', message.get('thinking_content', ''))) + tokens(message.get('tool_calls'))
    options = settings.get('options', {})
    reserve = max(0, options.get('num_predict') or 0)
    capacity = options.get('num_ctx') or None
    total = sum(parts.values())
    return dict(parts=parts, text_tokens=total, output_allowance=reserve, explicit_context=capacity,
        warning=bool(capacity and total + reserve >= .8 * capacity),
        image_overhead_unknown=any(m.get('images') for m in messages), template_overhead_unknown=True)


def describe_context(estimate):
    from gettext import gettext as _
    lines = [_('Estimated text tokens: {0} (UTF-8 bytes ÷ 4)').format(estimate['text_tokens']),
        _('History: {history}; prompt: {prompt}; system: {system}; tools/schema: {tools_schema}; sources: {retrieval}').format(**estimate['parts']),
        _('Output allowance: {0}').format(estimate['output_allowance'])]
    lines.append(_('Explicit context: {0}').format(estimate['explicit_context']) if estimate['explicit_context'] else
                 _('Context size uses the server default; model capacity may differ.'))
    lines.append(_('Image and chat-template overhead are unknown. This is an advisory estimate.'))
    if estimate['warning']:
        lines.append(_('Estimated input and output allowance reach {percent} of the configured context.').format(percent='80%'))
    return '\n'.join(lines)
