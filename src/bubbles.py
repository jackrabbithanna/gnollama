import base64
from typing import List, Optional, Any, Dict
from gi.repository import Gtk, GObject, Pango, GLib, Gdk
from .markdown_view import MarkdownView

@Gtk.Template(resource_path='/io/github/jackrabbithanna/Gnollama/user_bubble.ui')
class UserBubble(Gtk.ListBoxRow):
    """A chat bubble for user messages, supporting text and images."""
    __gtype_name__ = 'UserBubble'

    images_box: Gtk.Box = Gtk.Template.Child()
    label: Gtk.Label = Gtk.Template.Child()

    def __init__(self, text: str, images: Optional[List[str]] = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.init_template()
        self.label.set_text(text)
        
        if images:
            self.images_box.set_visible(True)
            for img_b64 in images:
                try:
                    start_idx = 0
                    if "," in img_b64:
                        start_idx = img_b64.find(",") + 1
                    
                    img_data = base64.b64decode(img_b64[start_idx:])
                    bytes_data = GLib.Bytes.new(img_data)
                    texture = Gdk.Texture.new_from_bytes(bytes_data)
                    
                    picture = Gtk.Picture.new_for_paintable(texture)
                    picture.set_content_fit(Gtk.ContentFit.SCALE_DOWN)
                    picture.set_size_request(200, 200)
                    picture.set_can_shrink(True)
                    
                    self.images_box.append(picture)
                except Exception as e:
                    print(f"Failed to load image in bubble: {e}")

@Gtk.Template(resource_path='/io/github/jackrabbithanna/Gnollama/ai_bubble.ui')
class AiBubble(Gtk.ListBoxRow):
    """A chat bubble for AI responses, supporting markdown and 'thinking' sections."""
    __gtype_name__ = 'AiBubble'

    bubble_box: Gtk.Box = Gtk.Template.Child()
    header: Gtk.Label = Gtk.Template.Child()
    api_expander: Gtk.Expander = Gtk.Template.Child()
    thinking_expander: Gtk.Expander = Gtk.Template.Child()
    thinking_label: Gtk.Label = Gtk.Template.Child()

    def __init__(self, model_name: Optional[str] = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.init_template()
        
        if model_name:
            self.header.set_visible(True)
            self.header.set_label(f"Ollama ({model_name})")
        
        self.api_markdown_view = MarkdownView()
        self.api_expander.set_child(self.api_markdown_view)
        
        self.markdown_view = MarkdownView()
        self.bubble_box.append(self.markdown_view)
        
        self.full_text: str = ""
        self.thinking_text: str = ""
        self._update_scheduled: bool = False

    def set_api_details(self, details_dict: Dict[str, Any]) -> None:
        """Displays the raw API request details in an expander."""
        self.api_expander.set_visible(True)
        import json
        details_str = json.dumps(details_dict, indent=2)
        md_text = f"```json\n{details_str}\n```"
        self.api_markdown_view.update(md_text)

    def append_text(self, text: str) -> None:
        """Appends a chunk of text to the main markdown response."""
        self.full_text += text
        
        if not self._update_scheduled:
            self._update_scheduled = True
            GLib.timeout_add(50, self._flush_update)
            
    def _flush_update(self) -> bool:
        """Flushes the accumulated text to the MarkdownView."""
        self.markdown_view.update(self.full_text)
        self._update_scheduled = False
        return False
        
    def append_thinking(self, text: str) -> None:
        """Appends text to the thinking section."""
        if not self.thinking_expander.get_visible():
            self.thinking_expander.set_visible(True)
        
        self.thinking_text += text
        self.thinking_label.set_label(self.thinking_text)
