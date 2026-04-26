import json
import os
import uuid
import time
from typing import List, Dict, Any, Optional
from gi.repository import GLib

class ChatStorage:
    """Handles persistence for chat history and host configurations."""

    def __init__(self) -> None:
        self.storage_dir: str = os.path.join(GLib.get_user_data_dir(), "gnollama")
        self.history_file: str = os.path.join(self.storage_dir, "history.json")
        
        if not os.path.exists(self.storage_dir):
            os.makedirs(self.storage_dir)
            
        self.hosts_file: str = os.path.join(self.storage_dir, "hosts.json")
        self.chats: Dict[str, Dict[str, Any]] = self._load_history()
        self.hosts: List[Dict[str, Any]] = self._load_hosts()

    def _load_hosts(self) -> List[Dict[str, Any]]:
        """Loads host configurations from the hosts file."""
        if not os.path.exists(self.hosts_file):
            default_hosts = [{
                "id": str(uuid.uuid4()),
                "name": "localhost",
                "hostname": "http://localhost:11434",
                "default": True
            }]
            self._save_hosts(default_hosts)
            return default_hosts
        try:
            with open(self.hosts_file, 'r') as f:
                return json.load(f)
        except Exception as e:
            print(f"Error loading hosts: {e}")
            return []

    def _save_hosts(self, hosts: Optional[List[Dict[str, Any]]] = None) -> None:
        """Saves host configurations to the hosts file."""
        if hosts is None:
            hosts = self.hosts
            
        if hosts:
            has_default = any(h.get("default", False) for h in hosts)
            if not has_default:
                hosts[0]["default"] = True
                
        try:
            with open(self.hosts_file, 'w') as f:
                json.dump(hosts, f, indent=2)
        except Exception as e:
            print(f"Error saving hosts: {e}")

    def get_all_hosts(self) -> List[Dict[str, Any]]:
        """Returns all configured hosts."""
        return self.hosts

    def get_host(self, host_id: str) -> Optional[Dict[str, Any]]:
        """Returns a specific host by its ID."""
        for host in self.hosts:
            if host["id"] == host_id:
                return host
        return None

    def set_default_host(self, host_id: str) -> None:
        """Sets a host as the default."""
        for host in self.hosts:
            host["default"] = (host["id"] == host_id)
        self._save_hosts()

    def add_host(self, name: str, hostname: str, is_default: bool = False) -> Dict[str, Any]:
        """Adds a new host configuration."""
        host_id = str(uuid.uuid4())
        new_host = {
            "id": host_id,
            "name": name,
            "hostname": hostname,
            "default": is_default
        }
        self.hosts.append(new_host)
        if is_default:
            self.set_default_host(host_id)
        else:
            self._save_hosts()
        return new_host

    def update_host(self, host_id: str, name: str, hostname: str, is_default: bool = False) -> Optional[Dict[str, Any]]:
        """Updates an existing host configuration."""
        for host in self.hosts:
            if host["id"] == host_id:
                host["name"] = name
                host["hostname"] = hostname
                if is_default:
                    self.set_default_host(host_id)
                else:
                    host["default"] = False
                    self._save_hosts()
                return host
        return None

    def delete_host(self, host_id: str) -> None:
        """Deletes a host configuration."""
        self.hosts = [h for h in self.hosts if h["id"] != host_id]
        self._save_hosts()

    def _load_history(self) -> Dict[str, Dict[str, Any]]:
        """Loads chat history from the history file."""
        if not os.path.exists(self.history_file):
            return {}
        try:
            with open(self.history_file, 'r') as f:
                return json.load(f)
        except Exception as e:
            print(f"Error loading history: {e}")
            return {}

    def _save_history(self) -> None:
        """Saves current chat history to the history file."""
        try:
            with open(self.history_file, 'w', encoding='utf-8') as f:
                json.dump(self.chats, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"Error saving history: {e}")

    def get_all_chats(self) -> List[Dict[str, Any]]:
        """Returns all chats, sorted by last update time (descending)."""
        chat_list = list(self.chats.values())
        chat_list.sort(key=lambda x: x.get('updated_at', 0), reverse=True)
        return chat_list

    def get_chat(self, chat_id: str) -> Optional[Dict[str, Any]]:
        """Returns a specific chat by its ID."""
        return self.chats.get(chat_id)

    def create_chat(self, model: str = "") -> Dict[str, Any]:
        """Creates a new empty chat."""
        chat_id = str(uuid.uuid4())
        timestamp = time.time()
        chat_data = {
            "id": chat_id,
            "title": "New Chat",
            "created_at": timestamp,
            "updated_at": timestamp,
            "model": model,
            "messages": []
        }
        self.chats[chat_id] = chat_data
        self._save_history()
        return chat_data

    def save_chat(self, chat_id: str, messages: List[Dict[str, Any]], 
                  model: Optional[str] = None, options: Optional[Dict[str, Any]] = None, 
                  system: Optional[str] = None, host: Optional[str] = None) -> None:
        """Saves messages and settings to a chat."""
        if chat_id not in self.chats:
            return
        
        import copy
        self.chats[chat_id]["messages"] = copy.deepcopy(messages)
        self.chats[chat_id]["updated_at"] = time.time()
        self.chats[chat_id]["model"] = model
        self.chats[chat_id]["options"] = copy.deepcopy(options)
        self.chats[chat_id]["system"] = system
        self.chats[chat_id]["host"] = host
             
        # Auto-generate title if it's the default "New Chat" and we have messages
        if self.chats[chat_id]["title"] == "New Chat" and messages:
            for msg in messages:
                if msg.get("role") == "user":
                    content = msg.get("content", "").strip()
                    if content:
                        # Take first 30 chars/first line
                        title = content.split('\n')[0][:30]
                        if len(content) > 30:
                            title += "..."
                        self.chats[chat_id]["title"] = title
                        break
        
        self._save_history()

    def update_title(self, chat_id: str, title: str) -> None:
        """Updates the title of a chat."""
        if chat_id in self.chats:
            self.chats[chat_id]["title"] = title
            self.chats[chat_id]["updated_at"] = time.time()
            self._save_history()

    def delete_chat(self, chat_id: str) -> None:
        """Deletes a chat."""
        if chat_id in self.chats:
            del self.chats[chat_id]
            self._save_history()

    def cleanup_empty_chats(self) -> None:
        """Deletes all chats that have no messages."""
        empty_ids = [chat_id for chat_id, chat in self.chats.items() if not chat.get("messages")]
        for chat_id in empty_ids:
            del self.chats[chat_id]
        if empty_ids:
            self._save_history()

    def clear_all_chats(self) -> None:
        """Deletes all chat history."""
        self.chats = {}
        self._save_history()
