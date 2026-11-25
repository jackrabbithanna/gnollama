"""Decision (System One) questions, requests, and answers; no GTK and no network."""
from gettext import gettext as _
import copy
import math
from dataclasses import dataclass, field, replace

from .structured import parse_json


TYPES = ('noul', 'choice', 'score', 'box')
# The most criteria a question of each type takes; each needs at least one.
LIMITS = {'choice': 255, 'score': 10}
EDGES = ('top', 'bottom', 'left', 'right')
# The shortest and longest side, in pixels, of an image that the models known to answer box questions take.
BOX_SIDES = (256, 1568)


class InvalidQuestions(ValueError):
    """Questions need editing before a request can be sent."""


@dataclass(frozen=True)
class Question:
    name: str
    type: str
    instructions: str | dict | list | None = None
    # An object for noul and choice, an ordered list of levels for score.
    criteria: dict | list | None = None


@dataclass(frozen=True)
class Answer:
    name: str
    # A question type, or refusal, missing, or invalid.
    type: str
    # The probability of yes for noul, the chosen name for choice, the score for score.
    value: float | str | None = None
    confidence: float | None = None
    probabilities: dict = field(default_factory=dict)
    legend: dict = field(default_factory=dict)
    # Edges as fractions of the image: top, bottom, left, right.
    box: dict | None = None
    note: str = ''
    # Fields this version does not know; for an invalid answer, everything the host sent.
    extra: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Result:
    model: str
    answers: tuple
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost: float | None = None
    extra: dict = field(default_factory=dict)


def _content(value, null=True):
    return isinstance(value, (str, dict, list)) or (null and value is None)


def _check_criteria(name, kind, criteria):
    def invalid(path):
        return ValueError(_('Question "{0}" has an invalid field: {1}').format(name, path))

    def count():
        return ValueError(_('Question "{0}" needs between 1 and {1} criteria.').format(name, LIMITS[kind]))

    if kind == 'box':
        if criteria is not None:
            raise invalid('criteria')
    elif kind == 'score':
        if criteria is None:
            raise count()
        if not isinstance(criteria, list):
            raise invalid('criteria')
        if not 0 < len(criteria) <= LIMITS[kind]:
            raise count()
        for index, value in enumerate(criteria):
            if not _content(value, null=False):
                raise invalid(f'criteria[{index}]')
    elif criteria is None:
        if kind == 'choice':
            raise count()
    elif not isinstance(criteria, dict):
        raise invalid('criteria')
    elif kind == 'choice' and not 0 < len(criteria) <= LIMITS[kind]:
        raise count()
    else:
        for key, value in criteria.items():
            if not isinstance(key, str) or (kind == 'noul' and key not in ('true', 'false')) or not _content(value):
                raise invalid(f'criteria.{key}')


def parse_questions(value):
    """Questions from JSON text, an object of named questions, or a sequence of Question records."""
    empty = _('Provide at least one named question as a JSON object.')
    try:
        if isinstance(value, str):
            if not value.strip():
                raise ValueError(empty)
            value = parse_json(value)
        if isinstance(value, dict):
            pairs = list(value.items())
        elif isinstance(value, (list, tuple)) and all(isinstance(item, Question) for item in value):
            pairs = [(item.name, {'type': item.type, 'instructions': item.instructions, 'criteria': item.criteria})
                     for item in value]
        else:
            raise ValueError(empty)
        if not pairs:
            raise ValueError(empty)
        questions: list[Question] = []
        names = set()
        for name, question in pairs:
            if not isinstance(name, str) or not name.strip() or name in names:
                raise ValueError(_('Question names must be unique and not blank: "{0}"').format(name))
            names.add(name)
            kind = question.get('type') if isinstance(question, dict) else None
            if not isinstance(kind, str) or kind not in TYPES:
                raise ValueError(_('Question "{0}" needs a type of noul, choice, score, or box.').format(name))
            for key in question:
                if key not in ('type', 'instructions', 'criteria') or (
                        key == 'instructions' and not _content(question[key])):
                    raise ValueError(_('Question "{0}" has an invalid field: {1}').format(name, key))
            _check_criteria(name, kind, question.get('criteria'))
            questions.append(Question(name, kind, copy.deepcopy(question.get('instructions')),
                                      copy.deepcopy(question.get('criteria'))))
        return questions
    except (ValueError, RecursionError) as exc:
        raise InvalidQuestions(str(exc)) from exc


def questions_object(questions):
    """The canonical JSON form: what is exported, and what a request carries."""
    result = {}
    for question in questions:
        item: dict = {'type': question.type}
        if question.instructions is not None:
            item['instructions'] = copy.deepcopy(question.instructions)
        if question.criteria is not None:
            item['criteria'] = copy.deepcopy(question.criteria)
        result[question.name] = item
    return result


def check_request(questions, images, capabilities):
    """Why a host and model with these capabilities cannot be sent this request, or ''."""
    images = images or ()
    boxes = any(question.type == 'box' for question in questions)
    limit = getattr(capabilities, 'decision_question_limit', 0)
    if not getattr(capabilities, 'decisions', False):
        return _('This server does not offer decision models.')
    if limit and len(questions) > limit:
        return _('This model takes at most {0} questions.').format(limit)
    if images and not getattr(capabilities, 'decision_images', False):
        return _('This model does not accept images.')
    if boxes and not getattr(capabilities, 'decision_boxes', False):
        return _('This model cannot answer box questions.')
    if boxes and len(images) != 1:
        return _('A box question needs exactly one image.')
    return ''


def box_size_hint(boxes, sizes):
    """Advice on images of these (width, height) sizes where a box question is asked, or ''. Nothing is refused for it."""
    low, high = BOX_SIDES
    if boxes and any(min(size) < low or max(size) > high for size in sizes):
        return _('Some models need images between 256 and 1568 pixels on each side.')
    return ''


def build_request(model, state, questions):
    """The request without images; how those are carried differs from host to host."""
    return {'model': model, 'state': copy.deepcopy(state), 'questions': questions_object(questions)}


def _number(value, low=0.0, high=1.0):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value) and low <= value <= high
    except OverflowError:
        return False


def _probabilities(raw):
    value = raw.get('probabilities')
    if value is None:
        return {}
    if not isinstance(value, dict) or not all(_number(item) for item in value.values()):
        raise ValueError('probabilities')
    return {key: float(item) for key, item in value.items()}


def _read_answer(question, raw):
    """The answer to one question; raises ValueError for one that does not fit it."""
    kind = question.type
    if raw.get('type') != kind:
        raise ValueError('type')
    confidence = raw.get('confidence')
    if confidence is not None:
        if not _number(confidence):
            raise ValueError('confidence')
        confidence = float(confidence)
    known = {'type', kind, 'confidence'}
    value = raw.get(kind)
    if kind == 'noul':
        if not _number(value):
            raise ValueError(kind)
        answer = Answer(question.name, kind, float(value), confidence)
    elif kind == 'choice':
        if not isinstance(value, str) or value not in question.criteria:
            raise ValueError(kind)
        known.add('probabilities')
        answer = Answer(question.name, kind, value, confidence, _probabilities(raw))
    elif kind == 'score':
        legend = raw.get('legend')
        if not _number(value, 0, len(question.criteria) - 1) or not isinstance(legend, (dict, type(None))):
            raise ValueError(kind)
        known.update(('probabilities', 'legend'))
        answer = Answer(question.name, kind, float(value), confidence, _probabilities(raw),
                        copy.deepcopy(legend or {}))
    else:
        if not isinstance(value, dict) or not all(_number(value.get(edge)) for edge in EDGES):
            raise ValueError(kind)
        if value['top'] > value['bottom'] or value['left'] > value['right']:
            raise ValueError(kind)
        answer = Answer(question.name, kind, confidence=confidence, box={edge: float(value[edge]) for edge in EDGES})
    extra = {key: copy.deepcopy(item) for key, item in raw.items() if key not in known}
    return replace(answer, extra=extra)


def _tokens(value):
    return value if type(value) is int and value >= 0 else None


def _cost(value):
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return None
    return float(value) if _number(value, 0, math.inf) else None


def parse_result(raw, questions):
    """A host's response as one Answer per question, in question order.

    Raises ValueError when the response is not an object with an `answers` object.
    """
    if isinstance(raw, str):
        try:
            raw = parse_json(raw)
        except RecursionError:
            raw = None
    answers = raw.get('answers') if isinstance(raw, dict) else None
    if not isinstance(answers, dict):
        raise ValueError(_('The response has no answers.'))
    parsed = []
    for question in questions:
        if question.name not in answers:
            parsed.append(Answer(question.name, 'missing'))
            continue
        answer = answers[question.name]
        if isinstance(answer, dict) and answer.get('type') == 'refusal':
            parsed.append(Answer(question.name, 'refusal', extra={
                key: copy.deepcopy(item) for key, item in answer.items() if key != 'type'}))
            continue
        try:
            if not isinstance(answer, dict):
                raise ValueError('type')
            parsed.append(_read_answer(question, answer))
        except ValueError:
            kept = copy.deepcopy(answer) if isinstance(answer, dict) else {'value': copy.deepcopy(answer)}
            parsed.append(Answer(question.name, 'invalid', note=_('The answer is not valid for this question.'),
                                 extra=kept))
    usage = raw.get('usage') if isinstance(raw.get('usage'), dict) else {}
    metadata = raw.get('provider_metadata')
    gateway = metadata.get('gateway') if isinstance(metadata, dict) else None
    cost = _cost(usage.get('cost'))
    if cost is None and isinstance(gateway, dict):
        cost = _cost(gateway.get('cost'))
    extra = {key: copy.deepcopy(item) for key, item in raw.items() if key not in ('model', 'answers', 'usage')}
    asked = {question.name for question in questions}
    unasked = {key: copy.deepcopy(item) for key, item in answers.items() if key not in asked}
    if unasked:
        extra['answers'] = unasked
    other_usage = {key: copy.deepcopy(item) for key, item in usage.items()
                   if key not in ('input_tokens', 'output_tokens', 'cost')}
    if other_usage:
        extra['usage'] = other_usage
    model = raw.get('model')
    return Result(model if isinstance(model, str) else '', tuple(parsed), _tokens(usage.get('input_tokens')),
                  _tokens(usage.get('output_tokens')), cost, extra)


def box_rect(box, image_w, image_h, width, height):
    """Where a box lies, as (x, y, w, h), in a widget showing its image whole and centred."""
    if min(image_w, image_h, width, height) <= 0:
        return (0.0, 0.0, 0.0, 0.0)
    scale = min(width / image_w, height / image_h)
    shown_w, shown_h = image_w * scale, image_h * scale
    left, top = (width - shown_w) / 2, (height - shown_h) / 2
    return (left + box['left'] * shown_w, top + box['top'] * shown_h,
            (box['right'] - box['left']) * shown_w, (box['bottom'] - box['top']) * shown_h)
