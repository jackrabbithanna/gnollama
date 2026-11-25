"""The settings an image model takes, a row for each, at the model's default until one is chosen."""
from gettext import gettext as _
import copy
from gi.repository import Adw, GObject, Gtk
from ..image_generation import LABELS


class ImageSettingsForm(Adw.PreferencesGroup):
    """Rows built from an `ImageParameters`; `options()` holds what was chosen, a default being absent.

    What was chosen is kept across models: a value the next model takes is shown again.
    """
    __gsignals__ = {'changed': (GObject.SignalFlags.RUN_FIRST, None, ())}

    def __init__(self):
        super().__init__(visible=False)
        # None until a model is selected; its settings are not known before.
        self.parameters = None
        self.rows = {}
        self._wanted = {}
        self._building = False

    def set_parameters(self, parameters, force=False):
        """Show the rows of a model's settings; None leaves none and forgets nothing chosen."""
        if parameters == self.parameters and not force:
            return
        self._building = True
        for row in self.rows.values():
            self.remove(row)
        self.rows = {}
        self.parameters = parameters
        for parameter in parameters.options if parameters else ():
            row = self._row(parameter)
            if row is not None:
                row.parameter = parameter
                self.rows[parameter.name] = row
                self.add(row)
        self.set_visible(bool(self.rows))
        self._building = False
        self._mark()

    def _row(self, parameter):
        name, wanted = parameter.name, self._wanted.get(parameter.name)
        title = _(LABELS[name])
        if parameter.kind == 'choice':
            # PNG stands in for a model's own output format, which may be one not every system can show.
            values = list(parameter.values) if name == 'output_format' and 'png' in parameter.values else \
                [None, *parameter.values]
            row = Adw.ComboRow(title=title, model=Gtk.StringList.new(
                [_('Model default') if value is None else value for value in values]))
            row.values = values
            row.set_selected(values.index(wanted) if wanted in values else values.index('png') if None not in values else 0)
            row.connect('notify::selected', lambda *args: self._picked(name))
            return row
        if parameter.kind == 'range' and name == 'n':
            if parameter.low == parameter.high:
                return None  # Nothing to choose.
            row = Adw.SpinRow.new_with_range(parameter.low, parameter.high, 1)
            row.set_title(title)
            row.set_value(wanted if parameter.accepts(wanted) else parameter.low)
            row.connect('notify::value', lambda *args: self._picked(name))
            return row
        # A seed, unset while empty: the model then picks one.
        row = Adw.EntryRow(title=title, input_purpose=Gtk.InputPurpose.DIGITS,
                           tooltip_text=_('Leave empty for a random seed.'))
        row.set_text('' if wanted is None else str(wanted))
        row.connect('changed', lambda *args: self._picked(name))
        return row

    @staticmethod
    def _value(row):
        """A row's value as sent, or None at the model's default."""
        parameter = row.parameter
        if isinstance(row, Adw.ComboRow):
            index = row.get_selected()
            return row.values[index] if index < len(row.values) else None
        if isinstance(row, Adw.SpinRow):
            value = int(row.get_value())
            return None if value == parameter.low else value
        text = row.get_text().strip()
        if not text:
            return None
        # Text that is no number is kept as written, for the request check to name.
        return int(text) if text.isascii() and text.isdigit() else text

    def _picked(self, name):
        if self._building:
            return
        value = self._value(self.rows[name])
        if value is None:
            self._wanted.pop(name, None)
        else:
            self._wanted[name] = value
        self._mark()
        self.emit('changed')

    def _mark(self):
        for row in self.rows.values():
            value = self._value(row)
            if value is not None and not row.parameter.accepts(value):
                row.add_css_class('error')
            else:
                row.remove_css_class('error')

    def options(self):
        """The settings chosen, by name; before a model's settings are known, all that were."""
        if self.parameters is None:
            return copy.deepcopy(self._wanted)
        return {name: value for name, row in self.rows.items() if (value := self._value(row)) is not None}

    def set_options(self, options):
        self._wanted = copy.deepcopy(dict(options or {}))
        self.set_parameters(self.parameters, force=True)
