"""Hosts, chats, and their messages."""
import json
import time
from typing import Any, Dict, List, Optional

from .records import has_saved_work
from .store_common import Repository, append_messages, consume_draft, read_draft, read_messages, save_messages


class ChatDatabase(Repository):
    # --- Hosts CRUD Operations ---

    def get_all_hosts(self) -> List[Dict[str, Any]]:
        """Returns all configured hosts from database."""
        with self._get_conn() as conn:
            cursor = conn.execute("SELECT id, name, hostname, is_default, provider, credential_id FROM hosts")
            return [
                {
                    "id": row["id"],
                    "name": row["name"],
                    "hostname": row["hostname"],
                    "provider": row["provider"],
                    "credential_id": row["credential_id"],
                    "default": bool(row["is_default"])
                }
                for row in cursor.fetchall()
            ]

    def get_host(self, host_id: str) -> Optional[Dict[str, Any]]:
        """Returns a specific host by its ID."""
        with self._get_conn() as conn:
            cursor = conn.execute("SELECT id, name, hostname, is_default, provider, credential_id FROM hosts WHERE id = ?", (host_id,))
            row = cursor.fetchone()
            if row:
                return {
                    "id": row["id"],
                    "name": row["name"],
                    "hostname": row["hostname"],
                    "provider": row["provider"],
                    "credential_id": row["credential_id"],
                    "default": bool(row["is_default"])
                }
            return None

    def add_host(self, host_id: str, name: str, hostname: str, is_default: bool,
                 provider='ollama', credential_id=None) -> None:
        """Adds a host to database."""
        with self._get_conn() as conn:
            conn.execute(
                "INSERT INTO hosts (id, name, hostname, is_default, provider, credential_id) VALUES (?, ?, ?, ?, ?, ?)",
                (host_id, name, hostname, 1 if is_default else 0, provider, credential_id)
            )
            conn.commit()

    def update_host(self, host_id: str, name: str, hostname: str, is_default: bool,
                    provider=None, credential_id=None) -> None:
        """Updates an existing host configuration."""
        with self._get_conn() as conn:
            conn.execute(
                "UPDATE hosts SET name = ?, hostname = ?, is_default = ? WHERE id = ?",
                (name, hostname, 1 if is_default else 0, host_id)
            )
            if provider is not None:
                conn.execute('UPDATE hosts SET provider = ?, credential_id = ? WHERE id = ?',
                             (provider, credential_id, host_id))
            conn.commit()

    def set_default_host(self, host_id: str) -> None:
        """Sets a host as the default, clearing other defaults."""
        with self._get_conn() as conn:
            conn.execute("UPDATE hosts SET is_default = 0")
            conn.execute("UPDATE hosts SET is_default = 1 WHERE id = ?", (host_id,))
            conn.commit()

    def delete_host(self, host_id: str) -> None:
        """Deletes a host configuration."""
        with self._get_conn() as conn:
            conn.execute("DELETE FROM hosts WHERE id = ?", (host_id,))
            conn.commit()

    # --- Chats CRUD Operations ---

    def get_all_chats(self) -> List[Dict[str, Any]]:
        """Returns all chats sorted by update time descending, excluding their full messages."""
        with self._get_conn() as conn:
            cursor = conn.execute("""
                SELECT id, title, created_at, updated_at, model, system_prompt, host_id, options, is_pinned, kind
                FROM chats
                ORDER BY is_pinned DESC, updated_at DESC
            """)
            chats = []
            for row in cursor.fetchall():
                options_dict = {}
                if row["options"]:
                    try:
                        options_dict = json.loads(row["options"])
                    except Exception:
                        pass
                chats.append({
                    "id": row["id"],
                    "title": row["title"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                    "model": row["model"],
                    "system": row["system_prompt"],
                    "host": row["host_id"],
                    "options": options_dict,
                    "is_pinned": bool(row["is_pinned"]),
                    "kind": row["kind"],
                    "messages": []  # Empty array by default for list queries
                })
            return chats

    def get_chat(self, chat_id: str) -> Optional[Dict[str, Any]]:
        """Returns a specific chat along with all its parsed and ordered messages."""
        with self._get_conn() as conn:
            cursor = conn.execute("""
                SELECT id, title, created_at, updated_at, model, system_prompt, host_id, options, is_pinned, kind
                FROM chats WHERE id = ?
            """, (chat_id,))
            row = cursor.fetchone()
            if not row:
                return None

            options_dict = {}
            if row["options"]:
                try:
                    options_dict = json.loads(row["options"])
                except Exception:
                    pass

            messages = read_messages(conn, chat_id)

            return {
                "id": row["id"],
                "title": row["title"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "model": row["model"],
                "system": row["system_prompt"],
                "host": row["host_id"],
                "options": options_dict,
                "is_pinned": bool(row["is_pinned"]),
                "kind": row["kind"],
                "messages": messages
            }

    def create_chat(self, chat_id: str, title: str, created_at: float, updated_at: float, model: str) -> None:
        """Inserts a new empty chat into database."""
        with self._get_conn() as conn:
            conn.execute("""
                INSERT INTO chats (id, title, created_at, updated_at, model, system_prompt, host_id, options)
                VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL)
            """, (chat_id, title, created_at, updated_at, model))
            conn.commit()

    def update_chat(self, chat_id: str, model: Optional[str], options: Optional[Dict[str, Any]],
                    system_prompt: Optional[str], host_id: Optional[str], updated_at: float) -> None:
        """Updates chat settings and metadata fields."""
        options_json = json.dumps(options) if options else None
        with self._get_conn() as conn:
            conn.execute("""
                UPDATE chats
                SET model = ?, options = ?, system_prompt = ?, host_id = ?, updated_at = ?
                WHERE id = ?
            """, (model, options_json, system_prompt, host_id, updated_at, chat_id))
            conn.commit()

    def update_chat_title(self, chat_id: str, title: str, updated_at: float) -> None:
        """Updates a chat's title."""
        with self._get_conn() as conn:
            conn.execute("UPDATE chats SET title = ?, updated_at = ? WHERE id = ?", (title, updated_at, chat_id))
            conn.commit()

    def update_chat_pinned(self, chat_id: str, is_pinned: bool) -> None:
        """Updates a chat's pinned status."""
        with self._get_conn() as conn:
            conn.execute("UPDATE chats SET is_pinned = ? WHERE id = ?", (1 if is_pinned else 0, chat_id))
            conn.commit()

    def delete_chat(self, chat_id: str) -> None:
        """Deletes a chat and cascades to delete all messages and images."""
        with self._get_conn() as conn:
            conn.execute("DELETE FROM chats WHERE id = ?", (chat_id,))
            conn.commit()

    def cleanup_empty_chats(self, chat_id=None) -> None:
        """Discard unused tabs, preserving applied playground definitions."""
        with self._get_conn() as conn:
            query = 'SELECT id, title, kind, is_pinned, system_prompt, options FROM chats WHERE id NOT IN (SELECT chat_id FROM messages)'
            rows = conn.execute(query + (' AND id = ?' if chat_id is not None else ''),
                                (chat_id,) if chat_id is not None else ()).fetchall()
            for row in rows:
                options = json.loads(row['options'] or '{}')
                draft_row = conn.execute('SELECT id FROM drafts WHERE chat_id=?', (row['id'],)).fetchone()
                draft = read_draft(conn, draft_row['id']) if draft_row else None
                # A decision session is named after its first run, so its title alone is not work.
                title = '' if row['kind'] == 'decision' else row['title']
                if not has_saved_work(draft=draft, options=options, title=title, pinned=row['is_pinned'], system=row['system_prompt']):
                    conn.execute('DELETE FROM chats WHERE id = ?', (row['id'],))
            conn.commit()

    def clear_all_chats(self) -> None:
        """Truncates all chats from the database and vacuums to reclaim space."""
        with self._get_conn() as conn:
            conn.execute("DELETE FROM chats")
            conn.commit()
            conn.execute("VACUUM;")

    # --- Message CRUD Operations ---

    def get_messages(self, chat_id: str, limit=None, offset=0, include_ids=False) -> List[Dict[str, Any]]:
        """Returns all messages belonging to a chat, with images decoded back to base64."""
        with self._get_conn() as conn:
            return read_messages(conn, chat_id, limit, offset, include_ids)

    def append_messages(self, chat_id, messages, start=0):
        with self._get_conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if conn.execute('SELECT 1 FROM chats WHERE id=?', (chat_id,)).fetchone():
                append_messages(conn, chat_id, messages, start)
            conn.commit()

    def update_message(self, chat_id, uid, message):
        with self._get_conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT order_index FROM messages WHERE chat_id=? AND uid=?', (chat_id, uid)).fetchone()
            if row:
                append_messages(conn, chat_id, [dict(message, uid=uid)], row['order_index'])
            conn.commit()

    def save_messages(self, chat_id, messages):
        with self._get_conn() as conn:
            save_messages(conn, chat_id, messages)
            conn.commit()

    def save_tool_state(self, chat_id, messages=None, options=None):
        """Merge playground edits without overwriting unrelated saved settings."""
        with self._get_conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT options FROM chats WHERE id = ?', (chat_id,)).fetchone()
            if row is None:
                return
            saved = json.loads(row['options'] or '{}')
            saved.update(options or {})
            if messages is not None:
                save_messages(conn, chat_id, messages)
            conn.execute('UPDATE chats SET options = ?, updated_at = ? WHERE id = ?',
                         (json.dumps(saved), time.time(), chat_id))
            conn.commit()

    def save_chat(self, chat_id, messages, model=None, options=None, system=None, host=None, *, start=None, draft_id=None, draft_revision=None):
        """Atomically update an existing chat; never recreate a deleted chat."""
        with self._get_conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT title FROM chats WHERE id = ?', (chat_id,)).fetchone()
            if row is None:
                return
            title = row['title']
            if title == 'New Chat':
                prompt = next((m.get('content', '').strip() for m in messages
                               if m['role'] == 'user' and m.get('content', '').strip()), '')
                if prompt:
                    title = prompt.splitlines()[0][:30] + ('...' if len(prompt) > 30 else '')
            if host and not conn.execute('SELECT 1 FROM hosts WHERE id = ?', (host,)).fetchone():
                host = None
            conn.execute("""UPDATE chats SET title = ?, model = ?, options = ?,
                         system_prompt = ?, host_id = ?, updated_at = ? WHERE id = ?""",
                         (title, model, json.dumps(options or {}), system, host, time.time(), chat_id))
            if start is None:
                save_messages(conn, chat_id, messages)
            else:
                append_messages(conn, chat_id, messages, start)
            consume_draft(conn, draft_id, draft_revision)
            conn.commit()
