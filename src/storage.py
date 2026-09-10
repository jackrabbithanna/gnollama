import os
import copy
import uuid
import time
from typing import List, Dict, Any, Optional, Callable
from gi.repository import GLib

from .database import DatabaseManager
from .writer import OrderedWriter

class ChatStorage:
    """Handles persistence for chat history and host configurations using SQLite."""

    def __init__(self, storage_dir=None) -> None:
        self.storage_dir: str = storage_dir or os.path.join(GLib.get_user_data_dir(), "gnollama")
        if not os.path.exists(self.storage_dir):
            os.makedirs(self.storage_dir)
            
        self.history_file: str = os.path.join(self.storage_dir, "history.json")
        self.hosts_file: str = os.path.join(self.storage_dir, "hosts.json")
        self.db_path: str = os.path.join(self.storage_dir, "gnollama.db")

        # Initialize SQLite Database Manager
        self.db = DatabaseManager(self.db_path)

        self.on_error = None
        self.writer = OrderedWriter(self._write_failed)

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

    def _write_failed(self, error):
        def notify():
            if self.on_error:
                self.on_error(error)
            return False
        GLib.idle_add(notify)

    def _submit(self, fn, *args, on_done=None, **kwargs):
        def job():
            result = fn(*args, **kwargs)
            if on_done:
                def notify():
                    on_done()
                    return False
                GLib.idle_add(notify)
            return result
        return self.writer.submit(job)

    def create_chat(self, model=''):
        chat_id = str(uuid.uuid4())
        timestamp = time.time()
        self._submit(self.db.create_chat, chat_id, 'New Chat', timestamp, timestamp, model)
        return {'id': chat_id, 'title': 'New Chat', 'created_at': timestamp,
                'updated_at': timestamp, 'model': model, 'messages': [], 'options': {}}

    def save_chat(self, chat_id, messages, model=None, options=None, system=None,
                  host=None, on_done=None):
        return self._submit(self.db.save_chat, chat_id, copy.deepcopy(messages), model,
                            copy.deepcopy(options), system, host, on_done=on_done)

    def update_title(self, chat_id, title, on_done=None):
        return self._submit(self.db.update_chat_title, chat_id, title, time.time(), on_done=on_done)

    def update_chat_pinned(self, chat_id, is_pinned, on_done=None):
        return self._submit(self.db.update_chat_pinned, chat_id, is_pinned, on_done=on_done)

    def delete_chat(self, chat_id, on_done=None):
        return self._submit(self.db.delete_chat, chat_id, on_done=on_done)

    def cleanup_empty_chats(self, on_done=None):
        return self._submit(self.db.cleanup_empty_chats, on_done=on_done)

    def clear_all_chats(self, on_done=None):
        return self._submit(self.db.clear_all_chats, on_done=on_done)
