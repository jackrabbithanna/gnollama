"""The database file: schema, upgrades, and the repositories that share its connections."""
import os
import sqlite3
from datetime import datetime
from gettext import gettext as _
from pathlib import Path
from typing import Callable, List, Optional, Union

from .chat_store import ChatDatabase
from .collections_store import MIGRATION as COLLECTIONS_MIGRATION
from .connections import ConnectionProvider
from .knowledge_store import KnowledgeDatabase, MIGRATION as KNOWLEDGE_MIGRATION
from .model_conversation_store import ModelConversationDatabase, migrate_model_conversations
from .vectors import load_extension, migrate_vectors, preflight_legacy_vectors
from .web_store import MIGRATION as WEB_MIGRATION
from .workspace_store import WorkspaceDatabase, migrate_workspace

# Sequential migrations list
# Add SQL scripts or functions accepting (conn, progress=None) to run sequentially.
# E.g. MIGRATIONS = ["ALTER TABLE chats ADD COLUMN is_pinned INTEGER DEFAULT 0;"]
MIGRATIONS: List[Union[str, Callable[..., None]]] = [
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
    # Version 13: An embedding host is its type and address; every earlier row was built on an Ollama host.
    """
    ALTER TABLE knowledge_collections ADD COLUMN host_kind TEXT NOT NULL DEFAULT 'ollama';
    ALTER TABLE knowledge_indexes ADD COLUMN host_kind TEXT NOT NULL DEFAULT 'ollama';
    """,
]


class DatabaseUpgradeError(RuntimeError):
    def __init__(self, message, backup_path=None):
        super().__init__(message)
        self.backup_path = backup_path


class DatabaseManager:
    """Opens and upgrades the database, then hands out its repositories."""

    def __init__(self, db_path: str, progress=None) -> None:
        self.db_path: str = db_path
        self.backup_path: Optional[str] = None
        self.progress = progress or (lambda message: None)
        self.connections = ConnectionProvider(db_path)
        self.chats = ChatDatabase(self.connections)
        self.knowledge = KnowledgeDatabase(self.connections)
        self.workspace = WorkspaceDatabase(self.connections)
        self.model_conversations = ModelConversationDatabase(self.connections)
        try:
            self._prepare_upgrade()
            self._init_db()
            self._run_migrations()
        except Exception as exc:
            self.connections.close()
            raise DatabaseUpgradeError(str(exc), self.backup_path) from exc
        finally:
            # A startup progress callback can own the temporary GTK window.
            self.progress = lambda message: None

    def _get_conn(self):
        """This thread's connection, with foreign keys, WAL, and sqlite-vec set up."""
        return self.connections.connection()

    def close(self) -> None:
        self.connections.close()

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
