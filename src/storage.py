import os
import uuid
import time
from typing import List, Dict, Any, Optional, Callable
from gi.repository import GLib

from .database import DatabaseManager

class ChatStorage:
    """Handles persistence for chat history and host configurations using SQLite."""

    def __init__(self) -> None:
        self.storage_dir: str = os.path.join(GLib.get_user_data_dir(), "gnollama")
        if not os.path.exists(self.storage_dir):
            os.makedirs(self.storage_dir)
            
        self.history_file: str = os.path.join(self.storage_dir, "history.json")
        self.hosts_file: str = os.path.join(self.storage_dir, "hosts.json")
        self.db_path: str = os.path.join(self.storage_dir, "gnollama.db")

        # Initialize SQLite Database Manager
        self.db = DatabaseManager(self.db_path)

        # Detect legacy JSON files and rename them
        self._handle_legacy_json()

        # Add default host if hosts table is empty
        if not self.db.get_all_hosts():
            self.db.add_host(
                host_id=str(uuid.uuid4()),
                name="localhost",
                hostname="http://localhost:11434",
                is_default=True
            )

    def _handle_legacy_json(self) -> None:
        """Renames legacy JSON files to .legacy so they aren't parsed but kept as backups."""
        if os.path.exists(self.history_file):
            try:
                os.rename(self.history_file, self.history_file + ".legacy")
                print(f"Renamed legacy {self.history_file} to history.json.legacy")
            except Exception as e:
                print(f"Error renaming legacy history file: {e}")
                
        if os.path.exists(self.hosts_file):
            try:
                os.rename(self.hosts_file, self.hosts_file + ".legacy")
                print(f"Renamed legacy {self.hosts_file} to hosts.json.legacy")
            except Exception as e:
                print(f"Error renaming legacy hosts file: {e}")

    # --- Hosts Management ---

    def get_all_hosts(self) -> List[Dict[str, Any]]:
        """Returns all configured hosts."""
        return self.db.get_all_hosts()

    def get_host(self, host_id: str) -> Optional[Dict[str, Any]]:
        """Returns a specific host by its ID."""
        return self.db.get_host(host_id)

    def set_default_host(self, host_id: str) -> None:
        """Sets a host as the default."""
        self.db.set_default_host(host_id)

    def add_host(self, name: str, hostname: str, is_default: bool = False) -> Dict[str, Any]:
        """Adds a new host configuration."""
        host_id = str(uuid.uuid4())
        self.db.add_host(host_id, name, hostname, is_default)
        if is_default:
            self.db.set_default_host(host_id)
        return self.db.get_host(host_id)

    def update_host(self, host_id: str, name: str, hostname: str, is_default: bool = False) -> Optional[Dict[str, Any]]:
        """Updates an existing host configuration."""
        self.db.update_host(host_id, name, hostname, is_default)
        if is_default:
            self.db.set_default_host(host_id)
        return self.db.get_host(host_id)

    def delete_host(self, host_id: str) -> None:
        """Deletes a host configuration."""
        self.db.delete_host(host_id)

    # --- Chats Management ---

    def get_all_chats(self) -> List[Dict[str, Any]]:
        """Returns all chats, sorted by last update time (descending)."""
        return self.db.get_all_chats()

    def get_chat(self, chat_id: str) -> Optional[Dict[str, Any]]:
        """Returns a specific chat by its ID."""
        return self.db.get_chat(chat_id)

    def create_chat(self, model: str = "") -> Dict[str, Any]:
        """Creates a new empty chat."""
        chat_id = str(uuid.uuid4())
        timestamp = time.time()
        self.db.create_chat(chat_id, "New Chat", timestamp, timestamp, model)
        return self.db.get_chat(chat_id)

    def save_chat(self, chat_id: str, messages: List[Dict[str, Any]], 
                  model: Optional[str] = None, options: Optional[Dict[str, Any]] = None, 
                  system: Optional[str] = None, host: Optional[str] = None,
                  on_done: Optional[Callable[[], None]] = None) -> None:
        """Saves messages and settings to a chat asynchronously."""
        import copy
        messages_snapshot = copy.deepcopy(messages)
        options_snapshot = copy.deepcopy(options) if options else None

        def save_task() -> None:
            try:
                # Auto-generate title if it's the default "New Chat" and we have messages
                chat_data = self.db.get_chat(chat_id)
                if chat_data and chat_data.get("title") == "New Chat" and messages_snapshot:
                    for msg in messages_snapshot:
                        if msg.get("role") == "user":
                            content = msg.get("content", "").strip()
                            if content:
                                # Take first 30 chars/first line
                                title = content.split('\n')[0][:30]
                                if len(content) > 30:
                                    title += "..."
                                self.db.update_chat_title(chat_id, title, time.time())
                                break

                # Update chat properties
                self.db.update_chat(
                    chat_id=chat_id,
                    model=model,
                    options=options_snapshot,
                    system_prompt=system,
                    host_id=host,
                    updated_at=time.time()
                )

                # Save new set of messages
                self.db.save_messages(chat_id, messages_snapshot)

                if on_done:
                    GLib.idle_add(on_done)
            except Exception as e:
                print(f"Error saving chat asynchronously in DB: {e}")

        try:
            from .session import worker
            worker.submit(save_task)
        except ImportError:
            save_task()

    def update_title(self, chat_id: str, title: str) -> None:
        """Updates the title of a chat."""
        self.db.update_chat_title(chat_id, title, time.time())

    def update_chat_pinned(self, chat_id: str, is_pinned: bool) -> None:
        """Updates the pinned status of a chat."""
        self.db.update_chat_pinned(chat_id, is_pinned)

    def delete_chat(self, chat_id: str) -> None:
        """Deletes a chat."""
        self.db.delete_chat(chat_id)

    def cleanup_empty_chats(self) -> None:
        """Deletes all chats that have no messages."""
        self.db.cleanup_empty_chats()

    def clear_all_chats(self) -> None:
        """Deletes all chat history."""
        self.db.clear_all_chats()
