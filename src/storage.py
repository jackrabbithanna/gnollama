import os
import copy
import uuid
import time
from gettext import gettext as _
from threading import RLock
from typing import List, Dict, Any, Optional, Callable
from gi.repository import GLib

from .database import DatabaseManager
from .writer import OrderedWriter
from .credentials import CredentialStore, CredentialError
from . import ollama
from .services import Services

class ChatStorage:
    """Handles persistence for chat history and host configurations using SQLite."""

    def __init__(self, storage_dir=None, progress=None, credentials=None) -> None:
        self.credentials = credentials if credentials is not None else CredentialStore()
        self.services = Services()
        self._host_lock = RLock()
        self.storage_dir: str = storage_dir or os.path.join(GLib.get_user_data_dir(), "gnollama")
        if not os.path.exists(self.storage_dir):
            os.makedirs(self.storage_dir)
            
        self.history_file: str = os.path.join(self.storage_dir, "history.json")
        self.hosts_file: str = os.path.join(self.storage_dir, "hosts.json")
        self.db_path: str = os.path.join(self.storage_dir, "gnollama.db")

        # Initialize SQLite Database Manager
        self.db = DatabaseManager(self.db_path, progress=progress)

        from .library_repository import LibraryRepository
        self.library = LibraryRepository(self.db)
        self.on_error = None
        self.pending_callbacks = 0
        self.writer = OrderedWriter(self._write_failed)
        self.db.interrupt_knowledge_indexes()
        self.db.interrupt_comparisons()
        from .knowledge import KnowledgeService
        self.knowledge = KnowledgeService(self)

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
        self.services.catalog.invalidate()
        host_id = str(uuid.uuid4())
        self.db.add_host(host_id, name, hostname, is_default)
        if is_default:
            self.db.set_default_host(host_id)
        return self.db.get_host(host_id)

    def update_host(self, host_id: str, name: str, hostname: str, is_default: bool = False) -> Optional[Dict[str, Any]]:
        """Updates an existing host configuration."""
        self.services.catalog.invalidate()
        self.db.update_host(host_id, name, hostname, is_default)
        if is_default:
            self.db.set_default_host(host_id)
        return self.db.get_host(host_id)

    def delete_host(self, host_id: str) -> None:
        """Deletes a host configuration."""
        self.services.catalog.invalidate()
        with self._host_lock:
            host = self.get_host(host_id)
            if host:
                self.credentials.clear(host.get('credential_id'))
            self.db.delete_host(host_id)
            self.credentials.forget_session(host_id)

    def connection(self, host):
        """Snapshot a host's destination without retrieving its secret."""
        if ollama.is_cloud(host):
            return ollama.Connection(host['hostname'], host['id'], host.get('credential_id'),
                                     credentials=self.credentials)
        return host['hostname']

    def save_host(self, name, hostname, is_default=False, *, host_id=None,
                  provider='ollama', api_key='', session_only=False):
        """Save from a worker; a keyring failure leaves the host unchanged."""
        self.services.catalog.invalidate()
        with self._host_lock:
            previous = self.get_host(host_id) if host_id else None
            if host_id and previous is None:
                raise ValueError(_('This host was removed.'))
            host_id = host_id or str(uuid.uuid4())
            reference = previous.get('credential_id') if previous else None
            hostname = ollama.validate_host(hostname)
            if provider not in ('ollama', 'ollama_cloud'):
                raise ValueError(_('Invalid host type.'))
            if not name.strip():
                raise ValueError(_('Enter a name for this server.'))
            if provider == 'ollama_cloud':
                if hostname != ollama.CLOUD_URL:
                    raise ValueError(_('Ollama Cloud requires https://ollama.com.'))
                if api_key:
                    api_key = self.credentials.validate(api_key)
                    if not session_only:
                        reference = reference or str(uuid.uuid4())
                        self.credentials.save(reference, api_key)
                elif not reference and not self.credentials.has_session(host_id):
                    raise CredentialError(_('An API key is required. Edit this host to enter one.'))
            else:
                self.credentials.clear(reference)
                reference = None
            if previous:
                self.db.update_host(host_id, name.strip(), hostname, is_default, provider, reference)
            else:
                try:
                    self.db.add_host(host_id, name.strip(), hostname, is_default, provider, reference)
                except Exception:
                    if reference:
                        self.credentials.clear(reference)
                    raise
            if provider == 'ollama_cloud' and api_key and session_only:
                self.credentials.set_session(host_id, api_key)
            elif provider != 'ollama_cloud' or api_key:
                self.credentials.forget_session(host_id)
            if is_default:
                self.db.set_default_host(host_id)
            return self.get_host(host_id)

    # --- Chats Management ---

    def get_all_chats(self) -> List[Dict[str, Any]]:
        """Returns all chats, sorted by last update time (descending)."""
        return self.db.get_all_chats()

    def export_snapshot(self, id):
        return self.db.export_snapshot(id)

    def chat_title(self, id):
        return self.db.chat_title(id)

    def conversation_page(self, id, match_uid=None):
        return self.db.conversation_page(id, match_uid)

    def list_history(self, query='', limit=100, offset=0):
        return self.db.list_history(query, limit, offset)

    def list_drafts(self, limit=100, offset=0):
        return self.db.list_drafts(limit, offset)

    def get_draft(self, id):
        return self.db.get_draft(id)

    def save_draft(self, draft, on_done=None):
        return self._submit(self.db.save_draft, copy.deepcopy(draft), on_done=on_done)

    def delete_draft(self, id, revision=None, on_done=None):
        return self._submit(self.db.delete_draft, id, revision, on_done=on_done)

    def get_messages(self, id, limit=None, offset=0, include_ids=False):
        return self.db.get_messages(id, limit, offset, include_ids)

    def save_chat_delta(self, chat_id, messages, start, *, draft_id=None, draft_revision=None, on_done=None, **settings):
        snapshot = copy.deepcopy(messages)
        for message in snapshot:
            message.setdefault('uid', str(uuid.uuid4()))
        return self._submit(self.db.save_chat, chat_id, snapshot, start=start, draft_id=draft_id,
                            draft_revision=draft_revision, on_done=on_done, **copy.deepcopy(settings))

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
                self.pending_callbacks += 1
                def notify():
                    try:
                        on_done()
                    finally:
                        self.pending_callbacks -= 1
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

    def save_tool_state(self, chat_id, messages=None, options=None, on_done=None):
        return self._submit(self.db.save_tool_state, chat_id, copy.deepcopy(messages),
                            copy.deepcopy(options), on_done=on_done)

    def update_chat_pinned(self, chat_id, is_pinned, on_done=None):
        return self._submit(self.db.update_chat_pinned, chat_id, is_pinned, on_done=on_done)

    def delete_chat(self, chat_id, on_done=None):
        return self._submit(self.db.delete_chat, chat_id, on_done=on_done)

    def cleanup_empty_chats(self, on_done=None):
        return self._submit(self.db.cleanup_empty_chats, on_done=on_done)

    def cleanup_empty_chat(self, chat_id, on_done=None):
        return self._submit(self.db.cleanup_empty_chats, chat_id, on_done=on_done)

    def clear_all_chats(self, on_done=None):
        return self._submit(self.db.clear_all_chats, on_done=on_done)
