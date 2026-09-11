"""Debounced draft snapshots with independently versioned consumption."""
import copy
import uuid
from dataclasses import asdict
from gi.repository import GLib
from .records import Draft, has_saved_work


class DraftController:
    def __init__(self, storage, mode, read, id=None, chat_id=None):
        self.storage, self.mode, self.read = storage, mode, read
        self.id = id or chat_id or str(uuid.uuid4())
        self.chat_id = chat_id
        self.revision = 0
        self.source = None
        self.restoring = False
        self.closed = False
        self.dirty = False

    def changed(self, *args):
        if self.restoring or self.closed:
            return
        self.revision += 1
        self.dirty = True
        if self.source is not None:
            GLib.source_remove(self.source)
        self.source = GLib.timeout_add(500, self.flush)

    def snapshot(self):
        values = self.read()
        return asdict(Draft(id=self.id, mode=self.mode, chat_id=self.chat_id,
            revision=self.revision, **copy.deepcopy(values)))

    def flush(self):
        if self.source is not None:
            GLib.source_remove(self.source)
            self.source = None
        if self.closed or self.storage.writer.closed:
            return False
        if not self.dirty:
            return False
        try:
            draft = self.snapshot()
        except (ValueError, OSError) as exc:
            self.last_error = str(exc)
            return False
        self.dirty = False
        if has_saved_work(draft=draft):
            self.storage.save_draft(draft)
        else:
            self.storage.delete_draft(self.id, self.revision)
        return False

    def consumed(self):
        if self.source is not None:
            GLib.source_remove(self.source)
            self.source = None
        self.dirty = False

    def close(self, discard=False):
        if discard:
            self.consumed()
        else:
            self.flush()
        self.closed = True
