# Codebase Analysis and Improvement Plan

This document outlines an analysis of the Gnollama codebase, focusing on Python coding standards, GNOME/GTK best practices, and opportunities for architectural improvements. It provides a roadmap for making the codebase more robust, maintainable, and aligned with modern development patterns while retaining all existing functionality.

## User Review Required

Please review the proposed architectural changes. Some changes (like introducing an asynchronous HTTP client or restructuring the core tab logic) are significant and would require careful implementation to avoid breaking existing features.

> [!IMPORTANT]
> This plan now includes a Phase 5 for migrating storage to SQLite, reversing the previous decision to stick with JSON. This will provide better performance and scalability as history grows.

## Open Questions

1. **Async vs Threads:** The app currently uses `threading.Thread` to prevent UI blocking during API calls. Are you open to using `asyncio` with an async HTTP library (like `aiohttp` or `httpx`), or would you prefer to stick to standard library `urllib` and threads?
2. **Minimum Libadwaita Version:** Are you targeting a specific minimum version of Libadwaita? Newer versions (1.5+) have `Adw.AlertDialog` which replaces `Adw.MessageDialog`.
3. **Theming:** Should code blocks in `markdown_view.py` follow the system's light/dark preference automatically?

---

## 1. Python Coding Standards & Best Practices

**Current State:**
- The codebase generally follows PEP 8 naming conventions.
- It lacks type hints and consistent docstrings.
- Error handling often relies on broad `except Exception as e:` blocks.
- Synchronous file I/O operations are performed on the main thread in some areas (especially in `storage.py`).

**Opportunities for Improvement:**

1. **Type Hinting:** Add Python type hints (`-> None`, `: str`, `: dict`, etc.) to function signatures and class properties across all files.
2. **Docstrings:** Add standard docstrings (Google/NumPy style) to classes and complex methods, especially in `tab.py`, `storage.py`, and `ollama.py`.
3. **Granular Exception Handling:** Instead of catching `Exception`, catch specific exceptions (e.g., `urllib.error.URLError`, `json.JSONDecodeError`) to avoid masking unexpected bugs.
4. **Thread Management:** Avoid spinning up raw `threading.Thread` instances repeatedly. Use `concurrent.futures.ThreadPoolExecutor` for background tasks to reuse threads and manage their lifecycle better.

---

## 2. GNOME/GTK Standards & Best Practices

**Current State:**
- Excellent use of GTK4, Libadwaita, and `Gtk.Template`.
- Good use of modern CSS variables for theming (`var(--accent-bg-color)`).
- Hardcoded CSS strings inside Python files (`window.py`, `model_manager.py`).
- Inline margins and UI properties defined in Python rather than XML templates.

**Opportunities for Improvement:**

1. **Externalize CSS:** Move all inline CSS blocks into a dedicated `style.css` file. Load this file via `Gtk.CssProvider` and include it in the application's GResources (`gnollama.gresource.xml`).
2. **Move Properties to UI Files:** Many widgets have properties set in Python (e.g., `label.set_margin_top(6)`). These should be defined in the `.ui` template files to keep the Python code focused on logic.
3. **Modern Dialogs:** If targeting Libadwaita >= 1.5, migrate `Adw.MessageDialog` (deprecated in 1.5) to `Adw.AlertDialog`.
4. **GObject Properties:** Leverage `GObject.Property` for state variables (like `pulling` state in `PullModelDialog`) to allow for future UI bindings.

---

## 3. Code Architecture & Structure Improvements

**Current State:**
- **Monolithic Components:** `tab.py` is over 800 lines long. `GenerationTab` manages UI interactions, builds API payloads, parses streaming responses, updates the UI, and coordinates saving to storage.
- **Tight Coupling:** The UI directly instantiates and interacts with `ChatStorage` and `ollama` network calls.
- **Manual Markdown Handling:** `markdown_view.py` implements a complex custom syncing logic that is hard to maintain.

**Opportunities for Improvement:**

### A. Modularize `tab.py`
Break `GenerationTab` and its associated UI into smaller, reusable components:
- `src/widgets/chat_input.py`: Handles text entry and image attachments.
- `src/widgets/options_panel.py`: Contains generation parameters (temperature, top_k, etc.).
- `src/widgets/message_list.py`: Manages the scrolling list of `ChatBubble` instances.
- `src/session.py`: Extract `ChatStrategy` and `GenerationStrategy` and the core "session" logic (API interaction + history management) out of the UI layer.

### B. Asynchronous Storage Optimization
- Move all `_save_history()` calls to a background thread to prevent UI stutters when writing large JSON payloads to disk.
- Implement a "dirty" flag or a debounced save mechanism to avoid writing to disk on every single message chunk if possible (or only save on full message completion).

### C. Refine `markdown_view.py`
- **Theme Support:** Update `_create_code_widget` to switch syntax highlighting schemes based on the system's light/dark mode.
- **Robust Syncing:** Refactor `_sync_view` to use a cleaner diffing approach to minimize widget churn during streaming.
- **Table Formatting:** Improve the ASCII table renderer to handle multi-line cells or more complex alignments.

### D. Network Layer Refinement (`ollama.py`)
- Standardize the return types and error handling. 
- Use a single session object (if using a more advanced library) or a more formal worker pattern to ensure main thread safety.

---

## 4. Phase 5: SQLite Migration

**Goal:** Replace JSON-based storage with SQLite to improve performance, scalability, and querying capabilities.

### Proposed Changes:
1. **Schema Design:** Define a normalized database schema:
    - `hosts`: Stores host configurations.
    - `chats`: Stores chat metadata (ID, title, timestamps, model, system prompt, options).
    - `messages`: Stores individual messages linked to `chats`.
2. **Database Manager:** Create a new `src/database.py` or refactor `src/storage.py` to handle SQLite connections, schema initialization, and migrations.
3. **Data Migration:** Implement a migration script to import data from `history.json` and `hosts.json` into the new SQLite database on first run.
4. **Asynchronous I/O:** Ensure all database operations are performed off the main thread to keep the UI responsive.
5. **Memory Optimization:** Instead of loading the entire history into memory, load chat summaries for the sidebar and fetch messages only when a chat is opened.

---

## Proposed Implementation Phasing

**Phase 1: Cleanup & Standards** (COMPLETED)
- Extract CSS to a `.css` resource file.
- Add Type Hinting and Docstrings.
- Move UI properties from Python to `.ui` files.

**Phase 2: Modularization** (IN PROGRESS)
- Break `tab.py` into smaller custom widgets.
- Extract session logic to `src/session.py`.

**Phase 3: Storage & Performance**
- Optimize `storage.py` with background saving (interim for JSON).
- Improve `markdown_view.py` stability and theme support.

**Phase 4: Network & State Management**
- Finalize the separation of UI and API logic.
- Refine thread/async management for API calls.

**Phase 5: SQLite Migration**
- Implement SQLite-based storage.
- Migrate data from JSON to SQLite.
- Optimize history loading and message retrieval.
