"""The runs of a decision session: one card for each answer, and what the run used."""
from gettext import gettext as _
from decimal import Decimal
import json
from gi.repository import Adw, Gtk, Pango
from ..decision import EDGES, box_rect, parse_questions, parse_result
from ..model_summary import amount
from .decision_form import compact, type_label
from .decision_images import load, thumbnail
from .feedback import copy_text
from .tool_view import text_block


def percent(value):
    # Translators: a percentage, such as "87%"; {0} is the number.
    return _('{0}%').format(round(value * 100))


def line(text, *classes, **kwargs):
    return Gtk.Label(label=text, xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, selectable=True,
                     css_classes=list(classes), **kwargs)


def bars(rows):
    """Probabilities, one to a row: what each is of, a bar, and the percentage."""
    grid = Gtk.Grid(column_spacing=8, row_spacing=4)
    for index, (label, value) in enumerate(rows):
        name = Gtk.Label(label=label, xalign=0, ellipsize=Pango.EllipsizeMode.END, max_width_chars=28, tooltip_text=label)
        level = Gtk.LevelBar(min_value=0, max_value=1, value=value, hexpand=True, valign=Gtk.Align.CENTER)
        level.update_property([Gtk.AccessibleProperty.LABEL], [label])
        # Without these a bar is coloured as a warning below a quarter full.
        for offset in (Gtk.LEVEL_BAR_OFFSET_LOW, Gtk.LEVEL_BAR_OFFSET_HIGH, Gtk.LEVEL_BAR_OFFSET_FULL):
            level.remove_offset_value(offset)
        grid.attach(name, 0, index, 1, 1)
        grid.attach(level, 1, index, 1, 1)
        grid.attach(Gtk.Label(label=percent(value), xalign=1, width_chars=5), 2, index, 1, 1)
    return [grid] if rows else []


def confidence(answer):
    return [line(_('Confidence: {0}').format(percent(answer.confidence)), 'dim-label')] if answer.confidence is not None else []


def raw_block(label, value):
    """JSON as the host sent it, folded away."""
    return Gtk.Expander(label=label, child=text_block(json.dumps(value, ensure_ascii=False, indent=2)))


def noul_card(question, answer, run, read):
    return ([line(_('Yes') if answer.value >= 0.5 else _('No'), 'title-3')]
            + bars([(_('Probability of yes'), answer.value)]) + confidence(answer))


def choice_card(question, answer, run, read):
    ranked = sorted(answer.probabilities.items(), key=lambda pair: -pair[1])
    return [line(answer.value, 'title-3')] + confidence(answer) + bars(ranked)


def score_card(question, answer, run, read):
    levels = question.criteria if isinstance(question.criteria, list) else []
    top = max(len(levels) - 1, 0)

    def described(level):
        """What a level means: as the host's legend says, else as the question does."""
        if str(level) in answer.legend:
            return compact(answer.legend[str(level)])
        return compact(levels[level]) if 0 <= level < len(levels) else ''
    nearest = min(max(int(answer.value + 0.5), 0), top)
    # Translators: a score and the highest it could be, such as "1.54 of 2".
    widgets = [line(_('{0} of {1}').format(f'{answer.value:.2f}', top), 'title-3')]
    if described(nearest):
        widgets.append(line(described(nearest)))
    rows = []
    for level in sorted(answer.probabilities, key=lambda key: (not key.isdigit(), int(key) if key.isdigit() else 0, key)):
        meaning = described(int(level)) if level.isdigit() else ''
        rows.append((f'{level} · {meaning}' if meaning else level, answer.probabilities[level]))
    return widgets + confidence(answer) + bars(rows)


class BoxPicture(Adw.Bin):
    """An image with the box a model found in it drawn over it; hidden until the image is read.

    An overlay of the picture and of an area the box is drawn in, measured here: a picture that
    may shrink asks for no height at all, and a scrolled page gives it none.
    """
    # The longest side the image is given, in pixels, and the least width it asks for: a narrow window shrinks it.
    SIDE, NARROW = 480, 120
    # The line and what sets it off from the image under it, as (red, green, blue, alpha) and width.
    HALO = ((1.0, 1.0, 1.0, 0.9), 6.0)

    def __init__(self, box, description):
        super().__init__(halign=Gtk.Align.START, visible=False)
        self.box = box
        # The width and height of the image as it was sent, once it is read.
        self.size = None
        self.picture = Gtk.Picture(can_shrink=True, content_fit=Gtk.ContentFit.CONTAIN, alternative_text=description)
        self.area = Gtk.DrawingArea(can_target=False)
        self.area.set_draw_func(self.draw)
        overlay = Gtk.Overlay(child=self.picture)
        overlay.add_overlay(self.area)
        # Without a layout manager the measure and allocation below are used.
        self.set_layout_manager(None)
        self.set_child(overlay)

    def show_image(self, seen):
        if seen is None:
            return
        self.size = (seen.width, seen.height)
        self.picture.set_paintable(seen.texture)
        self.set_visible(True)
        self.queue_resize()
        self.area.queue_draw()

    def do_get_request_mode(self):
        return Gtk.SizeRequestMode.HEIGHT_FOR_WIDTH

    def do_measure(self, orientation, for_size):
        """The image's own size up to SIDE, and for a width exactly the height that keeps its shape."""
        if not self.size:
            return (0, 0, -1, -1)
        scale = min(1, self.SIDE / max(self.size))
        width = max(1, round(self.size[0] * scale))
        if orientation == Gtk.Orientation.HORIZONTAL:
            return (min(width, self.NARROW), width, -1, -1)
        height = max(1, round((width if for_size < 0 else min(for_size, width)) * self.size[1] / self.size[0]))
        return (height, height, -1, -1)

    def do_size_allocate(self, width, height, baseline):
        self.get_child().allocate(width, height, baseline, None)

    def rect(self, width, height):
        """Where the box is drawn in an area of this size, as (x, y, w, h); None before the image is read."""
        return box_rect(self.box, *self.size, width, height) if self.size else None

    def strokes(self):
        """What is drawn, widest first: a pale halo, then the accent colour, so one shows on any image."""
        accent = Adw.StyleManager.get_default().get_accent_color_rgba()
        return [self.HALO, ((accent.red, accent.green, accent.blue, 1.0), 3.0)]

    def draw(self, area, context, width, height):
        rect = self.rect(width, height)
        if rect is None:
            return
        for colour, thickness in self.strokes():
            context.set_source_rgba(*colour)
            context.set_line_width(thickness)
            context.rectangle(*rect)
            context.stroke()


def box_card(question, answer, run, read):
    # Translators: where a box lies in an image; each number is a fraction of the image's height or width.
    edges = _('Box: top {0}, bottom {1}, left {2}, right {3}').format(*(f'{answer.box[edge]:.2f}' for edge in EDGES))
    widgets = [line(edges)] + confidence(answer)
    images = run.get('images') or []
    if not images:
        return widgets
    # A box question is asked of one image. Without it, or if it cannot be read, the numbers stand alone.
    picture = BoxPicture(answer.box, edges)
    read(images[0], picture.show_image)
    return [picture] + widgets


def refusal_card(question, answer, run, read):
    return [line(_('The model declined to answer this question.'), 'warning')] + (
        [raw_block(_('Raw answer'), answer.extra)] if answer.extra else [])


def missing_card(question, answer, run, read):
    return [line(_('The model returned no answer to this question.'), 'warning')]


def invalid_card(question, answer, run, read):
    return [line(answer.note, 'error'), raw_block(_('Raw answer'), answer.extra)]


# What fills the card of each type of answer: a function of the question, its answer, the run they belong to,
# and `read(image, done)`, which hands `done` what `decision_images.load` reads of one of the run's images.
CARDS = {'noul': noul_card, 'choice': choice_card, 'score': score_card, 'box': box_card,
         'refusal': refusal_card, 'missing': missing_card, 'invalid': invalid_card}


def answer_card(question, answer, run, read=load):
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_start=12, margin_end=12,
                  margin_top=12, margin_bottom=12)
    head = Gtk.Box(spacing=8)
    head.append(line(question.name, 'heading', hexpand=True))
    head.append(Gtk.Label(label=type_label(question.type), css_classes=['dim-label', 'caption']))
    box.append(head)
    if isinstance(question.instructions, str) and question.instructions:
        box.append(line(question.instructions, 'dim-label'))
    for widget in CARDS[answer.type](question, answer, run, read):
        box.append(widget)
    card = Gtk.Frame(child=box)
    card.answer = answer
    return card


def usage_text(result):
    """The model that answered and what the answer used, as far as the host said."""
    parts = [result.model] if result.model else []
    if result.input_tokens is not None:
        parts.append(_('Input: {0} tokens').format(result.input_tokens))
    if result.output_tokens is not None:
        parts.append(_('Output: {0} tokens').format(result.output_tokens))
    if result.cost is not None:
        # Translators: what a request cost, in US dollars.
        parts.append(_('Cost: ${0}').format(amount(Decimal(str(result.cost)))))
    return ' · '.join(parts)


def state_summary(run):
    """The start of a run's state, on one line."""
    words = ' '.join(run['state'].split())
    return words[:200] + '…' if len(words) > 200 else words


class RunView(Gtk.Box):
    """One run: what was asked of which model, how it ended, and its answers.

    With `submit`, which runs a function on a worker, the run's images are read there and shown when ready.
    """
    def __init__(self, run, submit=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.submit = submit
        self.title = line('', 'heading')
        self.spinner = Gtk.Spinner(visible=False)
        head = Gtk.Box(spacing=8)
        head.append(self.spinner)
        head.append(self.title)
        self.append(head)
        self.state = line('', 'dim-label')
        self.append(self.state)
        self.images = Adw.WrapBox(child_spacing=6, line_spacing=6, visible=bool(run.get('images')))
        for image in run.get('images') or []:
            self.images.append(thumbnail(image, _('Image sent with this run'), submit))
        self.append(self.images)
        self.cards = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.append(self.cards)
        self.usage = line('', 'dim-label', 'caption')
        self.append(self.usage)
        self.error = line('', 'error')
        self.append(self.error)
        self.copy_button = Gtk.Button(label=_('Copy JSON'), halign=Gtk.Align.START)
        self.copy_button.connect('clicked', lambda button: copy_text(button, self.raw))
        self.append(self.copy_button)
        self.update(run)

    @property
    def raw(self):
        """The answer as the host sent it."""
        return json.dumps(self.run['result'], ensure_ascii=False, indent=2)

    def update(self, run):
        self.run = run
        status = run['status']
        target = run.get('target') or {}
        ended = {'failed': _('Failed'), 'cancelled': _('Cancelled'), 'interrupted': _('Interrupted'),
                 'running': _('Running…')}.get(status)
        parts = [_('Run {0}').format(run['position'] + 1), target.get('model'), target.get('host_name'), ended]
        self.title.set_text(' · '.join(part for part in parts if part))
        self.spinner.set_visible(status == 'running')
        self.spinner.set_spinning(status == 'running')
        self.state.set_text(state_summary(run))
        self.state.set_visible(bool(run['state'].strip()))
        child = self.cards.get_first_child()
        while child:
            self.cards.remove(child)
            child = self.cards.get_first_child()
        result, error = None, run.get('error') or ''
        if status == 'done':
            try:
                questions = parse_questions(run['questions'])
                result = parse_result(run['result'], questions)
                for question, answer in zip(questions, result.answers):
                    self.cards.append(answer_card(question, answer, run, self.read))
            except ValueError as exc:
                result, error = None, str(exc)
        self.usage.set_text(usage_text(result) if result else '')
        self.usage.set_visible(bool(self.usage.get_text()))
        self.error.set_text(error)
        self.error.set_visible(bool(error))
        self.copy_button.set_visible(run.get('result') is not None)

    def read(self, image, done):
        load(image, done, self.submit)

    def answers(self):
        """The answer each card shows, in order."""
        child, answers = self.cards.get_first_child(), []
        while child:
            answers.append(child.answer)
            child = child.get_next_sibling()
        return answers
