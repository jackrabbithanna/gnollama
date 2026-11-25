"""The questions of a decision as a form, and as the JSON it is exchanged in."""
from gettext import gettext as _
import copy
import json
from gi.repository import Gtk
from ..decision import LIMITS, TYPES, InvalidQuestions, Question, parse_questions, questions_object
from .json_view import TextEditor

# The types any model answers, each with an editor for its criteria. A box question has none, and is offered
# where the selected model answers one; a question that already is one keeps its type whatever the model.
OFFERED = ('noul', 'choice', 'score')
# What an unused form holds; a draft does not keep it.
DEFAULT = {'question_1': {'type': 'noul'}}


def type_label(kind):
    return {'noul': _('Yes/No'), 'choice': _('Choice'), 'score': _('Score'), 'box': _('Box')}[kind]


def compact(value):
    """A value from JSON as one line of text; a string as it is."""
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def icon_button(icon, label, clicked):
    button = Gtk.Button(icon_name=icon, tooltip_text=label, valign=Gtk.Align.CENTER, css_classes=['flat'])
    button.update_property([Gtk.AccessibleProperty.LABEL], [label])
    button.connect('clicked', clicked)
    return button


def unused_name(base, taken):
    """`base` itself, or with the first number after it that makes a name not yet taken."""
    stem = base.rstrip('0123456789')
    number = int(base[len(stem):] or 1) if stem.endswith('_') else 1
    stem = stem if stem.endswith('_') else base + '_'
    name = base
    while name in taken:
        number += 1
        name = f'{stem}{number}'
    return name


class ValueEntry(Gtk.Entry):
    """One instruction or criterion. A value that is not text is shown as compact JSON and kept until edited."""
    def __init__(self, label, changed, value=None, **kwargs):
        super().__init__(placeholder_text=label, hexpand=True, **kwargs)
        self.update_property([Gtk.AccessibleProperty.LABEL], [label])
        self.raw = copy.deepcopy(value)
        self.shown = '' if value is None else compact(value)
        self.set_text(self.shown)
        self.connect('changed', lambda *args: changed())

    def value(self, blank=None):
        """The value as loaded while its text is untouched; otherwise the text, or `blank` for none."""
        text = self.get_text()
        if text == self.shown:
            return blank if self.raw is None else copy.deepcopy(self.raw)
        return text or blank


class CriteriaRows(Gtk.Box):
    """The options of a choice question, each a name and a description, or the levels of a score question."""
    def __init__(self, kind, changed):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.kind, self.changed = kind, changed
        self.rows = []
        self.list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.append(self.list)
        self.add_button = Gtk.Button(label=_('Add Option') if kind == 'choice' else _('Add Level'), halign=Gtk.Align.START)
        self.add_button.connect('clicked', lambda *args: self.add(focus=True))
        self.append(self.add_button)
        if kind == 'score':
            self.prepend(Gtk.Label(label=_('Levels, from the lowest score to the highest'), xalign=0, wrap=True,
                                   css_classes=['dim-label', 'caption']))

    def add(self, name='', value=None, focus=False, notify=True):
        if len(self.rows) >= LIMITS[self.kind]:
            return
        row = Gtk.Box(spacing=6)
        row.number = Gtk.Label(width_chars=2, xalign=1, css_classes=['dim-label'], visible=self.kind == 'score')
        row.append(row.number)
        row.key = Gtk.Entry(placeholder_text=_('Option'), text=name, width_chars=12, visible=self.kind == 'choice')
        row.key.update_property([Gtk.AccessibleProperty.LABEL], [_('Option')])
        row.key.connect('changed', lambda *args: self.changed())
        row.append(row.key)
        label = _('Description (optional)') if self.kind == 'choice' else _('Level description')
        row.value = ValueEntry(label, self.changed, value)
        row.append(row.value)
        if self.kind == 'score':
            row.up = icon_button('go-up-symbolic', _('Move Up'), lambda *args: self.move(row, -1))
            row.down = icon_button('go-down-symbolic', _('Move Down'), lambda *args: self.move(row, 1))
            row.append(row.up)
            row.append(row.down)
        row.append(icon_button('list-remove-symbolic', _('Remove'), lambda *args: self.remove_row(row)))
        self.rows.append(row)
        self.list.append(row)
        self._renumber()
        if focus:
            (row.key if self.kind == 'choice' else row.value).grab_focus()
        if notify:
            self.changed()

    def remove_row(self, row):
        self.rows.remove(row)
        self.list.remove(row)
        self._renumber()
        self.changed()

    def move(self, row, step):
        index = self.rows.index(row)
        other = index + step
        if not 0 <= other < len(self.rows):
            return
        self.rows.insert(other, self.rows.pop(index))
        self.list.reorder_child_after(row, self.rows[other - 1] if other else None)
        self._renumber()
        self.changed()

    def _renumber(self):
        for index, row in enumerate(self.rows):
            row.number.set_text(str(index))
            if self.kind == 'score':
                row.up.set_sensitive(index > 0)
                row.down.set_sensitive(index < len(self.rows) - 1)
        self.add_button.set_sensitive(len(self.rows) < LIMITS[self.kind])

    def load(self, criteria):
        for row in list(self.rows):
            self.rows.remove(row)
            self.list.remove(row)
        if self.kind == 'choice' and isinstance(criteria, dict):
            for name, value in criteria.items():
                self.add(str(name), value, notify=False)
        elif self.kind == 'score' and isinstance(criteria, list):
            for value in criteria:
                self.add(value=value, notify=False)
        self._renumber()

    def value(self):
        if self.kind == 'choice':
            return {row.key.get_text(): row.value.value() for row in self.rows}
        return [row.value.value(blank='') for row in self.rows]

    def flawed(self):
        """Whether a name or a level is blank, or a name is used twice; no object of criteria can say so."""
        if self.kind == 'choice':
            names = [row.key.get_text() for row in self.rows]
            return any(not name.strip() for name in names) or len(set(names)) != len(names)
        return any(value == '' for value in self.value())


class QuestionRow(Gtk.Frame):
    def __init__(self, form, name, item):
        super().__init__()
        self.form = form
        changed = form.edited
        kind = item.get('type') if item.get('type') in TYPES else 'noul'
        criteria = item.get('criteria')
        # An empty object of criteria is written back as one.
        self._empty_criteria = criteria == {} and kind == 'noul'
        self._offering = False
        self.kinds = self._kinds(form.offered(), kind)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_start=12, margin_end=12,
                      margin_top=12, margin_bottom=12)
        self.set_child(box)
        head = Gtk.Box(spacing=6)
        self.name_entry = Gtk.Entry(text=name, placeholder_text=_('Question name'), hexpand=True, width_chars=12)
        self.name_entry.update_property([Gtk.AccessibleProperty.LABEL], [_('Question name')])
        self.name_entry.connect('changed', lambda *args: changed())
        head.append(self.name_entry)
        self.type = Gtk.DropDown(model=Gtk.StringList.new([type_label(k) for k in self.kinds]),
                                 valign=Gtk.Align.CENTER, tooltip_text=_('Question type'))
        self.type.set_selected(self.kinds.index(kind))
        self.type.update_property([Gtk.AccessibleProperty.LABEL], [_('Question type')])
        head.append(self.type)
        self.up = icon_button('go-up-symbolic', _('Move Up'), lambda *args: form.move(self, -1))
        self.down = icon_button('go-down-symbolic', _('Move Down'), lambda *args: form.move(self, 1))
        self.remove_button = icon_button('user-trash-symbolic', _('Remove Question'), lambda *args: form.remove_row(self))
        # On a line of their own, so a question fits a narrow window.
        actions = Gtk.Box(spacing=6, halign=Gtk.Align.END)
        for button in (self.up, self.down, icon_button('edit-copy-symbolic', _('Duplicate Question'),
                                                       lambda *args: form.duplicate(self)), self.remove_button):
            actions.append(button)
        box.append(head)
        self.instructions = ValueEntry(_('Instructions'), changed, item.get('instructions'))
        box.append(self.instructions)
        told = criteria if kind == 'noul' and isinstance(criteria, dict) else {}
        self.yes = ValueEntry(_('Counts as yes (optional)'), changed, told.get('true'))
        self.no = ValueEntry(_('Counts as no (optional)'), changed, told.get('false'))
        self.noul = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.noul.append(self.yes)
        self.noul.append(self.no)
        self.choice = CriteriaRows('choice', changed)
        self.score = CriteriaRows('score', changed)
        self.choice.load(criteria if kind == 'choice' else None)
        self.score.load(criteria if kind == 'score' else None)
        for editor in (self.noul, self.choice, self.score):
            box.append(editor)
        box.append(actions)
        self.type.connect('notify::selected', self._type_changed)
        self._show_editor()

    @property
    def kind(self):
        return self.kinds[self.type.get_selected()]

    @staticmethod
    def _kinds(offered, kind):
        return list(offered) + ([kind] if kind not in offered else [])

    def offer(self, offered):
        """List these types, and the question's own whatever they are."""
        kind = self.kind
        kinds = self._kinds(offered, kind)
        if kinds != self.kinds:
            self._offering = True
            self.kinds = kinds
            self.type.set_model(Gtk.StringList.new([type_label(k) for k in kinds]))
            self.type.set_selected(kinds.index(kind))
            self._offering = False

    def _show_editor(self):
        for kind in OFFERED:
            getattr(self, kind).set_visible(kind == self.kind)

    def _type_changed(self, *args):
        if self._offering:
            return
        self._show_editor()
        editor = getattr(self, self.kind, None)
        if isinstance(editor, CriteriaRows) and not editor.rows:
            # Both need at least one; the first is there to fill in.
            editor.add(notify=False)
        self.form.edited()

    def item(self):
        """The question as it is written in JSON, valid or not."""
        kind = self.kind
        item = {'type': kind}
        instructions = self.instructions.value()
        if instructions is not None:
            item['instructions'] = instructions
        if kind == 'noul':
            criteria = {key: value for key, value in (('true', self.yes.value()), ('false', self.no.value()))
                        if value is not None}
            if criteria or self._empty_criteria:
                item['criteria'] = criteria
        elif kind in ('choice', 'score'):
            item['criteria'] = getattr(self, kind).value()
        return item

    def flawed(self):
        return self.kind in ('choice', 'score') and getattr(self, self.kind).flawed()


class QuestionsEditor(TextEditor):
    def __init__(self, text, on_apply):
        super().__init__(text, on_apply, title=_('Questions'), validate=parse_questions,
                         import_title=_('Import Questions'),
                         hint=_('An object of named questions. Each has a type of noul, choice, score, or box, '
                                'and may have instructions and criteria.'))
        export = Gtk.Button(label=_('Export…'), halign=Gtk.Align.START)
        export.connect('clicked', lambda *args: self.export_text(_('Export Questions'), 'questions.json'))
        self.content_box.prepend(export)


class QuestionForm(Gtk.Box):
    """Any number of questions. `changed` is called after every edit, once `error` says whether they can be asked."""
    def __init__(self, changed=lambda: None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.changed = changed
        self.rows = []
        self.error = ''
        self.dialog = None
        self._loading = False
        self.boxes = False
        head = Gtk.Box(spacing=6)
        head.append(Gtk.Label(label=_('Questions'), xalign=0, hexpand=True, css_classes=['heading']))
        self.json_button = Gtk.Button(label=_('Edit as JSON…'))
        self.json_button.connect('clicked', self.edit_json)
        head.append(self.json_button)
        self.append(head)
        self.list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.append(self.list)
        self.error_label = Gtk.Label(wrap=True, xalign=0, selectable=True, visible=False, css_classes=['error'])
        self.append(self.error_label)
        self.add_button = Gtk.Button(label=_('Add Question'), halign=Gtk.Align.START)
        self.add_button.connect('clicked', lambda *args: self.add())
        self.append(self.add_button)
        self.load({})

    def _names(self):
        return {row.name_entry.get_text() for row in self.rows}

    def offered(self):
        """The types a question may be given."""
        return OFFERED + (('box',) if self.boxes else ())

    def offer_boxes(self, offered):
        """List the box type for every question, or only for those that already are one."""
        if offered != self.boxes:
            self.boxes = offered
            for row in self.rows:
                row.offer(self.offered())

    def asks_box(self):
        return any(row.kind == 'box' for row in self.rows)

    def _insert(self, name, item, after=None):
        row = QuestionRow(self, name, item)
        index = self.rows.index(after) + 1 if after else len(self.rows)
        self.rows.insert(index, row)
        self.list.insert_child_after(row, self.rows[index - 1] if index else None)
        return row

    def add(self):
        row = self._insert(unused_name('question_1', self._names()), {'type': 'noul'})
        self.edited()
        row.name_entry.grab_focus()

    def duplicate(self, row):
        self._insert(unused_name(row.name_entry.get_text() or 'question_1', self._names()), row.item(), after=row)
        self.edited()

    def remove_row(self, row):
        if len(self.rows) > 1:
            self.rows.remove(row)
            self.list.remove(row)
            self.edited()

    def move(self, row, step):
        index = self.rows.index(row)
        other = index + step
        if not 0 <= other < len(self.rows):
            return
        self.rows.insert(other, self.rows.pop(index))
        self.list.reorder_child_after(row, self.rows[other - 1] if other else None)
        self.edited()

    def load(self, questions):
        """Replace the form with an object of questions, taking what it can of one that is not valid; none resets it."""
        self._loading = True
        for row in self.rows:
            self.list.remove(row)
        self.rows = []
        valid = {name: item for name, item in questions.items() if isinstance(item, dict)} if isinstance(questions, dict) else {}
        for name, item in (valid or DEFAULT).items():
            self._insert(name, item)
        self._loading = False
        self._check()

    def value(self):
        """The form as an object of questions, valid or not; of two with one name, the later."""
        return {row.name_entry.get_text(): row.item() for row in self.rows}

    def draft(self):
        """What a draft keeps: `value`, or nothing for a form left as it started."""
        value = self.value()
        return {} if value == DEFAULT else value

    def questions(self):
        """The questions as records; raises InvalidQuestions saying what has to change first."""
        for row in self.rows:
            if row.flawed():
                raise InvalidQuestions(_('Question "{0}" has a blank or repeated criterion.').format(row.name_entry.get_text()))
        return parse_questions([Question(row.name_entry.get_text(), row.kind, row.item().get('instructions'),
                                         row.item().get('criteria')) for row in self.rows])

    def _check(self):
        try:
            self.questions()
            self.error = ''
        except InvalidQuestions as exc:
            self.error = str(exc)
        self.error_label.set_text(self.error)
        self.error_label.set_visible(bool(self.error))
        for index, row in enumerate(self.rows):
            row.up.set_sensitive(index > 0)
            row.down.set_sensitive(index < len(self.rows) - 1)
            row.remove_button.set_sensitive(len(self.rows) > 1)

    def edited(self):
        if not self._loading:
            self._check()
            self.changed()

    def edit_json(self, *args):
        if self.dialog is not None:
            return
        def apply(text):
            self.load(questions_object(parse_questions(text)))
            self.changed()
        self.dialog = QuestionsEditor(json.dumps(self.value(), ensure_ascii=False, indent=2), apply)
        self.dialog.connect('closed', lambda *args: setattr(self, 'dialog', None))
        self.dialog.present(self)

    def close_dialog(self):
        if self.dialog is not None:
            self.dialog.close()
