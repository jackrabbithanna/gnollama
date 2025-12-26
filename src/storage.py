import json
import os
import uuid
import time
from gi.repository import GLib

class ChatStorage:
    def __init__(self):
        self.storage_dir = os.path.join(GLib.get_user_data_dir(), "gnollama")
        self.history_file = os.path.join(self.storage_dir, "history.json")
        
        if not os.path.exists(self.storage_dir):
            os.makedirs(self.storage_dir)
            
        self.chats = self._load_history()

    def _load_history(self):
        if not os.path.exists(self.history_file):
            return {}
        try:
            with open(self.history_file, 'r') as f:
                return json.load(f)
        except Exception as e:
            print(f"Error loading history: {e}")
            return {}

    def _save_history(self):
        try:
            # Only save chats that have messages
            chats_to_save = {k: v for k, v in self.chats.items() if v.get("messages") and len(v["messages"]) > 0}
            with open(self.history_file, 'w') as f:
                json.dump(chats_to_save, f, indent=2)
        except Exception as e:
            print(f"Error saving history: {e}")

    def get_all_chats(self):
        # Return list of chats sorted by updated_at desc
        chat_list = list(self.chats.values())
        chat_list.sort(key=lambda x: x.get('updated_at', 0), reverse=True)
        return chat_list

    def get_chat(self, chat_id):
        return self.chats.get(chat_id)

    def create_chat(self, model=""):
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

    def save_chat(self, chat_id, messages, model=None, options=None, system=None):
        if chat_id not in self.chats:
            return
        
        self.chats[chat_id]["messages"] = messages
        self.chats[chat_id]["updated_at"] = time.time()
        if model:
             self.chats[chat_id]["model"] = model
        if options is not None:
             self.chats[chat_id]["options"] = options
        if system is not None:
             self.chats[chat_id]["system"] = system
             
        # Auto-generate title if it's the default "New Chat" and we have messages
        if self.chats[chat_id]["title"] == "New Chat" and messages:
            # Simple heuristic: use first few words of first user message
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

    def update_title(self, chat_id, title):
        if chat_id in self.chats:
            self.chats[chat_id]["title"] = title
            self.chats[chat_id]["updated_at"] = time.time()
            self._save_history()

    def delete_chat(self, chat_id):
        if chat_id in self.chats:
            del self.chats[chat_id]
            self._save_history()
