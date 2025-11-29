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

from gi.repository import Adw, Gio, GLib, Gtk
import threading
import json
import urllib.request

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
        
        # Initial model fetch
        self.start_model_fetch_thread()

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

        self.add_message(f"You: {prompt}", sender="You")
        self.entry.set_text("")
        
        # Get selected model
        selected_item = self.model_dropdown.get_selected_item()
        model_name = "llama3" # Fallback
        if selected_item:
            model_name = selected_item.get_string()

        thread = threading.Thread(target=self.send_prompt_to_ollama, args=(prompt, model_name))
        thread.daemon = True
        thread.start()

    def add_message(self, text, sender="System"):
        label = Gtk.Label(label=text)
        label.set_wrap(True)
        label.set_xalign(0)
        label.set_selectable(True)
        if sender == "You":
            label.set_halign(Gtk.Align.END)
            # Optional: Add specific styling or background for user messages
        else:
            label.set_halign(Gtk.Align.START)
            
        self.chat_box.append(label)

    def start_new_response_block(self, model_name):
        # Add header
        header = Gtk.Label(label=f"Ollama ({model_name}):")
        header.set_xalign(0)
        header.set_halign(Gtk.Align.START)
        header.add_css_class("dim-label") # Example class
        self.chat_box.append(header)
        
        # Create label for response content
        self.current_response_label = Gtk.Label()
        self.current_response_label.set_wrap(True)
        self.current_response_label.set_xalign(0)
        self.current_response_label.set_selectable(True)
        self.current_response_label.set_halign(Gtk.Align.FILL)
        self.current_response_label.set_hexpand(True)
        self.chat_box.append(self.current_response_label)

    def append_response_chunk(self, text):
        if self.current_response_label:
            current_text = self.current_response_label.get_label()
            self.current_response_label.set_label(current_text + text)

    def reset_thinking_state(self):
        self.active_thinking_label = None
        self.current_response_label = None

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
            
            expander.set_child(inner_label)
            
            # Insert before the current response label if it exists, otherwise append
            # But simpler to just append to box. However, we want thinking BEFORE response.
            # Since we create response label at start, we might need to insert.
            # Actually, let's just append. If we call start_new_response_block first, 
            # then thinking will be after.
            # Strategy: Don't create response label until we have response or are done thinking?
            # Or just append expander. If response label exists, we should probably insert before it?
            # GtkBox append adds to end. 
            # Let's adjust: send_prompt_to_ollama calls start_new_response_block.
            # If thinking comes, we want it between header and response body?
            # For simplicity, let's just append expander to chat_box. 
            # If we want it strictly before response body, we'd need to manage order.
            # But usually thinking comes first.
            # Let's assume thinking comes before response text.
            # If we already created response label, we might need to reorder.
            # But for streaming, we might get thinking first.
            # Let's just append to chat_box. If response label was created, it's already there.
            # Wait, if we create response label at start, thinking will be after it if we just append.
            # We should create response label ONLY when we get first response chunk?
            # Or insert thinking before response label.
            
            # Let's try inserting before current_response_label if it exists
            if self.current_response_label:
                self.chat_box.insert_child_after(expander, self.current_response_label.get_prev_sibling())
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
