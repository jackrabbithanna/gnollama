import sqlite3
import time
from contextlib import contextmanager
import json
import base64
import os
from pathlib import Path
from datetime import datetime
from gettext import gettext as _
from typing import List, Dict, Any, Optional
from .knowledge_store import KnowledgeDatabase, MIGRATION as KNOWLEDGE_MIGRATION
from .vectors import load_extension, migrate_vectors, preflight_legacy_vectors
from .collections_store import MIGRATION as COLLECTIONS_MIGRATION
from .web_store import MIGRATION as WEB_MIGRATION
from .records import has_saved_work
from .workspace_store import WorkspaceDatabase, migrate_workspace
from .model_conversation_store import ModelConversationDatabase, migrate_model_conversations

# Sequential migrations list
# Add SQL scripts or functions accepting (conn, progress=None) to run sequentially.
# E.g. MIGRATIONS = ["ALTER TABLE chats ADD COLUMN is_pinned INTEGER DEFAULT 0;"]
MIGRATIONS = [
    # Version 2: Add indexes for faster foreign key queries and cascades
    """
    CREATE INDEX IF NOT EXISTS idx_messages_chat_id ON messages(chat_id);
    CREATE INDEX IF NOT EXISTS idx_message_images_message_id ON message_images(message_id);
    """,
    # Version 3: Add is_pinned to chats table
    """
    ALTER TABLE chats ADD COLUMN is_pinned INTEGER DEFAULT 0;
    """,
    # Version 4: Preserve response outcomes and generation statistics.
    "ALTER TABLE messages ADD COLUMN response_metadata TEXT;",
    # Version 5: Native tool calls and manually supplied results.
    """
    ALTER TABLE messages ADD COLUMN tool_calls TEXT;
    ALTER TABLE messages ADD COLUMN tool_name TEXT;
    ALTER TABLE messages ADD COLUMN tool_call_id TEXT;
    """,
    KNOWLEDGE_MIGRATION,
    migrate_vectors,
    COLLECTIONS_MIGRATION,
    WEB_MIGRATION,
    # Version 10: Cloud host configuration; secrets stay in the desktop keyring.
    """
    ALTER TABLE hosts ADD COLUMN provider TEXT NOT NULL DEFAULT 'ollama';
    ALTER TABLE hosts ADD COLUMN credential_id TEXT;
    """,
    migrate_workspace,
    migrate_model_conversations,
]


class DatabaseUpgradeError(RuntimeError):
    def __init__(self, message, backup_path=None):
        super().__init__(message)
        self.backup_path = backup_path


class DatabaseManager(KnowledgeDatabase, WorkspaceDatabase, ModelConversationDatabase):
    """Manages SQLite database initialization and operations."""

    def __init__(self, db_path: str, progress=None) -> None:
        self.db_path: str = db_path
        self.backup_path = None
        self.progress = progress or (lambda message: None)
        try:
            self._prepare_upgrade()
            self._init_db()
            self._run_migrations()
        except Exception as exc:
            raise DatabaseUpgradeError(str(exc), self.backup_path) from exc
        finally:
            # A startup progress callback can own the temporary GTK window.
            self.progress = lambda message: None

    def _prepare_upgrade(self):
        if not os.path.exists(self.db_path) or not os.path.getsize(self.db_path):
            return
        source = sqlite3.connect(Path(self.db_path).resolve().as_uri() + '?mode=ro', uri=True)
        try:
            version = self._get_version(source)
            target = len(MIGRATIONS) + 1
            if version > target:
                raise ValueError(_('This database was created by a newer Gnollama version. Update Gnollama to open it.'))
            if version == target:
                return
            # Check the native dependency and limits before changing an old schema.
            load_extension(source)
            if version == 6:
                preflight_legacy_vectors(source)
            self.progress(_('Backing up the database…'))
            stamp = datetime.now().strftime('%Y%m%d-%H%M%S-%f')
            backup = self.db_path + f'.pre-v{target}-{stamp}.bak'
            # Exclusive creation avoids overwriting an earlier recovery copy.
            fd = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            destination = None
            try:
                destination = sqlite3.connect(backup)
                source.backup(destination, pages=256)
                destination.close()
                destination = None
                self.backup_path = backup
            except Exception:
                if destination is not None:
                    destination.close()
                os.unlink(backup)
                raise
        finally:
            source.close()

    @contextmanager
    def _get_conn(self):
        """Returns a database connection with foreign key, WAL, and fast-sync enabled."""
        conn = sqlite3.connect(self.db_path)
        try:
            load_extension(conn)
            conn.execute("PRAGMA foreign_keys = ON;")
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.execute("PRAGMA synchronous = NORMAL;")
            conn.row_factory = sqlite3.Row
            conn.create_function('casefold', 1, lambda value: (value or '').casefold(), deterministic=True)
            yield conn
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        """Initializes tables if they do not exist."""
        with self._get_conn() as conn:
            # Create hosts table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS hosts (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    hostname TEXT NOT NULL,
                    is_default INTEGER NOT NULL DEFAULT 0
                )
            """)

            # Create chats table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS chats (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    model TEXT,
                    system_prompt TEXT,
                    host_id TEXT,
                    options TEXT,
                    FOREIGN KEY(host_id) REFERENCES hosts(id) ON DELETE SET NULL
                )
            """)

            # Create messages table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    model TEXT,
                    thinking_content TEXT,
                    api_details TEXT,
                    order_index INTEGER NOT NULL,
                    FOREIGN KEY(chat_id) REFERENCES chats(id) ON DELETE CASCADE
                )
            """)

            # Create message_images table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS message_images (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id INTEGER NOT NULL,
                    image_data BLOB NOT NULL,
                    FOREIGN KEY(message_id) REFERENCES messages(id) ON DELETE CASCADE
                )
            """)
            conn.commit()

    def _get_version(self, conn: sqlite3.Connection) -> int:
        """Retrieves the current schema version from SQLite header."""
        cursor = conn.execute("PRAGMA user_version;")
        return cursor.fetchone()[0]

    def _set_version(self, conn: sqlite3.Connection, version: int) -> None:
        """Sets the schema version in SQLite header."""
        conn.execute(f"PRAGMA user_version = {version};")

    def _run_migrations(self) -> None:
        """Sequential migration runner using SQLite PRAGMA user_version."""
        target_version = len(MIGRATIONS) + 1  # Base schema is Version 1

        with self._get_conn() as conn:
            current_version = self._get_version(conn)

            if current_version > target_version:
                raise ValueError(_('This database requires a newer Gnollama version.'))
            if current_version == target_version:
                return  # Database is up-to-date

            print(f"Database migration needed: current version {current_version}, target version {target_version}")

            # Base case: Fresh database starts at 0. We set it to 1 immediately
            # because _init_db() has already created the baseline schema.
            if current_version == 0:
                self._set_version(conn, 1)
                current_version = 1

            # Apply missing migrations sequentially
            for ver in range(current_version, target_version):
                migration_idx = ver - 1  # 0-indexed MIGRATIONS list
                migration_sql = MIGRATIONS[migration_idx]

                try:
                    print(f"Applying database migration to Version {ver + 1}...")
                    self.progress(_('Upgrading the database to version {0}…').format(ver + 1))
                    conn.execute("BEGIN IMMEDIATE;")

                    if isinstance(migration_sql, str):
                        statement = ''
                        for fragment in migration_sql.split(';'):
                            statement += fragment + ';'
                            if sqlite3.complete_statement(statement):
                                conn.execute(statement)
                                statement = ''
                        if statement.strip(' \n\t;'):
                            raise ValueError('Incomplete migration statement')
                    else:
                        migration_sql(conn, progress=self.progress)

                    self._set_version(conn, ver + 1)
                    conn.commit()
                    print(f"Migration to Version {ver + 1} succeeded.")
                except Exception as e:
                    conn.rollback()
                    print(f"CRITICAL: Migration to Version {ver + 1} failed: {e}")
                    raise e

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

            messages = self.get_messages(chat_id)

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
            query = 'SELECT id, title, is_pinned, system_prompt, options FROM chats WHERE id NOT IN (SELECT chat_id FROM messages)'
            rows = conn.execute(query + (' AND id = ?' if chat_id is not None else ''),
                                (chat_id,) if chat_id is not None else ()).fetchall()
            for row in rows:
                options = json.loads(row['options'] or '{}')
                draft_row = conn.execute('SELECT id FROM drafts WHERE chat_id=?', (row['id'],)).fetchone()
                draft = self.get_draft(draft_row['id']) if draft_row else None
                if not has_saved_work(draft=draft, options=options, title=row['title'], pinned=row['is_pinned'], system=row['system_prompt']):
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
            return self._read_messages(conn, chat_id, limit, offset, include_ids)

    def _read_messages(self, conn, chat_id, limit=None, offset=0, include_ids=False):
        messages = []
        cursor = conn.execute("""
            SELECT id, role, content, model, thinking_content, api_details, response_metadata,
                   tool_calls, tool_name, tool_call_id, uid, extra
            FROM messages
            WHERE chat_id = ?
            ORDER BY order_index ASC LIMIT ? OFFSET ?
        """, (chat_id, -1 if limit is None else limit, offset))
        rows = cursor.fetchall()
        for row in rows:
            msg_id = row["id"]
            msg = {
                "role": row["role"],
                "content": row["content"]
            }
            if row["model"] is not None:
                msg["model"] = row["model"]
            if row["thinking_content"] is not None:
                msg["thinking_content"] = row["thinking_content"]
            if row["api_details"] is not None:
                try:
                    msg["api_details"] = json.loads(row["api_details"])
                except Exception:
                    pass

            if row["response_metadata"]:
                msg["response_metadata"] = json.loads(row["response_metadata"])
            if row['tool_calls'] is not None:
                msg['tool_calls'] = json.loads(row['tool_calls'])
            for key in ('tool_name', 'tool_call_id'):
                if row[key] is not None:
                    msg[key] = row[key]

            # Fetch attached images
            img_cursor = conn.execute("SELECT image_data FROM message_images WHERE message_id = ?", (msg_id,))
            img_rows = img_cursor.fetchall()
            if img_rows:
                images_b64 = []
                for img_row in img_rows:
                    img_bin = img_row["image_data"]
                    img_b64 = base64.b64encode(img_bin).decode("utf-8")
                    images_b64.append(img_b64)
                msg["images"] = images_b64

            msg.update(json.loads(row['extra'] or '{}'))
            if include_ids:
                msg['uid'] = row['uid']
            messages.append(msg)
        return messages

    def _append_messages(self, conn, chat_id, messages, start):
        import uuid
        fields = ('role', 'content', 'model', 'thinking_content', 'api_details',
                  'response_metadata', 'tool_calls', 'tool_name', 'tool_call_id', 'extra')
        json_fields = ('api_details', 'response_metadata', 'tool_calls')
        known = set(fields) | {'images', 'uid'}
        for index, message in enumerate(messages, start):
            uid = message.get('uid')
            old = conn.execute('SELECT * FROM messages WHERE chat_id=? AND (uid=? OR order_index=?)',
                               (chat_id, uid, index)).fetchone()
            values = {k: message.get(k) for k in fields}
            values['content'] = message.get('content', '')
            values['extra'] = json.dumps({k: v for k, v in message.items() if k not in known})
            for key in json_fields:
                values[key] = json.dumps(message[key]) if key in message and message[key] is not None else None
            if old is None:
                cursor = conn.execute('INSERT INTO messages(chat_id,uid,order_index,' + ','.join(fields)
                    + ') VALUES (' + ','.join('?' for _ in range(len(fields)+3)) + ')',
                    (chat_id, uid or str(uuid.uuid4()), index, *(values[k] for k in fields)))
                id = cursor.lastrowid
            else:
                id = old['id']
                if any(old[k] != values[k] for k in fields):
                    conn.execute('UPDATE messages SET ' + ','.join(k+'=?' for k in fields) + ' WHERE id=?',
                                 (*(values[k] for k in fields), id))
            images = [base64.b64decode(image.split(',', 1)[-1], validate=True) for image in message.get('images', [])]
            previous = [r[0] for r in conn.execute('SELECT image_data FROM message_images WHERE message_id=? ORDER BY id', (id,))] if old else []
            if images != previous:
                conn.execute('DELETE FROM message_images WHERE message_id=?', (id,))
                conn.executemany('INSERT INTO message_images(message_id,image_data) VALUES (?,?)', [(id, raw) for raw in images])

    def _save_messages(self, conn, chat_id, messages):
        self._append_messages(conn, chat_id, messages, 0)
        conn.execute('DELETE FROM messages WHERE chat_id=? AND order_index>=?', (chat_id, len(messages)))

    def append_messages(self, chat_id, messages, start=0):
        with self._get_conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if conn.execute('SELECT 1 FROM chats WHERE id=?', (chat_id,)).fetchone():
                self._append_messages(conn, chat_id, messages, start)
            conn.commit()

    def update_message(self, chat_id, uid, message):
        with self._get_conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT order_index FROM messages WHERE chat_id=? AND uid=?', (chat_id, uid)).fetchone()
            if row:
                self._append_messages(conn, chat_id, [dict(message, uid=uid)], row['order_index'])
            conn.commit()

    def save_messages(self, chat_id, messages):
        with self._get_conn() as conn:
            self._save_messages(conn, chat_id, messages)
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
                self._save_messages(conn, chat_id, messages)
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
                self._save_messages(conn, chat_id, messages)
            else:
                self._append_messages(conn, chat_id, messages, start)
            self._consume_draft(conn, draft_id, draft_revision)
            conn.commit()
