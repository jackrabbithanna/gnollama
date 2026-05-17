from typing import List, Optional, Any, Dict, Callable
from gi.repository import Gtk, GObject, Gio, GdkPixbuf, GLib, Gdk
import threading
from .. import ollama

@Gtk.Template(resource_path='/io/github/jackrabbithanna/Gnollama/widgets/chat_input.ui')
class ChatInput(Gtk.Box):
    __gtype_name__ = 'ChatInput'

    model_dropdown: Gtk.DropDown = Gtk.Template.Child()
    thinking_dropdown: Gtk.DropDown = Gtk.Template.Child()
    entry: Gtk.Entry = Gtk.Template.Child()
    send_button: Gtk.Button = Gtk.Template.Child()
    
    image_preview_scrolled: Gtk.ScrolledWindow = Gtk.Template.Child()
    image_preview_box: Gtk.Box = Gtk.Template.Child()
    attach_button: Gtk.Button = Gtk.Template.Child()
    image_label: Gtk.Label = Gtk.Template.Child()
    clear_image_button: Gtk.Button = Gtk.Template.Child()

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        
        # Setup thinking dropdown
        thinking_options = Gtk.StringList.new([_("Thinking"), _("Low"), _("Medium"), _("High"), _("Max"), _("None")])
        self.thinking_dropdown.set_model(thinking_options)
        self.thinking_dropdown.set_selected(0)

        self.selected_image_paths: List[str] = []
        
        self.attach_button.connect("clicked", self.on_attach_clicked)
        self.clear_image_button.connect("clicked", self.on_clear_image_clicked)
        
        # We don't connect send_button here; the parent handles it.
        # But we could also emit a custom signal if we wanted to be more self-contained.

    def set_models(self, models: List[str]) -> None:
        """Populates the model dropdown."""
        string_list = Gtk.StringList.new(models)
        self.model_dropdown.set_model(string_list)
        if models:
            pending = getattr(self, 'pending_model_selection', None)
            if pending and pending in models:
                self.select_model(pending)
                self.pending_model_selection = None
            else:
                self.model_dropdown.set_selected(0)

    def select_model(self, model_name: str) -> None:
        """Selects a specific model in the dropdown if available."""
        model = self.model_dropdown.get_model()
        if not model: return
        for i in range(model.get_n_items()):
            item = model.get_item(i)
            if item and item.get_string() == model_name:
                self.model_dropdown.set_selected(i)
                break

    def get_selected_model(self) -> str:
        """Returns the currently selected model name."""
        selected_item = self.model_dropdown.get_selected_item()
        if selected_item:
            return selected_item.get_string()
        return "llama3"

    def get_thinking_value(self) -> Any:
        """Returns the currently selected thinking value."""
        thinking_item = self.thinking_dropdown.get_selected_item()
        if thinking_item:
            thinking_str = thinking_item.get_string()
            if thinking_str == _("Thinking"): return True
            elif thinking_str == _("Low"): return "low"
            elif thinking_str == _("Medium"): return "medium"
            elif thinking_str == _("High"): return "high"
            elif thinking_str == _("Max"): return "max"    
            elif thinking_str == _("None"): return None
            elif thinking_str == _("No thinking"): return False
        return None

    def load_thinking_val(self, val: Any) -> None:
        """Sets the thinking dropdown based on the stored value."""
        model = self.thinking_dropdown.get_model()
        if not model: return
        
        target = _("None")
        if val is True: target = _("Thinking")
        elif val == "low": target = _("Low")
        elif val == "medium": target = _("Medium")
        elif val == "high": target = _("High")
        elif val == "max": target = _("Max")
        elif val is False: target = _("No thinking")
        
        for i in range(model.get_n_items()):
            item = model.get_item(i)
            if item and item.get_string() == target:
                self.thinking_dropdown.set_selected(i)
                break

    def fetch_models(self, host: str) -> None:
        """Asynchronously fetches available models from the host."""
        def thread_func() -> None:
            models = ollama.fetch_models(host)
            GLib.idle_add(self.set_models, models)
            
        thread = threading.Thread(target=thread_func, daemon=True)
        thread.start()

    def on_attach_clicked(self, btn: Gtk.Button) -> None:
        """Opens a file chooser to attach one or multiple images."""
        parent_window = self.get_root()
        if not isinstance(parent_window, Gtk.Window):
            return

        dialog = Gtk.FileDialog()
        dialog.set_title(_("Select Images"))
        
        filters = Gio.ListStore.new(Gtk.FileFilter)
        image_filter = Gtk.FileFilter()
        image_filter.set_name(_("Images"))
        image_filter.add_mime_type("image/png")
        image_filter.add_mime_type("image/jpeg")
        filters.append(image_filter)
        dialog.set_filters(filters)
        
        def on_files_selected(dialog: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
            try:
                files = dialog.open_multiple_finish(result)
                if files:
                    for i in range(files.get_n_items()):
                        file = files.get_item(i)
                        path = file.get_path()
                        if path and path not in self.selected_image_paths:
                            self.selected_image_paths.append(path)
                    self.update_image_preview()
            except GLib.Error as e:
                if not (e.domain == 'gtk-dialog-error-quark' and e.code == 2):
                    print(f"Error selecting files: {e}")
                    
        dialog.open_multiple(parent_window, None, on_files_selected)

    def update_image_preview(self) -> None:
        """Updates the image preview UI based on selected paths."""
        child = self.image_preview_box.get_first_child()
        while child:
            next_child = child.get_next_sibling()
            self.image_preview_box.remove(child)
            child = next_child

        if not self.selected_image_paths:
            self.image_preview_scrolled.set_visible(False)
            self.image_label.set_text(_("No image selected"))
            self.clear_image_button.set_visible(False)
            return

        self.image_preview_scrolled.set_visible(True)
        count = len(self.selected_image_paths)
        self.image_label.set_text(_("{0} image(s) selected").format(count))
        self.clear_image_button.set_visible(True)

        for path in self.selected_image_paths:
            try:
                with open(path, "rb") as f:
                    data = f.read()
                texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(data))
                img_widget = Gtk.Picture.new_for_paintable(texture)
                img_widget.set_size_request(80, 80)
                img_widget.set_content_fit(Gtk.ContentFit.SCALE_DOWN)
                
                remove_btn = Gtk.Button.new_from_icon_name("window-close-symbolic")
                remove_btn.add_css_class("osd")
                remove_btn.add_css_class("circular")
                remove_btn.set_valign(Gtk.Align.START)
                remove_btn.set_halign(Gtk.Align.END)
                remove_btn.connect("clicked", self.on_remove_single_image_clicked, path)
                
                overlay = Gtk.Overlay()
                overlay.set_child(img_widget)
                overlay.add_overlay(remove_btn)
                
                self.image_preview_box.append(overlay)
            except GLib.Error as e:
                print(f"Error loading image preview for {path}: {e}")

    def on_remove_single_image_clicked(self, btn: Gtk.Button, path: str) -> None:
        """Removes a single image from the selection."""
        if path in self.selected_image_paths:
            self.selected_image_paths.remove(path)
            self.update_image_preview()

    def on_clear_image_clicked(self, btn: Optional[Gtk.Button]) -> None:
        """Clears all selected images."""
        self.selected_image_paths = []
        self.update_image_preview()
