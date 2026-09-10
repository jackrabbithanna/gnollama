"""JSON editor and response view, with portal-based import and export."""
from gi.repository import Adw, Gtk, Gio, GLib
from ..markdown_view import GtkSource
from ..structured import formatted_json, request_format


def code_view(editable=False):
    if GtkSource:
        buffer = GtkSource.Buffer()
        buffer.set_language(GtkSource.LanguageManager.get_default().get_language('json'))
        view = GtkSource.View.new_with_buffer(buffer)
        view.set_show_line_numbers(editable)
        view.set_auto_indent(True)
        view.set_tab_width(2)
        manager = Adw.StyleManager.get_default()
        def style(*args):
            name = 'oblivion' if manager.get_dark() else 'classic'
            buffer.set_style_scheme(GtkSource.StyleSchemeManager.get_default().get_scheme(name))
        style()
        handler = manager.connect('notify::dark', style)
        view.connect('destroy', lambda *args: manager.disconnect(handler))
    else:
        view = Gtk.TextView(monospace=True)
    view.set_editable(editable)
    view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
    view.set_hexpand(True)
    view.set_left_margin(12)
    view.set_right_margin(12)
    view.set_top_margin(8)
    view.set_bottom_margin(8)
    return view


def editor_frame(view, **kwargs):
    scrolled = Gtk.ScrolledWindow(child=view, hscrollbar_policy=Gtk.PolicyType.NEVER, **kwargs)
    scrolled.add_css_class('editor-frame')
    return scrolled


def buffer_text(view):
    buffer = view.get_buffer()
    return buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)


def json_filter():
    filter = Gtk.FileFilter(name=_('JSON files'))
    filter.add_pattern('*.json')
    filter.add_mime_type('application/json')
    filters = Gio.ListStore.new(Gtk.FileFilter)
    filters.append(filter)
    return filters


class TextEditor(Adw.Dialog):
    def __init__(self, text, on_apply, *, title, hint, validate=None, import_title=None, apply_label=None):
        super().__init__(title=title, content_width=640, content_height=520)
        self.validate = validate
        self.import_title = import_title
        self._cancel = Gio.Cancellable()
        self.connect('closed', lambda *args: self._cancel.cancel())
        self.on_apply = on_apply
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.header = header
        if import_title:
            import_button = Gtk.Button(label=_('Import…'))
            import_button.connect('clicked', self.import_text)
            header.pack_start(import_button)
        apply = Gtk.Button(label=apply_label or _('Apply'))
        apply.add_css_class('suggested-action')
        apply.connect('clicked', self.apply_text)
        self.apply_button = apply
        header.pack_end(apply)
        toolbar.add_top_bar(header)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                      margin_start=12, margin_end=12, margin_top=12, margin_bottom=12)
        self.content_box = box
        hint = Gtk.Label(label=hint,
                         wrap=True, xalign=0)
        box.append(hint)
        self.editor = code_view(editable=True)
        self.editor.get_buffer().set_text(text)
        editor_label = Gtk.Label(label=_('Content'), xalign=0, mnemonic_widget=self.editor)
        box.append(editor_label)
        box.append(editor_frame(self.editor, vexpand=True))
        self.error_label = Gtk.Label(wrap=True, xalign=0, selectable=True, visible=False)
        self.error_label.add_css_class('error')
        box.append(self.error_label)
        toolbar.set_content(box)
        self.set_child(toolbar)

    def show_error(self, error):
        self.error_label.set_text(str(error))
        self.error_label.set_visible(True)

    def apply_text(self, *args):
        text = buffer_text(self.editor)
        try:
            if self.validate:
                self.validate(text)
        except (ValueError, RecursionError) as exc:
            self.show_error(exc)
            return
        if self.on_apply(text) is False:
            return
        self.close()

    def import_text(self, *args):
        dialog = Gtk.FileDialog(title=self.import_title, filters=json_filter())
        def selected(dialog, result):
            try:
                file = dialog.open_finish(result)
                file.load_contents_async(self._cancel, loaded)
            except GLib.Error as exc:
                if not self._cancel.is_cancelled() and not exc.matches(Gtk.dialog_error_quark(), Gtk.DialogError.DISMISSED):
                    self.show_error(exc.message)
        def loaded(file, result):
            try:
                _, contents, _etag = file.load_contents_finish(result)
                if not self._cancel.is_cancelled():
                    self.editor.get_buffer().set_text(contents.decode('utf-8-sig'))
                    self.error_label.set_visible(False)
            except (GLib.Error, UnicodeError) as exc:
                if not self._cancel.is_cancelled():
                    self.show_error(exc)
        dialog.open(self.get_root(), self._cancel, selected)

    def export_text(self, title, filename):
        text = buffer_text(self.editor)
        try:
            if self.validate:
                self.validate(text)
        except (ValueError, RecursionError) as exc:
            self.show_error(exc)
            return
        dialog = Gtk.FileDialog(title=title, initial_name=filename, filters=json_filter())
        def selected(dialog, result):
            try:
                file = dialog.save_finish(result)
                file.replace_contents_bytes_async(GLib.Bytes.new(text.encode('utf-8')), None, False,
                                                  Gio.FileCreateFlags.NONE, self._cancel, saved)
            except GLib.Error as exc:
                if not self._cancel.is_cancelled() and not exc.matches(Gtk.dialog_error_quark(), Gtk.DialogError.DISMISSED):
                    self.show_error(exc)
        def saved(file, result):
            try:
                file.replace_contents_finish(result)
            except GLib.Error as exc:
                if not self._cancel.is_cancelled():
                    self.show_error(exc)
        dialog.save(self.get_root(), self._cancel, selected)


class SchemaEditor(TextEditor):
    def __init__(self, text, on_apply):
        super().__init__(text, on_apply, title=_('JSON Schema'),
                         hint=_('Paste or import a self-contained JSON Schema. Describe the expected JSON in your prompt.'),
                         validate=lambda text: request_format('schema', text), import_title=_('Import JSON Schema'))

    apply_schema = TextEditor.apply_text
    import_schema = TextEditor.import_text


class JsonResponseView(Gtk.Box):
    def __init__(self, has_schema=False):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6, hexpand=True)
        self.has_schema = has_schema
        self.raw = ''
        self.pretty = None
        self._cancel = Gio.Cancellable()
        self.connect('destroy', lambda *args: self._cancel.cancel())
        self.status_label = Gtk.Label(xalign=0, wrap=True, selectable=True)
        self.append(self.status_label)
        self.view = code_view()
        self.append(self.view)
        actions = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, column_spacing=6,
                              row_spacing=6, min_children_per_line=1, max_children_per_line=4)
        self.raw_toggle = Gtk.ToggleButton(label=_('Raw'), sensitive=False)
        self.raw_toggle.connect('toggled', lambda *args: self.render())
        self.copy_json = Gtk.Button(label=_('Copy JSON'), sensitive=False)
        self.copy_json.connect('clicked', lambda *args: self.get_clipboard().set(self.pretty or ''))
        self.save_json = Gtk.Button(label=_('Save JSON…'), sensitive=False)
        self.save_json.connect('clicked', self.export)
        copy_raw = Gtk.Button(label=_('Copy Raw'))
        copy_raw.connect('clicked', lambda *args: self.get_clipboard().set(self.raw))
        for button in (self.raw_toggle, copy_raw, self.copy_json, self.save_json):
            actions.append(button)
        self.append(actions)

    def update(self, text):
        self.raw = text
        self.render()

    def render(self):
        self.view.get_buffer().set_text(self.pretty if self.pretty is not None and not self.raw_toggle.get_active() else self.raw)

    def finish(self, validation):
        validation = validation or {'status': 'incomplete'}
        status = validation.get('status')
        labels = {'valid': _('Matches JSON Schema') if self.has_schema else _('Valid JSON'),
                  'incomplete': _('Incomplete JSON response'),
                  'invalid_json': _('Invalid JSON'), 'schema_mismatch': _('JSON does not match the schema'),
                  'validation_error': _('Schema validation could not finish')}
        label = labels.get(status, _('JSON response'))
        if validation.get('message'):
            label += ': ' + validation.get('path', '') + ' ' + validation['message']
        self.status_label.set_text(label)
        self.pretty = None
        if status != 'incomplete':
            try:
                self.pretty = formatted_json(self.raw)
            except (ValueError, RecursionError):
                pass
        for button in (self.raw_toggle, self.copy_json, self.save_json):
            button.set_sensitive(self.pretty is not None)
        self.render()

    def export(self, *args):
        if self.pretty is None:
            return
        contents = self.pretty.encode('utf-8')
        dialog = Gtk.FileDialog(title=_('Save JSON'), initial_name='response.json', filters=json_filter())
        def error(exc):
            if not self._cancel.is_cancelled():
                alert = Adw.AlertDialog(heading=_('JSON could not be saved'), body=str(exc))
                alert.add_response('close', _('Close'))
                alert.present(self.get_root())
        def selected(dialog, result):
            try:
                file = dialog.save_finish(result)
                file.replace_contents_bytes_async(GLib.Bytes.new(contents), None, False,
                                                  Gio.FileCreateFlags.NONE, self._cancel, saved)
            except GLib.Error as exc:
                if not exc.matches(Gtk.dialog_error_quark(), Gtk.DialogError.DISMISSED):
                    error(exc)
        def saved(file, result):
            try:
                file.replace_contents_finish(result)
            except GLib.Error as exc:
                error(exc)
        dialog.save(self.get_root(), self._cancel, selected)
