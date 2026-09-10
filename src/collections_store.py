"""Named collections reuse immutable document indexes rather than copying vectors."""
import json
import time
import uuid
from gettext import gettext as _


MIGRATION = """
CREATE TABLE knowledge_collections (
    id TEXT PRIMARY KEY, name TEXT NOT NULL CHECK(length(trim(name)) > 0),
    config_id TEXT NOT NULL REFERENCES embedding_configs(id),
    host TEXT NOT NULL, model TEXT NOT NULL,
    chunk_size INTEGER NOT NULL CHECK(chunk_size BETWEEN 64 AND 32000),
    overlap INTEGER NOT NULL CHECK(overlap >= 0 AND overlap < chunk_size),
    created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE knowledge_collection_documents (
    collection_id TEXT NOT NULL REFERENCES knowledge_collections(id) ON DELETE CASCADE,
    document_id TEXT NOT NULL REFERENCES knowledge_documents(id) ON DELETE CASCADE,
    index_id TEXT REFERENCES knowledge_indexes(id) ON DELETE SET NULL,
    PRIMARY KEY(collection_id, document_id)
);
CREATE INDEX collection_documents_document ON knowledge_collection_documents(document_id);
CREATE INDEX collection_documents_index ON knowledge_collection_documents(index_id);
CREATE TRIGGER collection_settings_immutable BEFORE UPDATE OF config_id, chunk_size, overlap
ON knowledge_collections WHEN NEW.config_id IS NOT OLD.config_id OR
    NEW.chunk_size IS NOT OLD.chunk_size OR NEW.overlap IS NOT OLD.overlap
BEGIN SELECT RAISE(ABORT, 'Copy the collection to change embedding settings'); END;
CREATE TRIGGER collection_index_insert BEFORE INSERT ON knowledge_collection_documents
WHEN NEW.index_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM knowledge_indexes i JOIN knowledge_collections c ON c.id=NEW.collection_id
    WHERE i.id=NEW.index_id AND i.document_id=NEW.document_id AND i.config_id=c.config_id
        AND i.chunk_size=c.chunk_size AND i.overlap=c.overlap)
BEGIN SELECT RAISE(ABORT, 'Incompatible collection index'); END;
CREATE TRIGGER collection_index_update BEFORE UPDATE ON knowledge_collection_documents
WHEN NEW.index_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM knowledge_indexes i JOIN knowledge_collections c ON c.id=NEW.collection_id
    WHERE i.id=NEW.index_id AND i.document_id=NEW.document_id AND i.config_id=c.config_id
        AND i.chunk_size=c.chunk_size AND i.overlap=c.overlap)
BEGIN SELECT RAISE(ABORT, 'Incompatible collection index'); END;
CREATE TRIGGER collection_index_settings BEFORE UPDATE OF document_id, config_id, chunk_size, overlap
ON knowledge_indexes WHEN EXISTS (
    SELECT 1 FROM knowledge_collection_documents m JOIN knowledge_collections c ON c.id=m.collection_id
    WHERE m.index_id=OLD.id AND (NEW.document_id IS NOT m.document_id OR NEW.config_id IS NOT c.config_id
        OR NEW.chunk_size IS NOT c.chunk_size OR NEW.overlap IS NOT c.overlap))
BEGIN SELECT RAISE(ABORT, 'Incompatible collection index'); END;
"""


class CollectionDatabase:
    def knowledge_collections(self):
        with self._get_conn() as conn:
            return [dict(r) for r in conn.execute('''SELECT c.*, e.digest, e.dimensions, e.preset,
                count(m.document_id) AS documents,
                coalesce(sum(i.status='complete' AND EXISTS
                    (SELECT 1 FROM knowledge_chunks WHERE index_id=i.id)), 0) AS ready
                FROM knowledge_collections c JOIN embedding_configs e ON e.id=c.config_id
                LEFT JOIN knowledge_collection_documents m ON m.collection_id=c.id
                LEFT JOIN knowledge_indexes i ON i.id=m.index_id
                GROUP BY c.id ORDER BY c.name COLLATE NOCASE, c.id''')]

    def knowledge_collection(self, id):
        with self._get_conn() as conn:
            row = conn.execute('SELECT * FROM knowledge_collections WHERE id=?', (id,)).fetchone()
            return dict(row) if row else None

    def collection_documents(self, id):
        with self._get_conn() as conn:
            return self._collection_documents(conn, id)

    def _collection_documents(self, conn, id):
        return [dict(r) for r in conn.execute('''SELECT d.id, d.title, d.filename,
            length(d.text) AS characters, m.index_id, i.status, i.error,
            (SELECT count(*) FROM knowledge_chunks WHERE index_id=i.id) AS chunks
            FROM knowledge_collection_documents m JOIN knowledge_documents d ON d.id=m.document_id
            LEFT JOIN knowledge_indexes i ON i.id=m.index_id WHERE m.collection_id=?
            ORDER BY d.title COLLATE NOCASE, d.id''', (id,))]

    def source_collections(self, document_id=None, index_id=None):
        with self._get_conn() as conn:
            return [dict(r) for r in conn.execute('''SELECT DISTINCT c.id, c.name
                FROM knowledge_collections c JOIN knowledge_collection_documents m ON m.collection_id=c.id
                WHERE (? IS NULL OR m.document_id=?) AND (? IS NULL OR m.index_id=?)
                ORDER BY c.name COLLATE NOCASE, c.id''', (document_id, document_id, index_id, index_id))]

    def ungrouped_document_ids(self):
        with self._get_conn() as conn:
            return {r[0] for r in conn.execute('''SELECT id FROM knowledge_documents WHERE NOT EXISTS
                (SELECT 1 FROM knowledge_collection_documents WHERE document_id=knowledge_documents.id)''')}

    def create_knowledge_collection(self, collection, document_ids=()):
        with self._get_conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            for document_id in dict.fromkeys(document_ids):
                if not conn.execute('SELECT 1 FROM knowledge_documents WHERE id=?', (document_id,)).fetchone():
                    raise ValueError(_('A document was deleted. Choose documents again.'))
            conn.execute('''INSERT INTO knowledge_collections
                (id, name, config_id, host, model, chunk_size, overlap, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''', tuple(collection[k] for k in
                ('id', 'name', 'config_id', 'host', 'model', 'chunk_size', 'overlap', 'created_at', 'updated_at')))
            conn.executemany('''INSERT INTO knowledge_collection_documents(collection_id, document_id)
                VALUES (?, ?)''', [(collection['id'], id) for id in dict.fromkeys(document_ids)])
            conn.commit()

    def rename_knowledge_collection(self, id, name):
        if not name.strip():
            raise ValueError(_('Enter a collection name.'))
        with self._get_conn() as conn:
            conn.execute('UPDATE knowledge_collections SET name=?, updated_at=? WHERE id=?', (name.strip(), time.time(), id))
            conn.commit()

    def update_collection_endpoint(self, id, host, model, digest):
        with self._get_conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('''SELECT e.digest FROM knowledge_collections c
                JOIN embedding_configs e ON e.id=c.config_id WHERE c.id=?''', (id,)).fetchone()
            if row is None or row['digest'] != digest:
                raise ValueError(_('Choose a model with the collection’s original digest.'))
            conn.execute('UPDATE knowledge_collections SET host=?, model=?, updated_at=? WHERE id=?',
                         (host, model, time.time(), id))
            conn.commit()

    def delete_knowledge_collection(self, id):
        with self._get_conn() as conn:
            conn.execute('DELETE FROM knowledge_collections WHERE id=?', (id,))
            conn.commit()

    def remove_collection_document(self, collection_id, document_id):
        with self._get_conn() as conn:
            conn.execute('DELETE FROM knowledge_collection_documents WHERE collection_id=? AND document_id=?',
                         (collection_id, document_id))
            conn.execute('UPDATE knowledge_collections SET updated_at=? WHERE id=?', (time.time(), collection_id))
            conn.commit()

    def prepare_collection_indexes(self, id, document_ids=(), queued_indexes=None):
        """Atomically add members, reuse builds, and reserve missing builds before scheduling."""
        with self._get_conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            collection = conn.execute('SELECT * FROM knowledge_collections WHERE id=?', (id,)).fetchone()
            if collection is None:
                raise ValueError(_('The collection was deleted.'))
            for document_id in dict.fromkeys(document_ids):
                if not conn.execute('SELECT 1 FROM knowledge_documents WHERE id=?', (document_id,)).fetchone():
                    raise ValueError(_('A document was deleted. Choose documents again.'))
                conn.execute('''INSERT OR IGNORE INTO knowledge_collection_documents(collection_id, document_id)
                    VALUES (?, ?)''', (id, document_id))
            # An ordinary document build may be queued but not have started its
            # first write yet. Register matching active jobs before choosing builds.
            for index in queued_indexes or ():
                if (index['config_id'], index['chunk_size'], index['overlap']) != (
                        collection['config_id'], collection['chunk_size'], collection['overlap']):
                    continue
                if not conn.execute('''SELECT 1 FROM knowledge_collection_documents
                    WHERE collection_id=? AND document_id=?''', (id, index['document_id'])).fetchone():
                    continue
                conn.execute('''INSERT OR IGNORE INTO knowledge_indexes
                    (id, document_id, config_id, host, model, chunk_size, overlap, status, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'indexing', ?)''', tuple(index[k] for k in
                    ('id', 'document_id', 'config_id', 'host', 'model', 'chunk_size', 'overlap', 'created_at')))
            jobs = []
            for member in self._collection_documents(conn, id):
                if queued_indexes is not None and member['status'] == 'indexing' and not any(
                        j['id'] == member['index_id'] for j in queued_indexes):
                    conn.execute("UPDATE knowledge_indexes SET status='interrupted' WHERE id=?", (member['index_id'],))
                    member['status'] = 'interrupted'
                if member['status'] in ('complete', 'indexing') and (member['status'] != 'complete' or member['chunks']):
                    continue
                match = conn.execute('''SELECT * FROM knowledge_indexes WHERE document_id=? AND config_id=?
                    AND chunk_size=? AND overlap=? AND ((status='indexing' AND
                        (? IS NULL OR id IN (SELECT value FROM json_each(?)))) OR
                        (status='complete' AND EXISTS (SELECT 1 FROM knowledge_chunks WHERE index_id=knowledge_indexes.id)))
                    ORDER BY (status='complete') DESC, created_at DESC, id DESC LIMIT 1''',
                    (member['id'], collection['config_id'], collection['chunk_size'], collection['overlap'],
                     None if queued_indexes is None else 1, json.dumps([j['id'] for j in queued_indexes or ()]))).fetchone()
                if match:
                    index_id = match['id']
                else:
                    index_id = member['index_id'] or str(uuid.uuid4())
                    index = dict(id=index_id, document_id=member['id'], config_id=collection['config_id'],
                                 host=collection['host'], model=collection['model'], chunk_size=collection['chunk_size'],
                                 overlap=collection['overlap'], created_at=time.time(), title=member['title'])
                    conn.execute('''INSERT INTO knowledge_indexes
                        (id, document_id, config_id, host, model, chunk_size, overlap, status, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, 'indexing', ?)
                        ON CONFLICT(id) DO UPDATE SET status='indexing', error=NULL, completed_at=NULL,
                            host=excluded.host, model=excluded.model''', tuple(index[k] for k in
                        ('id', 'document_id', 'config_id', 'host', 'model', 'chunk_size', 'overlap', 'created_at')))
                    jobs.append(index)
                conn.execute('''UPDATE knowledge_collection_documents SET index_id=?
                    WHERE collection_id=? AND document_id=?''', (index_id, id, member['id']))
            conn.execute('UPDATE knowledge_collections SET updated_at=? WHERE id=?', (time.time(), id))
            conn.commit()
            return jobs

    def resolve_collection_sources(self, conn, config_id, selection, collection_ids, check_cancel=lambda: None):
        selection = dict(selection)
        snapshot = []
        for id in sorted(set(collection_ids)):
            check_cancel()
            collection = conn.execute('SELECT * FROM knowledge_collections WHERE id=?', (id,)).fetchone()
            if collection is None:
                raise ValueError(_('A selected collection was deleted. Choose sources again.'))
            if collection['config_id'] != config_id:
                raise ValueError(_('Collection “{0}” uses a different embedding configuration.').format(collection['name']))
            members = self._collection_documents(conn, id)
            if not members:
                raise ValueError(_('Collection “{0}” is empty. Add documents or adjust sources.').format(collection['name']))
            missing = [m['title'] for m in members if m['status'] != 'complete' or not m['chunks']]
            if missing:
                raise ValueError(_('Collection “{0}” is not ready. Build missing embeddings or retry failed documents: {1}').format(
                    collection['name'], ', '.join(missing)))
            for member in members:
                selection[member['index_id']] = None
            snapshot.append(dict(id=id, name=collection['name'], members=[
                dict(document_id=m['id'], index_id=m['index_id']) for m in members]))
        return selection, snapshot

    def check_collection_sources(self, config_id, selection, collection_ids):
        with self._get_conn() as conn:
            conn.execute('BEGIN')
            self.resolve_collection_sources(conn, config_id, selection, collection_ids)
