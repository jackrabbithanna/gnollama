"""Editors and transcript cards for the manual tool-calling playground."""
import json
from gi.repository import Gtk
from .json_view import TextEditor, code_view
from ..tool_calling import EXAMPLE_TOOLS, parse_tools, valid_call


class ToolsEditor(TextEditor):
    def __init__(self, text, on_apply):
        super().__init__(text, on_apply, title=_('Tool Definitions'), validate=parse_tools,
                         import_title=_('Import Tools'),
                         hint=_('Define an Ollama tools array. Results are supplied manually. Example prompt: Read calculator.py, fix add so it adds instead of subtracting, then run python3 -m unittest -v.'))
        example = Gtk.Button(label=_('Example'))
        example.connect('clicked', lambda *args: self.editor.get_buffer().set_text(EXAMPLE_TOOLS))
        export = Gtk.Button(label=_('Export…'))
        export.connect('clicked', lambda *args: self.export_text(_('Export Tools'), 'tools.json'))
        actions = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, column_spacing=6, row_spacing=6,
                              min_children_per_line=1, max_children_per_line=2, halign=Gtk.Align.START)
        actions.append(example)
        actions.append(export)
        self.content_box.prepend(actions)


def text_block(text):
    view = code_view()
    view.get_buffer().set_text(text)
    return Gtk.ScrolledWindow(child=view, min_content_height=60, max_content_height=180,
                             propagate_natural_height=True, hscrollbar_policy=Gtk.PolicyType.NEVER)


class ToolCallsView(Gtk.Box):
    def __init__(self, message, save_result, continue_round, cancel_round):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12, hexpand=True)
        self.message = message
        self.save_result = save_result
        self.dialog = None
        self.buttons = []
        self.results = []
        self.status = Gtk.Label(xalign=0, wrap=True)
        self.append(self.status)
        round = message['response_metadata']['tool_round']
        for index, call in enumerate(message['tool_calls']):
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            name = call['function']['name'] if valid_call(call) else _('Malformed tool call')
            box.append(Gtk.Label(label=_('Tool {0}: {1}').format(index + 1, name), xalign=0, wrap=True, selectable=True))
            raw = json.dumps(call, ensure_ascii=False, indent=2)
            arguments = json.dumps(call['function']['arguments'], ensure_ascii=False, indent=2) if valid_call(call) else raw
            box.append(text_block(arguments))
            check = round['validation'][index]
            box.append(Gtk.Label(label=check['message'], xalign=0, wrap=True, selectable=True))
            actions = Gtk.Box(spacing=6)
            copy = Gtk.Button(label=_('Copy Call'))
            copy.connect('clicked', lambda button, raw=raw: button.get_clipboard().set(raw))
            actions.append(copy)
            result = Gtk.Button(label=_('Enter Result…'))
            result.connect('clicked', self.edit_result, index)
            actions.append(result)
            self.buttons.append(result)
            box.append(actions)
            display = Gtk.Label(xalign=0, wrap=True, selectable=True)
            box.append(display)
            self.results.append(display)
            frame = Gtk.Frame(child=box)
            box.set_margin_top(12)
            box.set_margin_bottom(12)
            box.set_margin_start(12)
            box.set_margin_end(12)
            self.append(frame)
        self.actions = Gtk.Box(spacing=6)
        self.continue_button = Gtk.Button(label=_('Continue'))
        self.continue_button.add_css_class('suggested-action')
        self.continue_button.connect('clicked', lambda *args: continue_round())
        self.cancel_button = Gtk.Button(label=_('Cancel Tool Round'))
        self.cancel_button.connect('clicked', lambda *args: cancel_round())
        self.actions.append(self.continue_button)
        self.actions.append(self.cancel_button)
        self.append(self.actions)
        self.update(False)

    def update(self, editable):
        round = self.message['response_metadata']['tool_round']
        state = round['state']
        labels = {'pending': _('Awaiting manual tool results'), 'submitted': _('Tool results submitted'),
                  'cancelled': _('Tool round cancelled'), 'incomplete': _('Incomplete tool calls'),
                  'invalid': _('Malformed tool calls cannot be continued')}
        self.status.set_text(labels[state])
        pending = state == 'pending'
        self.actions.set_visible(pending)
        self.actions.set_sensitive(editable)
        self.continue_button.set_sensitive(editable and all(r is not None for r in round['results']))
        for index, button in enumerate(self.buttons):
            result = round['results'][index]
            button.set_visible(pending)
            button.set_sensitive(editable)
            button.set_label(_('Edit Result…') if result is not None else _('Enter Result…'))
            self.results[index].set_text(_('Saved mock result: {0}').format(result if result else _('(empty)')) if result is not None else '')

    def edit_result(self, button, index):
        if not button.get_sensitive() or self.dialog is not None:
            return
        round = self.message['response_metadata']['tool_round']
        def apply(text):
            self.save_result(self.message, index, text)
        self.dialog = TextEditor(round['results'][index] or '', apply, title=_('Mock Tool Result'),
                                 hint=_('Enter the result to send to the model. Text, JSON, and empty results are accepted. No tool is executed.'),
                                 apply_label=_('Save Result'))
        self.dialog.connect('closed', lambda *args: setattr(self, 'dialog', None))
        self.dialog.present(self)

    def close_editor(self):
        if self.dialog is not None:
            self.dialog.close()
