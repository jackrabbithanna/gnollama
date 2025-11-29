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
    text_view = Gtk.Template.Child()
    send_button = Gtk.Template.Child()
    model_dropdown = Gtk.Template.Child()
    thinking_dropdown = Gtk.Template.Child()

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.settings = Gio.Settings.new('com.github.jackrabbithanna.Gnollama')
        self.buffer = self.text_view.get_buffer()
        
        self.send_button.connect('clicked', self.on_send_clicked)
        self.entry.connect('activate', self.on_send_clicked)
        
        self.settings.connect('changed::ollama-host', self.on_host_changed)
        
        # Refresh models on dropdown click
        click_controller = Gtk.GestureClick()
        click_controller.connect("pressed", self.on_dropdown_clicked)
        self.model_dropdown.add_controller(click_controller)
        
        self.model_dropdown.connect('notify::selected-item', self.on_model_changed)
        
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

        self.append_text(f"You: {prompt}\n")
        self.entry.set_text("")
        
        # Get selected model
        selected_item = self.model_dropdown.get_selected_item()
        model_name = "llama3" # Fallback
        if selected_item:
            model_name = selected_item.get_string()

        thread = threading.Thread(target=self.send_prompt_to_ollama, args=(prompt, model_name))
        thread.daemon = True
        thread.start()

    def append_text(self, text):
        end_iter = self.buffer.get_end_iter()
        self.buffer.insert(end_iter, text)

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
        
        GLib.idle_add(self.append_text, f"Ollama ({model_name}): ")
        
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
                                GLib.idle_add(self.append_text, thinking_fragment)
                                
                            # Handle response
                            response_fragment = json_obj.get('response', '')
                            if response_fragment:
                                GLib.idle_add(self.append_text, response_fragment)
                                
                            if json_obj.get('done'):
                                break
                        except ValueError:
                            pass
                GLib.idle_add(self.append_text, "\n\n")
        except Exception as e:
            GLib.idle_add(self.append_text, f"\nError: {str(e)}\n\n")
