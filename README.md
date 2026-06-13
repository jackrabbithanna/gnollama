<div align="center">

# Gnollama

<img src="./data/icons/hicolor/scalable/apps/io.github.jackrabbithanna.Gnollama.svg" width="128" height="128"></img>

A Gnome user interface to [Ollama](https://ollama.com)
</div>

## Description

**Gnollama** is a modern, feature-rich GNOME user interface for [Ollama](https://ollama.com) built using Python, GTK4, and Libadwaita. It provides a native, responsive Linux desktop experience for interacting with LLMs.

Whether you are developing, experimenting, or chatting with local models, Gnollama makes it easy to run prompts across multiple local or remote hosts simultaneously.

## Features

* **Multi-Host Management**: Easily connect to different Ollama servers. Add, edit, or delete configurations, verify host status, and define a default host.
* **Dual Tab Workflows**:
  * **New Chat (`/api/chat`)**: Multi-turn sessions that preserve conversation context.
  * **New Response (`/api/generate`)**: Single-turn completions ideal for prompt engineering and testing.
* **Conversation History & Sidebar**:
  * Automatically saves chat logs and model configurations between runs.
  * **Pin Chats**: Pin essential conversations to the top of your history list.
  * **Popover Options**: Use a three-vertical-dots menu on any saved chat to quickly Pin, Rename, or Delete.
* **Model Manager**:
  * Pull or delete models directly from the UI.
  * View comprehensive model info, including size, parameter specifications, Modelfiles, templates, and licenses.
* **Rich Markdown & Code Rendering**: Full Markdown support and code syntax highlighting (powered by GTKSourceView 5).
* **Multimodal Image Support**: Upload and attach multiple images to your prompts for vision-enabled models.
* **Thinking & completion Details**:
  * Inline rendering of the model's `<think>` reasoning stream.
  * Display generation stats (token counts, load times, speeds) and logprobs.

<img src="./screenshots/gnollama-screenshot.png" alt="gnollama" align="left"/>

<img src="./screenshots/gnollama-chat-options.png" alt="gnollama" align="left"/>

<img src="./screenshots/gnollama-manage-models.png" alt="gnollama" align="left"/>

## Motivation

I wanted a GNOME application for Ollama that I could use to test and experiment with different models. I have multiple computers with Ollama and wanted a way to easily query and compare the responses from all of them using the same interface. Perhaps others would find this useful as well so here you go.

## Build

*gnollama* can be built and run with [GNOME Builder](https://wiki.gnome.org/Apps/Builder).

1. Open GNOME Builder
2. Click the **Clone Repository** button
3. Enter `https://github.com/jackrabbithanna/gnollama.git` in the field **Repository URL**
4. Click the **Clone Project** button
5. Click the **Run** button to start building application

### Meson

Requires python3 and markdown
Code highlighting requires [GTKSourceView](https://wiki.gnome.org/Projects/GtkSourceView) version 5

To install in Ubuntu:
```bash
apt-get install libgtksourceview-5-0 libgtksourceview-5-common libgtksourceview-5-dev
apt-get install gir1.2-gtksource-5
apt-get install python3-markdown python3-gi
```

```bash
meson setup build
meson compile -C build
meson install -C build
```
You can then run `gnollama` to execute the application.

## TODO

*   More UI Multi-lingual translations
*   Consider how to add tool calls response fieldset and allow the user to set optional list of function tools the model may call during the chat
*   Embeddings?
 
## Contribute

The [GNOME Code of Conduct](https://conduct.gnome.org/) is applicable to this project


## License

*gnollama* is released under the terms of the [GNU General Public License V3](https://www.gnu.org/licenses/gpl-3.0.html).

No warranty provided. No guarantee it does anything at all. Use at your own risk.
