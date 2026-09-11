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

    def __init__(self, model_name: Optional[str] = None, output_format=None, **kwargs: Any) -> None:
        from .widgets.json_view import JsonResponseView
        super().__init__(**kwargs)
        self.init_template()
        
        if model_name:
            self.header.set_visible(True)
            self.header.set_label(f"Ollama ({model_name})")
        
        self.api_markdown_view = MarkdownView(wrap_code=True)
        self.api_expander.set_child(self.api_markdown_view)
        
        self.json_view = JsonResponseView(has_schema=isinstance(output_format, dict)) if output_format is not None else None
        self.markdown_view = self.json_view or MarkdownView()
        self.bubble_box.append(self.markdown_view)
        
        self.full_text: str = ""
        self.thinking_text: str = ""
        self._update_scheduled: bool = False
        self._update_source = None
        self.copy_button = Gtk.Button(label=_('Copy Answer'), halign=Gtk.Align.END)
        from .widgets.feedback import copy_text
        self.copy_button.connect('clicked', lambda button: copy_text(button, self.full_text))
        self.bubble_box.append(self.copy_button)

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
            self._update_source = GLib.timeout_add(50, self._flush_update)
            
    def _flush_update(self) -> bool:
        """Flushes the accumulated text to the MarkdownView."""
        self._update_source = None
        self.markdown_view.update(self.full_text)
        self._update_scheduled = False
        return False

    def cancel_delivery(self):
        if self._update_source is not None:
            GLib.source_remove(self._update_source)
            self._update_source = None
        self._update_scheduled = False
        
    def append_thinking(self, text: str) -> None:
        """Appends text to the thinking section."""
        if not text:
            return
        if not self.thinking_expander.get_visible():
            self.thinking_expander.set_visible(True)
        
        self.thinking_text += text
        self.thinking_label.set_label(self.thinking_text)

    def show_stats(self, stats):
        if not hasattr(self, '_stats_label'):
            self._stats_label = Gtk.Label(xalign=0, wrap=True, selectable=True)
            self._stats_label.add_css_class('dim-label')
            expander = Gtk.Expander(label=_('Response Statistics'), child=self._stats_label)
            self.bubble_box.append(expander)
        self._stats_label.set_text(format_statistics(stats))

    def show_response_metadata(self, metadata, show_stats=True):
        self.cancel_delivery()
        self._flush_update()
        if metadata.get('tool_round') and self.json_view is not None:
            self.bubble_box.remove(self.json_view)
            self.json_view = None
            self.markdown_view = MarkdownView()
            self.markdown_view.update(self.full_text)
            self.bubble_box.append(self.markdown_view)
        if self.json_view is not None:
            self.json_view.update(self.full_text)
            self.json_view.finish(metadata.get('validation'))
        if metadata.get('history_images_omitted'):
            self.bubble_box.append(Gtk.Label(label=_('Earlier images were omitted from the text-only request.'),
                                            xalign=0, wrap=True))
        status = metadata.get('status', 'complete')
        if status in ('stopped', 'failed'):
            label = Gtk.Label(xalign=0, wrap=True, selectable=True)
            title = _('Stopped') if status == 'stopped' else _('Failed')
            error = metadata.get('error')
            label.set_text(title + (': ' + error if error else ''))
            label.add_css_class('dim-label')
            self.bubble_box.append(label)
        if show_stats and metadata.get('metrics'):
            self.show_stats(metadata['metrics'])
        if show_stats and metadata.get('elapsed_seconds') is not None:
            self.bubble_box.append(Gtk.Label(label=_('Elapsed: {0}s').format(round(metadata['elapsed_seconds'], 2)),
                                            xalign=0, wrap=True, css_classes=['dim-label']))

    def append_logprobs(self, logprobs_data: Any) -> None:
        """Appends logprobs data to a text view in an expander."""
        if not hasattr(self, 'active_logprobs_label') or self.active_logprobs_label is None:
            # Create expander for logprobs
            expander = Gtk.Expander(label=_("Logprobs"))
            expander.set_hexpand(True)
            expander.set_halign(Gtk.Align.FILL)
            
            # Use ScrolledWindow + TextView for performance with large data
            scrolled = Gtk.ScrolledWindow()
            scrolled.set_hscrollbar_policy(Gtk.PolicyType.NEVER)
            scrolled.set_min_content_height(150)
            scrolled.set_propagate_natural_height(True)
            
            text_view = Gtk.TextView()
            text_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
            text_view.set_editable(False)
            text_view.set_monospace(True)
            text_view.set_bottom_margin(6)
            text_view.set_top_margin(6)
            text_view.set_left_margin(6)
            text_view.set_right_margin(6)
            
            scrolled.set_child(text_view)
            expander.set_child(scrolled)
            self.bubble_box.append(expander)
            
            self.active_logprobs_label = text_view # Reusing variable name for TextView

        buffer = self.active_logprobs_label.get_buffer()
        end_iter = buffer.get_end_iter()
        
        # Format logprobs data compactly
        text_chunk = ""
        import json
        if isinstance(logprobs_data, list):
            for item in logprobs_data:
                if isinstance(item, dict):
                    token = item.get('token', '')
                    logprob = item.get('logprob', 0.0)
                    text_chunk += f"Token: {repr(token):<15} Logprob: {logprob:.4f}\n"
                else:
                    text_chunk += str(item) + "\n"
        else:
             text_chunk = json.dumps(logprobs_data) + "\n"
             
        buffer.insert(end_iter, text_chunk)


def format_statistics(stats):
    """Format optional API metrics without treating missing values as zero."""
    unavailable = _('Unavailable')
    def count(key):
        value = stats.get(key)
        return str(value) if isinstance(value, (int, float)) and value >= 0 else unavailable

    def duration(key):
        value = stats.get(key)
        return f'{value / 1e9:.2f}s' if isinstance(value, (int, float)) and value >= 0 else unavailable

    tokens, elapsed = stats.get('eval_count'), stats.get('eval_duration')
    speed = (f'{tokens * 1e9 / elapsed:.2f}'
             if isinstance(tokens, (int, float)) and tokens >= 0
             and isinstance(elapsed, (int, float)) and elapsed > 0 else unavailable)
    fields = [
        _('Total: {0}').format(duration('total_duration')),
        _('Load: {0}').format(duration('load_duration')),
        _('Prompt: {0} tokens').format(count('prompt_eval_count')),
        _('Cached: {0} tokens').format(count('prompt_eval_cached_count')),
        _('Prompt evaluation: {0}').format(duration('prompt_eval_duration')),
        _('Generated: {0} tokens').format(count('eval_count')),
        _('Generation: {0}').format(duration('eval_duration')),
        _('Tokens/s: {0}').format(speed),
    ]
    if stats.get('done_reason'):
        fields.append(_('Finish: {0}').format(stats['done_reason']))
    return ' | '.join(fields)
