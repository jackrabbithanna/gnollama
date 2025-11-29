# window.py
#
# Copyright 2025 Jackrabbithanna
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# SPDX-License-Identifier: GPL-3.0-or-later

from gi.repository import Adw, Gtk, Gio, GLib, Gdk
import threading
import json
import urllib.request
import re
import html

@Gtk.Template(resource_path='/com/github/jackrabbithanna/Gnollama/window.ui')
class GnollamaWindow(Adw.ApplicationWindow):
    __gtype_name__ = 'GnollamaWindow'

    entry = Gtk.Template.Child()
    chat_box = Gtk.Template.Child()
    send_button = Gtk.Template.Child()
    model_dropdown = Gtk.Template.Child()
    thinking_dropdown = Gtk.Template.Child()

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.settings = Gio.Settings.new('com.github.jackrabbithanna.Gnollama')
        
        self.send_button.connect('clicked', self.on_send_clicked)
        self.entry.connect('activate', self.on_send_clicked)
        
        self.settings.connect('changed::ollama-host', self.on_host_changed)
        
        # Refresh models on dropdown click
        click_controller = Gtk.GestureClick()
        click_controller.connect("pressed", self.on_dropdown_clicked)
        self.model_dropdown.add_controller(click_controller)
        
        self.model_dropdown.connect('notify::selected-item', self.on_model_changed)
        
        self.active_thinking_label = None
        self.current_response_label = None
        self.current_response_raw_text = ""
        
        # Load CSS
        self.load_css()
        
        # Initial model fetch
        self.start_model_fetch_thread()

    def load_css(self):
        css_provider = Gtk.CssProvider()
        css = """
        .user-bubble {
            background-color: @accent_bg_color;
            color: @accent_fg_color;
            border-radius: 15px;
            padding: 10px;
            margin-bottom: 5px;
        }
        .bot-bubble {
            background-color: alpha(@window_fg_color, 0.05);
            border-radius: 15px;
            padding: 10px;
            margin-bottom: 5px;
        }
        .thinking-text {
            color: alpha(@window_fg_color, 0.6);
            font-style: italic;
        }
        .dim-label {
            opacity: 0.7;
            font-size: smaller;
            margin-bottom: 2px;
        }
        """
        css_provider.load_from_data(css.encode('utf-8'))
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            css_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

    def on_host_changed(self, settings, key):
        self.start_model_fetch_thread()

    def on_dropdown_clicked(self, gesture, n_press, x, y):
        self.start_model_fetch_thread()
        
    def on_model_changed(self, *args):
        selected_item = self.model_dropdown.get_selected_item()
        if selected_item:
            model_name = selected_item.get_string()
            self.update_thinking_options(model_name)

    def update_thinking_options(self, model_name):
        if model_name.startswith("gpt-oss"):
            options = ["None", "Low", "Medium", "High"]
        else:
            options = ["Thinking", "No thinking"]
            
        string_list = Gtk.StringList.new(options)
        self.thinking_dropdown.set_model(string_list)
        
        # Set default
        if model_name.startswith("gpt-oss"):
             self.thinking_dropdown.set_selected(0) # None
        else:
             self.thinking_dropdown.set_selected(1) # No thinking (default false)

    def start_model_fetch_thread(self):
        thread = threading.Thread(target=self.fetch_models)
        thread.daemon = True
        thread.start()

    def fetch_models(self):
        host = self.settings.get_string('ollama-host')
        url = f"{host}/api/tags"
        try:
            with urllib.request.urlopen(url) as response:
                result = json.loads(response.read().decode('utf-8'))
                models = [model['name'] for model in result.get('models', [])]
                if models:
                    GLib.idle_add(self.update_model_dropdown, models)
        except Exception as e:
            print(f"Failed to fetch models: {e}")

    def update_model_dropdown(self, models):
        # Check if models have changed to avoid unnecessary updates
        current_model = self.model_dropdown.get_model()
        if current_model:
            current_items = [current_model.get_string(i) for i in range(current_model.get_n_items())]
            if current_items == models:
                return

        string_list = Gtk.StringList.new(models)
        self.model_dropdown.set_model(string_list)
        # Select first model by default if available and nothing selected
        if models and self.model_dropdown.get_selected() == Gtk.INVALID_LIST_POSITION:
            self.model_dropdown.set_selected(0)
            # Trigger thinking update for initial selection
            self.update_thinking_options(models[0])

    def on_send_clicked(self, widget):
        prompt = self.entry.get_text()
        if not prompt:
            return

        self.add_message(prompt, sender="You")
        self.entry.set_text("")
        
        # Get selected model
        selected_item = self.model_dropdown.get_selected_item()
        model_name = "llama3" # Fallback
        if selected_item:
            model_name = selected_item.get_string()

        thread = threading.Thread(target=self.send_prompt_to_ollama, args=(prompt, model_name))
        thread.daemon = True
        thread.start()

    def parse_markdown(self, text):
        # Escape HTML characters first
        text = html.escape(text)
        
        # Headers: ### Header -> <span size="large" weight="bold">Header</span>
        text = re.sub(r'^###\s+(.+)$', r'<span size="large" weight="bold">\1</span>', text, flags=re.MULTILINE)
        text = re.sub(r'^##\s+(.+)$', r'<span size="x-large" weight="bold">\1</span>', text, flags=re.MULTILINE)
        text = re.sub(r'^#\s+(.+)$', r'<span size="xx-large" weight="bold">\1</span>', text, flags=re.MULTILINE)
        
        # Bold: **text** -> <b>text</b>
        text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
        
        # Italic: *text* -> <i>text</i>
        text = re.sub(r'\*(.+?)\*', r'<i>\1</i>', text)
        
        # Inline code: `text` -> <tt>text</tt>
        text = re.sub(r'`(.+?)`', r'<tt>\1</tt>', text)
        
        # Math blocks: \[...\] -> <tt>...</tt> (basic fallback)
        text = re.sub(r'\\\[(.*?)\\\]', r'<tt>\1</tt>', text, flags=re.DOTALL)
        
        # Inline math: \(...\) -> <tt>...</tt>
        text = re.sub(r'\\\((.*?)\\\)', r'<tt>\1</tt>', text)
        
        # Newlines to <br> (optional, but GtkLabel handles newlines)
        # However, if we use markup, we might need to be careful. 
        # GtkLabel handles \n correctly even with markup.
        
        return text

    def add_message(self, text, sender="System"):
        # Container box for alignment and styling
        container = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        
        # Bubble box for background
        bubble = Gtk.Box()
        
        label = Gtk.Label()
        label.set_wrap(True)
        label.set_max_width_chars(50) # Limit width for better readability
        label.set_xalign(0)
        label.set_selectable(True)
        
        # Parse markdown for display
        markup = self.parse_markdown(text)
        label.set_markup(markup)
        
        bubble.append(label)
        container.append(bubble)

        if sender == "You":
            container.set_halign(Gtk.Align.END)
            bubble.add_css_class("user-bubble")
        else:
            container.set_halign(Gtk.Align.START)
            bubble.add_css_class("bot-bubble")
            
        self.chat_box.append(container)

    def start_new_response_block(self, model_name):
        self.current_response_raw_text = ""
        
        # Add header
        header = Gtk.Label(label=f"Ollama ({model_name}):")
        header.set_xalign(0)
        header.set_halign(Gtk.Align.START)
        header.add_css_class("dim-label")
        self.chat_box.append(header)
        
        # Container for response bubble
        container = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        container.set_halign(Gtk.Align.START)
        container.set_hexpand(True)
        
        # Bubble box
        bubble = Gtk.Box()
        bubble.add_css_class("bot-bubble")
        bubble.set_hexpand(True) # Allow bubble to expand to fill width if needed, or just wrap
        
        # Create label for response content
        self.current_response_label = Gtk.Label()
        self.current_response_label.set_wrap(True)
        self.current_response_label.set_xalign(0)
        self.current_response_label.set_selectable(True)
        self.current_response_label.set_halign(Gtk.Align.FILL)
        self.current_response_label.set_hexpand(True)
        
        bubble.append(self.current_response_label)
        container.append(bubble)
        self.chat_box.append(container)

    def append_response_chunk(self, text):
        if self.current_response_label:
            self.current_response_raw_text += text
            markup = self.parse_markdown(self.current_response_raw_text)
            self.current_response_label.set_markup(markup)

    def reset_thinking_state(self):
        self.active_thinking_label = None
        self.current_response_label = None
        self.current_response_raw_text = ""

    def append_thinking(self, text):
        if self.active_thinking_label is None:
            # Create expander and inner label
            expander = Gtk.Expander(label="See thinking")
            expander.set_hexpand(True)
            expander.set_halign(Gtk.Align.FILL)
            
            inner_label = Gtk.Label()
            inner_label.set_wrap(True)
            inner_label.set_xalign(0)
            inner_label.set_selectable(True)
            inner_label.set_hexpand(True)
            inner_label.set_halign(Gtk.Align.FILL)
            inner_label.add_css_class("thinking-text")
            
            expander.set_child(inner_label)
            
            # Insert before the current response container if it exists
            # Note: current_response_label is inside a bubble inside a container.
            # We need to find the container of the current response label.
            # But simpler: just append expander to chat_box. 
            # If we want it *inside* the bubble, that's different.
            # User said "response area and thinking area have a modern styling".
            # Expander outside bubble seems fine, or inside?
            # Let's put it outside for now, but styled.
            
            # To insert before response, we need reference to the response container.
            # But we don't store it.
            # Let's just append. It will appear after the header and before the response if we are lucky with timing?
            # No, we call start_new_response_block first. So response container is already appended.
            # We should insert expander *before* the last child of chat_box (which is the response container).
            
            last_child = self.chat_box.get_last_child()
            if last_child and self.current_response_label:
                 # Verify last_child is indeed the response container (it should be)
                 self.chat_box.insert_child_after(expander, last_child.get_prev_sibling())
            else:
                 self.chat_box.append(expander)
            
            self.active_thinking_label = inner_label

        current_text = self.active_thinking_label.get_label()
        self.active_thinking_label.set_label(current_text + text)

    def send_prompt_to_ollama(self, prompt, model_name):
        host = self.settings.get_string('ollama-host')
        url = f"{host}/api/generate"
        
        # Get thinking parameter
        thinking_item = self.thinking_dropdown.get_selected_item()
        thinking_val = None # Default None
        if thinking_item:
            thinking_str = thinking_item.get_string()
            if thinking_str == "Thinking":
                thinking_val = True
            elif thinking_str == "No thinking":
                thinking_val = False
            elif thinking_str == "Low":
                thinking_val = "low"
            elif thinking_str == "Medium":
                thinking_val = "medium"
            elif thinking_str == "High":
                thinking_val = "high"
            elif thinking_str == "None":
                thinking_val = None

        data = {
            "model": model_name,
            "prompt": prompt,
            "stream": True
        }
        
        if thinking_val is not None:
            data["thinking"] = thinking_val
        
        GLib.idle_add(self.reset_thinking_state)
        GLib.idle_add(self.start_new_response_block, model_name)
        
        try:
            req = urllib.request.Request(url, data=json.dumps(data).encode('utf-8'), headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(req) as response:
                for line in response:
                    if line:
                        try:
                            json_obj = json.loads(line.decode('utf-8'))
                            
                            # Handle thinking
                            thinking_fragment = json_obj.get('thinking', '')
                            if thinking_fragment and thinking_val is not False and thinking_val is not None:
                                GLib.idle_add(self.append_thinking, thinking_fragment)
                                
                            # Handle response
                            response_fragment = json_obj.get('response', '')
                            if response_fragment:
                                GLib.idle_add(self.append_response_chunk, response_fragment)
                                
                            if json_obj.get('done'):
                                break
                        except ValueError:
                            pass
        except Exception as e:
            GLib.idle_add(self.add_message, f"\nError: {str(e)}\n", "System")
