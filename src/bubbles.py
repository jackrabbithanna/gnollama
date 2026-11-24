from gettext import gettext as _
import base64
from typing import List, Optional, Any, Dict
from gi.repository import Gtk, GLib, Gdk
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
    # The text view that shows log probabilities, once any have arrived.
    active_logprobs_label: Optional[Gtk.TextView] = None

    def __init__(self, model_name: Optional[str] = None, output_format=None,
                 host_name: Optional[str] = None, **kwargs: Any) -> None:
        from .widgets.json_view import JsonResponseView
        super().__init__(**kwargs)
        self.init_template()

        # "host name (model)"; the model alone when the host is not known.
        self.title = f'{host_name} ({model_name})' if host_name and model_name else model_name or ''
        if self.title:
            self.header.set_visible(True)
            self.header.set_label(self.title)
        
        self.api_markdown_view = MarkdownView(wrap_code=True)
        self.api_expander.set_child(self.api_markdown_view)
        
        self.json_view = JsonResponseView(has_schema=isinstance(output_format, dict)) if output_format is not None else None
        self.markdown_view = self.json_view or MarkdownView()
        self.bubble_box.append(self.markdown_view)
        
        self.full_text: str = ""
        self.thinking_text: str = ""
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
        """Appends a chunk of text to the main markdown response.
        Callers deliver streamed chunks already coalesced, so each one is shown at once."""
        if not text:
            return
        self.full_text += text
        self._show_text(streaming=True)

    def _show_text(self, streaming: bool = False) -> None:
        if isinstance(self.markdown_view, MarkdownView):
            self.markdown_view.update(self.full_text, streaming=streaming)
        else:
            self.markdown_view.update(self.full_text)

    def append_thinking(self, text: str) -> None:
        """Appends text to the thinking section."""
        if not text:
            return
        if not self.thinking_expander.get_visible():
            self.thinking_expander.set_visible(True)
        
        self.thinking_text += text
        self.thinking_label.set_label(self.thinking_text)

    def show_stats(self, stats, elapsed=None, first_token=None):
        if not hasattr(self, '_stats_label'):
            self._stats_label = Gtk.Label(xalign=0, wrap=True, selectable=True)
            self._stats_label.add_css_class('dim-label')
            expander = Gtk.Expander(label=_('Response Statistics'), child=self._stats_label)
            self.bubble_box.append(expander)
        self._stats_label.set_text(format_statistics(stats, elapsed, first_token))
        self._stats_label.set_tooltip_text(None if server_timed(stats) or not _measured(elapsed) else _(
            'Timed by Gnollama, not reported by the server. The total includes network delay and model loading.'))

    def show_response_metadata(self, metadata, show_stats=True):
        # One complete render, so a finished response looks the same as one loaded from history.
        self._show_text()
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
        metrics, elapsed = metadata.get('metrics') or {}, metadata.get('elapsed_seconds')
        line = format_statistics(metrics, elapsed, metadata.get('first_token_seconds')) if metrics else ''
        if show_stats and line:
            self.show_stats(metrics, elapsed, metadata.get('first_token_seconds'))
        # The statistics line already holds the local total when the server sent no durations.
        if show_stats and elapsed is not None and (not line or server_timed(metrics)):
            self.bubble_box.append(Gtk.Label(label=_('Elapsed: {0}s').format(round(metadata['elapsed_seconds'], 2)),
                                            xalign=0, wrap=True, css_classes=['dim-label']))

    def append_logprobs(self, logprobs_data: Any) -> None:
        """Appends logprobs data to a text view in an expander."""
        if self.active_logprobs_label is None:
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


DURATION_KEYS = ('total_duration', 'load_duration', 'prompt_eval_duration', 'eval_duration')


def _measured(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0


def server_timed(stats):
    """Whether the server reported any duration of its own."""
    return any(_measured(stats.get(key)) for key in DURATION_KEYS)


def format_statistics(stats, elapsed=None, first_token=None):
    """Format the metrics a server reported, leaving out what it did not.

    `elapsed` and `first_token` are seconds on this application's clock, from
    sending the request. They are shown only when the server sent no durations.
    """
    fields = []

    def add(key, text, shown=str):
        if _measured(stats.get(key)):
            fields.append(text.format(shown(stats[key])))

    def duration(value):
        return f'{value / 1e9:.2f}s'

    tokens = stats.get('eval_count')
    local = not server_timed(stats) and _measured(elapsed)
    if local:
        fields.append(_('Total (measured locally): {0}').format(f'{elapsed:.2f}s'))
    add('total_duration', _('Total: {0}'), duration)
    add('load_duration', _('Load: {0}'), duration)
    add('prompt_eval_count', _('Prompt: {0} tokens'))
    add('prompt_eval_cached_count', _('Cached: {0} tokens'))
    add('prompt_eval_duration', _('Prompt evaluation: {0}'), duration)
    add('eval_count', _('Generated: {0} tokens'))
    add('eval_duration', _('Generation: {0}'), duration)
    if _measured(tokens) and _measured(stats.get('eval_duration')) and stats['eval_duration'] > 0:
        fields.append(_('Tokens/s: {0}').format(f"{tokens * 1e9 / stats['eval_duration']:.2f}"))
    elif local and _measured(tokens):
        # Generation starts at the first token; without that time the whole request is all there is.
        span = elapsed - first_token if _measured(first_token) and first_token < elapsed else elapsed
        if span > 0:
            fields.append(_('Tokens/s (measured locally): {0}').format(f'{tokens / span:.2f}'))
    if stats.get('done_reason'):
        fields.append(_('Finish: {0}').format(stats['done_reason']))
    return ' | '.join(fields)
