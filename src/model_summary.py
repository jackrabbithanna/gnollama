"""What a model's details say about it, as short text for the model selector and the details dialog."""
from decimal import Context, Decimal, InvalidOperation
from gettext import gettext as _

from .providers.base import advisory, supports

MILLION = 1_000_000


def prices(details):
    """The prices in a model's details, as Decimals in US dollars per unit.

    An entry that is not a number is left out, never read as zero. So is a
    negative one, which a catalog uses to say the price varies.
    """
    table = details.get('pricing') if isinstance(details, dict) else None
    found = {}
    for name, value in (table.items() if isinstance(table, dict) else ()):
        try:
            price = Decimal(value) if isinstance(value, str) else None
        except InvalidOperation:
            continue
        if price is not None and price.is_finite() and price >= 0:
            found[name] = price
    return found


def is_free(costs):
    return 'prompt' in costs and 'completion' in costs and not any(costs.values())


def amount(price, units=1):
    """A price for `units` units, to four significant digits, without the zeros that say nothing."""
    value = Context(prec=4).create_decimal(price * units)
    if value == value.to_integral_value():
        return format(value.to_integral_value(), 'f')
    text = format(value.normalize(), 'f')
    # Cents read as two digits.
    return text + '0' if len(text.partition('.')[2]) < 2 else text


def token_count(count):
    """A number of tokens, rounded as people say them: 8192 is 8K."""
    if count >= 999_500:
        tenths = (count + 50_000) // 100_000
        # Translators: a number of tokens in millions, as in "1M context".
        return _('{0}M').format(f'{tenths // 10}.{tenths % 10}' if tenths % 10 else tenths // 10)
    if count >= 1000:
        # Translators: a number of tokens in thousands, as in "128K context".
        return _('{0}K').format((count + 500) // 1000)
    return str(count)


def _size(value):
    return value if type(value) is int and value > 0 else None


def capability_parts(details):
    """What a model can and cannot do. A capability the server did not report is left out, not shown as missing."""
    parts = []
    for name, has, lacks in (('vision', _('Images'), _('No images')), ('tools', _('Tools'), _('No tools')),
                             ('thinking', _('Thinking'), _('No thinking'))):
        known = supports(details, name)
        if known is False and name == 'tools' and advisory(details, name):
            parts.append(_('Not trained for tools'))
        elif known is not None:
            parts.append(has if known else lacks)
    return parts


def summary_parts(details):
    """Short statements about a model, in the order shown; none where its details say nothing."""
    if not isinstance(details, dict):
        return []
    parts = []
    costs = prices(details)
    if is_free(costs):
        parts.append(_('Free'))
    elif 'prompt' in costs and 'completion' in costs:
        # Translators: the prices of a model's input and output, in US dollars per million tokens.
        parts.append(_('${0} / ${1} per 1M tokens').format(amount(costs['prompt'], MILLION),
                                                           amount(costs['completion'], MILLION)))
    parts += capability_parts(details)
    loaded, most = _size(details.get('context_length')), _size(details.get('max_context_length'))
    if loaded:
        # Translators: {0} is a number of tokens, such as "128K".
        parts.append(_('{0} context').format(token_count(loaded)))
    elif most:
        # Translators: the largest context a model can be loaded with; {0} is a number of tokens, such as "128K".
        parts.append(_('{0} context max').format(token_count(most)))
    return parts


def price_lines(details):
    """Every price in a model's details, one to a line, for the details dialog; none where there are no prices."""
    costs = prices(details)
    if not costs:
        return []
    per_token = (('prompt', _('Prompt: ${0} per 1M tokens')), ('completion', _('Completion: ${0} per 1M tokens')),
                 ('input_cache_read', _('Cache read: ${0} per 1M tokens')),
                 ('input_cache_write', _('Cache write: ${0} per 1M tokens')),
                 ('input_cache_write_1h', _('Cache write, kept one hour: ${0} per 1M tokens')),
                 ('internal_reasoning', _('Reasoning: ${0} per 1M tokens')),
                 ('image', _('Image input: ${0} per 1M tokens')), ('audio', _('Audio input: ${0} per 1M tokens')),
                 ('input_audio_cache', _('Cached audio input: ${0} per 1M tokens')),
                 ('image_output', _('Image output: ${0} per 1M tokens')),
                 ('audio_output', _('Audio output: ${0} per 1M tokens')))
    each = (('web_search', _('Web search: ${0} per search')), ('request', _('Request: ${0} per request')))
    if is_free(costs):
        lines = [_('Free')]
    else:
        # The two every model has are shown even at zero; another is shown only where it costs something.
        lines = [text.format(amount(costs[name], units)) for names, units in ((per_token, MILLION), (each, 1))
                 for name, text in names if name in costs and (costs[name] or name in ('prompt', 'completion'))]
        named = {name for name, _text in per_token + each}
        # A price this table does not know is shown as the catalog gave it, per whatever it counts.
        lines += [_('{0}: ${1}').format(name.replace('_', ' ').capitalize(), amount(price))
                  for name, price in costs.items() if name not in named and price]
    return lines + [_('Prices are as listed when this was read; they can change.')]
