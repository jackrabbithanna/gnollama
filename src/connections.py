"""SQLite connections shared by the repositories, one per thread."""
import sqlite3
import threading
import weakref
from contextlib import contextmanager
from typing import Iterator, Optional

from .vectors import load_extension


class _Slot:
    """A thread's connection; it closes when the thread ends."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn: Optional[sqlite3.Connection] = conn
        self.depth = 0

    def close(self) -> None:
        conn, self.conn = self.conn, None
        if conn is not None:
            conn.close()

    def __del__(self) -> None:
        self.close()


class ConnectionProvider:
    """Open one connection per thread and keep it for later use.

    The writer thread and each worker thread load sqlite-vec and set their
    pragmas once. After close(), every use opens and closes its own connection.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._local = threading.local()
        self._lock = threading.Lock()
        self._slots: 'weakref.WeakSet[_Slot]' = weakref.WeakSet()
        self._closed = False

    def _open(self) -> sqlite3.Connection:
        # close() may run on another thread than the one that opened it.
        conn = sqlite3.connect(self.path, check_same_thread=False)
        try:
            load_extension(conn)
            conn.execute('PRAGMA foreign_keys = ON;')
            conn.execute('PRAGMA journal_mode = WAL;')
            conn.execute('PRAGMA synchronous = NORMAL;')
            conn.row_factory = sqlite3.Row
            conn.create_function('casefold', 1, lambda value: (value or '').casefold(), deterministic=True)
        except Exception:
            conn.close()
            raise
        return conn

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Yield this thread's connection; uncommitted work is rolled back on exit."""
        with self._lock:
            slot: Optional[_Slot] = getattr(self._local, 'slot', None)
            if slot is not None and slot.conn is not None:
                slot.depth += 1
            else:
                slot = None
        if slot is None:
            slot = self._local.slot = _Slot(self._open())
            slot.depth = 1
            with self._lock:
                if not self._closed:
                    self._slots.add(slot)
        conn = slot.conn
        assert conn is not None
        try:
            yield conn
        finally:
            with self._lock:
                slot.depth -= 1
                finished = slot.depth == 0
                release = finished and self._closed
            # A nested use shares the outer transaction and leaves it open.
            if finished and conn.in_transaction:
                conn.rollback()
            if release:
                slot.close()

    def close(self) -> None:
        """Close idle connections now and busy ones as their work ends."""
        with self._lock:
            self._closed = True
            for slot in list(self._slots):
                if slot.depth == 0:
                    slot.close()
            self._slots = weakref.WeakSet()
