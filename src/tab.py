import gi
gi.require_version('WebKit', '6.0')
from gi.repository import Adw, Gtk, Gio, GLib, Gdk, WebKit
import threading
import json
import urllib.request
import re
import html
import os

@Gtk.Template(resource_path='/com/github/jackrabbithanna/Gnollama/tab.ui')
class GenerationTab(Gtk.Box):
    __gtype_name__ = 'GenerationTab'

    entry = Gtk.Template.Child()
    webview_container = Gtk.Template.Child()
    # chat_box = Gtk.Template.Child() # Removed
    send_button = Gtk.Template.Child()
    model_dropdown = Gtk.Template.Child()
    thinking_dropdown = Gtk.Template.Child()
    system_prompt_entry = Gtk.Template.Child()
    stats_check = Gtk.Template.Child()
    logprobs_check = Gtk.Template.Child()
    top_logprobs_entry = Gtk.Template.Child()
    host_entry = Gtk.Template.Child()
    
    # Advanced Options
    seed_entry = Gtk.Template.Child()
    temperature_entry = Gtk.Template.Child()
    top_k_entry = Gtk.Template.Child()
    top_p_entry = Gtk.Template.Child()
    min_p_entry = Gtk.Template.Child()
    num_ctx_entry = Gtk.Template.Child()
    num_predict_entry = Gtk.Template.Child()
    stop_entry = Gtk.Template.Child()

    BASE_HTML = """
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <style>
            :root {
                --bg-color: #ffffff;
                --fg-color: #000000;
                --user-bg: #3584e4;
                --user-fg: #ffffff;
                --bot-bg: #f6f5f4;
                --bot-fg: #000000;
                --dim-color: #888888;
            }
            @media (prefers-color-scheme: dark) {
                :root {
                    --bg-color: #242424;
                    --fg-color: #ffffff;
                    --user-bg: #3584e4;
                    --user-fg: #ffffff;
                    --bot-bg: #383838;
                    --bot-fg: #ffffff;
                    --dim-color: #aaaaaa;
                }
            }
            body {
                background-color: var(--bg-color);
                color: var(--fg-color);
                font-family: sans-serif;
                margin: 0;
                padding: 12px;
            }
            .message-container {
                display: flex;
                flex-direction: column;
                margin-bottom: 12px;
            }
            .user-container {
                align-items: flex-end;
            }
            .bot-container {
                align-items: flex-start;
            }
            .bubble {
                padding: 10px;
                border-radius: 15px;
                max-width: 80%;
                word-wrap: break-word;
            }
            .user-bubble {
                background-color: var(--user-bg);
                color: var(--user-fg);
            }
            .bot-bubble {
                background-color: var(--bot-bg);
                color: var(--bot-fg);
            }
            .header {
                font-size: 0.8em;
                color: var(--dim-color);
                margin-bottom: 4px;
                margin-left: 4px;
            }
            .thinking {
                font-style: italic;
                color: var(--dim-color);
                margin-top: 4px;
                border-left: 2px solid var(--dim-color);
                padding-left: 8px;
                display: none; /* Hidden by default, toggleable? */
            }
            .thinking-header {
                cursor: pointer;
                font-size: 0.8em;
                color: var(--dim-color);
                margin-top: 4px;
                user-select: none;
            }
            .logprobs {
                font-family: monospace;
                font-size: 0.8em;
                background-color: rgba(0,0,0,0.05);
                padding: 8px;
                border-radius: 6px;
                margin-top: 4px;
                white-space: pre-wrap;
                display: none;
            }
            .stats {
                font-size: 0.8em;
                color: var(--dim-color);
                margin-top: 4px;
            }
            pre {
                background-color: rgba(127, 127, 127, 0.1);
                padding: 8px;
                border-radius: 4px;
                overflow-x: auto;
            }
            code {
                font-family: monospace;
            }
        </style>
        <script src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js"></script>
        <script>
            function scrollToBottom() {
                window.scrollTo(0, document.body.scrollHeight);
            }

            function appendUserMessage(text) {
                const container = document.createElement('div');
                container.className = 'message-container user-container';
                
                const bubble = document.createElement('div');
                bubble.className = 'bubble user-bubble';
                bubble.innerHTML = text; // Assumes text is already HTML escaped/formatted if needed, or plain text
                
                container.appendChild(bubble);
                document.body.appendChild(container);
                scrollToBottom();
            }

            let currentBotBubble = null;
            let currentThinkingContainer = null;
            let currentLogprobsContainer = null;

            function startBotMessage(modelName) {
                const container = document.createElement('div');
                container.className = 'message-container bot-container';
                
                const header = document.createElement('div');
                header.className = 'header';
                header.textContent = 'Ollama (' + modelName + '):';
                container.appendChild(header);
                
                const bubble = document.createElement('div');
                bubble.className = 'bubble bot-bubble';
                container.appendChild(bubble);
                currentBotBubble = bubble;
                
                document.body.appendChild(container);
                scrollToBottom();
                
                currentThinkingContainer = null;
                currentLogprobsContainer = null;
            }

            function updateBotMessage(htmlContent) {
                if (currentBotBubble) {
                    currentBotBubble.innerHTML = htmlContent;
                    if (window.MathJax) {
                        MathJax.typesetPromise([currentBotBubble]);
                    }
                    scrollToBottom();
                }
            }

            function updateThinking(text) {
                if (!currentBotBubble) return;
                
                if (!currentThinkingContainer) {
                    const header = document.createElement('div');
                    header.className = 'thinking-header';
                    header.textContent = '▶ See thinking';
                    header.onclick = function() {
                        const content = this.nextElementSibling;
                        if (content.style.display === 'block') {
                            content.style.display = 'none';
                            this.textContent = '▶ See thinking';
                        } else {
                            content.style.display = 'block';
                            this.textContent = '▼ Hide thinking';
                        }
                    };
                    
                    const content = document.createElement('div');
                    content.className = 'thinking';
                    
                    // Insert after bubble? Or inside? Usually thinking comes before or during.
                    // Let's put it before the bubble for now, or inside the container but before bubble?
                    // Actually, let's append to the container (which is flex column).
                    const container = currentBotBubble.parentElement;
                    // Insert before the bubble?
                    container.insertBefore(header, currentBotBubble);
                    container.insertBefore(content, currentBotBubble);
                    
                    currentThinkingContainer = content;
                }
                
                // Append text (escape it?)
                // Simple text append for now
                currentThinkingContainer.textContent += text;
            }

            function addLogprobs(text) {
                if (!currentBotBubble) return;
                
                if (!currentLogprobsContainer) {
                    const header = document.createElement('div');
                    header.className = 'thinking-header'; // Reuse style
                    header.textContent = '▶ Logprobs';
                    header.onclick = function() {
                        const content = this.nextElementSibling;
                        if (content.style.display === 'block') {
                            content.style.display = 'none';
                            this.textContent = '▶ Logprobs';
                        } else {
                            content.style.display = 'block';
                            this.textContent = '▼ Logprobs';
                        }
                    };
                    
                    const content = document.createElement('div');
                    content.className = 'logprobs';
                    
                    const container = currentBotBubble.parentElement;
                    container.appendChild(header);
                    container.appendChild(content);
                    
                    currentLogprobsContainer = content;
                }
                
                currentLogprobsContainer.textContent += text;
            }

            function addStats(text) {
                if (!currentBotBubble) return;
                const container = currentBotBubble.parentElement;
                const statsDiv = document.createElement('div');
                statsDiv.className = 'stats';
                statsDiv.textContent = text;
                container.appendChild(statsDiv);
                scrollToBottom();
            }
        </script>
    </head>
    <body>
        <div id="chat"></div>
    </body>
    </html>
    """

    def __init__(self, tab_label=None, **kwargs):
        super().__init__(**kwargs)
        self.tab_label = tab_label
        
        self.send_button.connect('clicked', self.on_send_clicked)
        self.entry.connect('activate', self.on_send_clicked)
        self.system_prompt_entry.connect('activate', self.on_send_clicked)
        self.top_logprobs_entry.connect('activate', self.on_send_clicked)
        
        # Connect advanced options to send
        self.seed_entry.connect('activate', self.on_send_clicked)
        self.temperature_entry.connect('activate', self.on_send_clicked)
        self.top_k_entry.connect('activate', self.on_send_clicked)
        self.top_p_entry.connect('activate', self.on_send_clicked)
        self.min_p_entry.connect('activate', self.on_send_clicked)
        self.num_ctx_entry.connect('activate', self.on_send_clicked)
        self.num_predict_entry.connect('activate', self.on_send_clicked)
        self.stop_entry.connect('activate', self.on_send_clicked)
        
        # Initialize host entry
        default_host = "http://localhost:11434"
        self.host_entry.set_text(default_host)
        
        # Connect host entry changed signal
        self.host_entry.connect('changed', self.on_host_changed)
        self.host_update_source_id = None
        
        # Refresh models on dropdown click
        click_controller = Gtk.GestureClick()
        click_controller.connect("pressed", self.on_dropdown_clicked)
        self.model_dropdown.add_controller(click_controller)
        
        self.model_dropdown.connect('notify::selected-item', self.on_model_changed)
        
        self.active_thinking_label = None
        self.active_logprobs_label = None
        self.current_response_label = None
        self.current_response_raw_text = ""
        
        # Setup WebView
        self.webview = WebKit.WebView()
        settings = self.webview.get_settings()
        settings.set_enable_developer_extras(True)
        self.webview_container.append(self.webview)
        self.webview.load_html(self.BASE_HTML, "file:///")
        # Initial model fetch
        self.start_model_fetch_thread()

    def on_host_changed(self, widget):
        # Debounce
        if self.host_update_source_id:
            GLib.source_remove(self.host_update_source_id)
        self.host_update_source_id = GLib.timeout_add(500, self.on_host_update_timeout)

    def on_host_update_timeout(self):
        self.host_update_source_id = None
        self.start_model_fetch_thread()
        return False

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
        # Use local entry
        host = self.host_entry.get_text()
        if not host:
            return
            
        url = f"{host}/api/tags"
        try:
            with urllib.request.urlopen(url) as response:
                result = json.loads(response.read().decode('utf-8'))
                models = [model['name'] for model in result.get('models', [])]
                if models:
                    GLib.idle_add(self.update_model_dropdown, models)
        except Exception as e:
            print(f"Failed to fetch models: {e}")
            # Optionally clear dropdown or show error?
            # For now, just print.

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

        # Update tab label if available
        if self.tab_label:
            truncated = prompt[:20] + "..." if len(prompt) > 20 else prompt
            self.tab_label.set_label(truncated)

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
        
        # Headers: ### Header -> <h3>Header</h3>
        text = re.sub(r'^###\s+(.+)$', r'<h3>\1</h3>', text, flags=re.MULTILINE)
        text = re.sub(r'^##\s+(.+)$', r'<h2>\1</h2>', text, flags=re.MULTILINE)
        text = re.sub(r'^#\s+(.+)$', r'<h1>\1</h1>', text, flags=re.MULTILINE)
        
        # Bold: **text** -> <b>text</b>
        text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
        
        # Italic: *text* -> <i>text</i>
        text = re.sub(r'\*(.+?)\*', r'<i>\1</i>', text)
        
        # Inline code: `text` -> <code>text</code>
        text = re.sub(r'`(.+?)`', r'<code>\1</code>', text)
        
        # Code blocks: ```lang ... ``` -> <pre><code>...</code></pre>
        text = re.sub(r'```(\w+)?\n(.*?)```', r'<pre><code>\2</code></pre>', text, flags=re.DOTALL)
        
        # Math blocks: \[...\] -> $$...$$ (for MathJax)
        text = re.sub(r'\\\[(.*?)\\\]', r'$$\1$$', text, flags=re.DOTALL)
        
        # Inline math: \(...\) -> \(...\) (MathJax handles this)
        # No change needed if MathJax is configured for \( \)
        
        # Newlines to <br> (simple approach)
        text = text.replace('\n', '<br>')
        
        return text

    def _js_callback(self, webview, result, user_data):
        try:
            webview.evaluate_javascript_finish(result)
        except Exception as e:
            print(f"JS Execution Error: {e}")

    def run_js(self, script):
        # print(f"Running JS: {script[:100]}...") # Debug print
        GLib.idle_add(self.webview.evaluate_javascript, script, -1, None, None, None, self._js_callback, None)

    def add_message(self, text, sender="System"):
        print(f"Adding message from {sender}: {text[:50]}...")
        markup = self.parse_markdown(text)
        # Escape for JS string
        js_content = json.dumps(markup)
        if sender == "You":
            self.run_js(f"appendUserMessage({js_content})")
        else:
            # System message?
            pass

    def start_new_response_block(self, model_name):
        self.current_response_raw_text = ""
        js_model = json.dumps(model_name)
        self.run_js(f"startBotMessage({js_model})")

    def append_response_chunk(self, text):
        # print(f"Received chunk: {text[:20]}...")
        self.current_response_raw_text += text
        markup = self.parse_markdown(self.current_response_raw_text)
        js_content = json.dumps(markup)
        self.run_js(f"updateBotMessage({js_content})")

    def reset_thinking_state(self):
        self.active_thinking_label = None
        self.active_logprobs_label = None
        self.current_response_label = None
        self.current_response_raw_text = ""

    def append_thinking(self, text):
        js_text = json.dumps(text)
        self.run_js(f"updateThinking({js_text})")

    def append_logprobs(self, logprobs_data):
        # Format logprobs data compactly
        text_chunk = ""
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
             
        js_text = json.dumps(text_chunk)
        self.run_js(f"addLogprobs({js_text})")

    def show_stats(self, stats):
        # Format stats
        total_duration = stats.get('total_duration', 0) / 1e9
        load_duration = stats.get('load_duration', 0) / 1e9
        prompt_eval_count = stats.get('prompt_eval_count', 0)
        prompt_eval_duration = stats.get('prompt_eval_duration', 0) / 1e9
        eval_count = stats.get('eval_count', 0)
        eval_duration = stats.get('eval_duration', 0) / 1e9
        
        stats_text = (
            f"Total: {total_duration:.2f}s | Load: {load_duration:.2f}s | "
            f"Prompt: {prompt_eval_count} tokens ({prompt_eval_duration:.2f}s) | "
            f"Eval: {eval_count} tokens ({eval_duration:.2f}s)"
        )
        
        js_text = json.dumps(stats_text)
        self.run_js(f"addStats({js_text})")

    def send_prompt_to_ollama(self, prompt, model_name):
        # host = self.settings.get_string('ollama-host')
        host = self.host_entry.get_text()
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
            
        # Get system prompt
        system_prompt = self.system_prompt_entry.get_text().strip()
        if system_prompt:
            data["system"] = system_prompt

        # Get logprobs parameters
        if self.logprobs_check.get_active():
            data["logprobs"] = True
            top_logprobs_text = self.top_logprobs_entry.get_text().strip()
            if top_logprobs_text.isdigit():
                data["top_logprobs"] = int(top_logprobs_text)
                
        # Get advanced options
        options = {}
        
        def add_option(entry, key, type_func):
            text = entry.get_text().strip()
            if text:
                try:
                    val = type_func(text)
                    options[key] = val
                except ValueError:
                    pass
        
        add_option(self.seed_entry, 'seed', int)
        add_option(self.temperature_entry, 'temperature', float)
        add_option(self.top_k_entry, 'top_k', int)
        add_option(self.top_p_entry, 'top_p', float)
        add_option(self.min_p_entry, 'min_p', float)
        add_option(self.num_ctx_entry, 'num_ctx', int)
        add_option(self.num_predict_entry, 'num_predict', int)
        
        # Stop sequence
        stop_text = self.stop_entry.get_text().strip()
        if stop_text:
            stops = [s.strip() for s in stop_text.split(',') if s.strip()]
            if stops:
                options['stop'] = stops
                
        if options:
            data['options'] = options

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
                                
                            # Handle logprobs
                            if "logprobs" in json_obj and json_obj["logprobs"]:
                                 GLib.idle_add(self.append_logprobs, json_obj["logprobs"])

                            if json_obj.get('done'):
                                if self.stats_check.get_active():
                                    GLib.idle_add(self.show_stats, json_obj)
                                break
                        except ValueError:
                            pass
        except Exception as e:
            GLib.idle_add(self.add_message, f"\nError: {str(e)}\n", "System")
