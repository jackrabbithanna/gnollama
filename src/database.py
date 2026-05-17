import sqlite3
import os
import json
import base64
from typing import List, Dict, Any, Optional

# Sequential migrations list
# Add future SQL scripts to this array to run sequentially.
# E.g. MIGRATIONS = ["ALTER TABLE chats ADD COLUMN is_pinned INTEGER DEFAULT 0;"]
MIGRATIONS: List[str] = []

class DatabaseManager:
    """Manages SQLite database initialization and operations."""
    
    def __init__(self, db_path: str) -> None:
        self.db_path: str = db_path
        self._init_db()
        self._run_migrations()

    def _get_conn(self) -> sqlite3.Connection:
        """Returns a database connection with foreign key support enabled."""
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.row_factory = sqlite3.Row
        return conn

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
            
            if current_version >= target_version:
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
                    conn.execute("BEGIN TRANSACTION;")
                    
                    if isinstance(migration_sql, str):
                        conn.executescript(migration_sql)
                    
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
            cursor = conn.execute("SELECT id, name, hostname, is_default FROM hosts")
            return [
                {
                    "id": row["id"],
                    "name": row["name"],
                    "hostname": row["hostname"],
                    "default": bool(row["is_default"])
                }
                for row in cursor.fetchall()
            ]

    def get_host(self, host_id: str) -> Optional[Dict[str, Any]]:
        """Returns a specific host by its ID."""
        with self._get_conn() as conn:
            cursor = conn.execute("SELECT id, name, hostname, is_default FROM hosts WHERE id = ?", (host_id,))
            row = cursor.fetchone()
            if row:
                return {
                    "id": row["id"],
                    "name": row["name"],
                    "hostname": row["hostname"],
                    "default": bool(row["is_default"])
                }
            return None

    def add_host(self, host_id: str, name: str, hostname: str, is_default: bool) -> None:
        """Adds a host to database."""
        with self._get_conn() as conn:
            conn.execute(
                "INSERT INTO hosts (id, name, hostname, is_default) VALUES (?, ?, ?, ?)",
                (host_id, name, hostname, 1 if is_default else 0)
            )
            conn.commit()

    def update_host(self, host_id: str, name: str, hostname: str, is_default: bool) -> None:
        """Updates an existing host configuration."""
        with self._get_conn() as conn:
            conn.execute(
                "UPDATE hosts SET name = ?, hostname = ?, is_default = ? WHERE id = ?",
                (name, hostname, 1 if is_default else 0, host_id)
            )
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
                SELECT id, title, created_at, updated_at, model, system_prompt, host_id, options 
                FROM chats 
                ORDER BY updated_at DESC
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
                    "messages": []  # Empty array by default for list queries
                })
            return chats

    def get_chat(self, chat_id: str) -> Optional[Dict[str, Any]]:
        """Returns a specific chat along with all its parsed and ordered messages."""
        with self._get_conn() as conn:
            cursor = conn.execute("""
                SELECT id, title, created_at, updated_at, model, system_prompt, host_id, options 
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

    def delete_chat(self, chat_id: str) -> None:
        """Deletes a chat and cascades to delete all messages and images."""
        with self._get_conn() as conn:
            conn.execute("DELETE FROM chats WHERE id = ?", (chat_id,))
            conn.commit()

    def cleanup_empty_chats(self) -> None:
        """Deletes chats that have no messages."""
        with self._get_conn() as conn:
            conn.execute("""
                DELETE FROM chats 
                WHERE id NOT IN (SELECT DISTINCT chat_id FROM messages)
            """)
            conn.commit()

    def clear_all_chats(self) -> None:
        """Truncates all chats from the database."""
        with self._get_conn() as conn:
            conn.execute("DELETE FROM chats")
            conn.commit()

    # --- Message CRUD Operations ---

    def get_messages(self, chat_id: str) -> List[Dict[str, Any]]:
        """Returns all messages belonging to a chat, with images decoded back to base64."""
        messages = []
        with self._get_conn() as conn:
            cursor = conn.execute("""
                SELECT id, role, content, model, thinking_content, api_details 
                FROM messages 
                WHERE chat_id = ? 
                ORDER BY order_index ASC
            """, (chat_id,))
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
                    
                messages.append(msg)
        return messages

    def save_messages(self, chat_id: str, messages: List[Dict[str, Any]]) -> None:
        """Saves a clean array of messages, replacing older ones. Decodes base64 images into BLOBs."""
        with self._get_conn() as conn:
            # Delete old messages; cascades to delete from message_images too
            conn.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
            
            for idx, msg in enumerate(messages):
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO messages (chat_id, role, content, model, thinking_content, api_details, order_index)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    chat_id,
                    msg.get("role"),
                    msg.get("content"),
                    msg.get("model"),
                    msg.get("thinking_content"),
                    json.dumps(msg.get("api_details")) if msg.get("api_details") else None,
                    idx
                ))
                msg_id = cursor.lastrowid
                
                # Save associated images
                images = msg.get("images", [])
                for img_b64 in images:
                    try:
                        if "," in img_b64:
                            img_data = base64.b64decode(img_b64.split(",")[1])
                        else:
                            img_data = base64.b64decode(img_b64)
                        
                        conn.execute("""
                            INSERT INTO message_images (message_id, image_data)
                            VALUES (?, ?)
                        """, (msg_id, sqlite3.Binary(img_data)))
                    except Exception as e:
                        print(f"Error decoding or saving image blob: {e}")
            conn.commit()
