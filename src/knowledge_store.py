"""SQLite persistence for immutable sources and independently replaceable indexes."""
import json
import math
import sqlite3
import time
from gettext import gettext as _
from .vectors import create_vector_table, table_exists, vector_blob, vector_table, vector_values, validate_dimensions, validate_search_metric, distance_candidates
from .collections_store import CollectionDatabase
from .web_store import WebSourceDatabase


MIGRATION = """
CREATE TABLE knowledge_documents (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, filename TEXT NOT NULL,
    text TEXT NOT NULL, pages TEXT NOT NULL, content_hash TEXT NOT NULL,
    file_hash TEXT, created_at REAL NOT NULL
);
CREATE INDEX knowledge_content_hash ON knowledge_documents(content_hash);
CREATE TABLE embedding_configs (
    id TEXT PRIMARY KEY, model TEXT NOT NULL, digest TEXT NOT NULL,
    dimensions INTEGER, requested_dimensions INTEGER, preset TEXT NOT NULL,
    document_prefix TEXT NOT NULL, query_prefix TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE knowledge_indexes (
    id TEXT PRIMARY KEY, document_id TEXT NOT NULL, config_id TEXT NOT NULL,
    host TEXT NOT NULL, model TEXT NOT NULL, chunk_size INTEGER NOT NULL, overlap INTEGER NOT NULL,
    status TEXT NOT NULL, error TEXT, created_at REAL NOT NULL, completed_at REAL,
    FOREIGN KEY(document_id) REFERENCES knowledge_documents(id) ON DELETE CASCADE,
    FOREIGN KEY(config_id) REFERENCES embedding_configs(id)
);
CREATE INDEX knowledge_indexes_document ON knowledge_indexes(document_id);
CREATE INDEX knowledge_indexes_config ON knowledge_indexes(config_id);
CREATE TABLE knowledge_chunks (
    id TEXT PRIMARY KEY, index_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
    start INTEGER NOT NULL, end INTEGER NOT NULL, vector BLOB NOT NULL,
    FOREIGN KEY(index_id) REFERENCES knowledge_indexes(id) ON DELETE CASCADE,
    UNIQUE(index_id, ordinal)
);
CREATE INDEX knowledge_chunks_index ON knowledge_chunks(index_id);
"""


class KnowledgeDatabase(WebSourceDatabase, CollectionDatabase):
    def import_collection_document(self, document, collection_id):
        """Save source and membership together, reusing an identical library source."""
        with self._get_conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if not conn.execute('SELECT 1 FROM knowledge_collections WHERE id=?', (collection_id,)).fetchone():
                raise ValueError(_('The destination collection was deleted.'))
            existing = conn.execute('SELECT id FROM knowledge_documents WHERE content_hash=? ORDER BY created_at, id LIMIT 1',
                                    (document['content_hash'],)).fetchone()
            id = existing['id'] if existing else document['id']
            if existing is None:
                conn.execute('INSERT INTO knowledge_documents (id, title, filename, text, pages, content_hash, file_hash, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                             (id, document['title'], document['filename'], document['text'], json.dumps(document['pages']),
                              document['content_hash'], document['file_hash'], document['created_at']))
            conn.execute('INSERT OR IGNORE INTO knowledge_collection_documents (collection_id, document_id) VALUES (?, ?)',
                         (collection_id, id))
            conn.commit()
            return id

    def _delete_index_vectors(self, conn, index_id):
        index = conn.execute('SELECT config_id FROM knowledge_indexes WHERE id=?', (index_id,)).fetchone()
        if index is not None and table_exists(conn, index['config_id']):
            conn.execute(f'DELETE FROM {vector_table(index["config_id"])} WHERE index_id=?', (index_id,))

    def knowledge_documents(self):
        with self._get_conn() as conn:
            return [dict(r) for r in conn.execute('''SELECT id, title, filename, content_hash,
                created_at, length(text) AS characters FROM knowledge_documents ORDER BY created_at DESC''')]

    def knowledge_document(self, id):
        with self._get_conn() as conn:
            row = conn.execute('SELECT * FROM knowledge_documents WHERE id = ?', (id,)).fetchone()
            if row:
                result = dict(row)
                result['pages'] = json.loads(result['pages'])
                result['web_source'] = self._web_source(conn, id)
                return result

    def add_knowledge_document(self, document):
        with self._get_conn() as conn:
            conn.execute('''INSERT OR IGNORE INTO knowledge_documents
                (id, title, filename, text, pages, content_hash, file_hash, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                tuple(document[k] if k != 'pages' else json.dumps(document[k]) for k in
                      ('id', 'title', 'filename', 'text', 'pages', 'content_hash', 'file_hash', 'created_at')))
            conn.commit()

    def rename_knowledge_document(self, id, title):
        with self._get_conn() as conn:
            conn.execute('UPDATE knowledge_documents SET title = ? WHERE id = ?', (title, id))
            conn.commit()

    def delete_knowledge_document(self, id):
        with self._get_conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            for row in conn.execute('SELECT id FROM knowledge_indexes WHERE document_id=?', (id,)).fetchall():
                self._delete_index_vectors(conn, row['id'])
            conn.execute('DELETE FROM knowledge_documents WHERE id = ?', (id,))
            conn.commit()

    def embedding_configs(self):
        with self._get_conn() as conn:
            return [dict(r) for r in conn.execute('SELECT * FROM embedding_configs ORDER BY created_at DESC')]

    def embedding_config(self, id):
        with self._get_conn() as conn:
            row = conn.execute('SELECT * FROM embedding_configs WHERE id = ?', (id,)).fetchone()
            return dict(row) if row else None

    def add_embedding_config(self, config):
        with self._get_conn() as conn:
            conn.execute('''INSERT OR IGNORE INTO embedding_configs
                (id, model, digest, dimensions, requested_dimensions, preset, document_prefix, query_prefix, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''', tuple(config[k] for k in
                ('id', 'model', 'digest', 'dimensions', 'requested_dimensions', 'preset',
                 'document_prefix', 'query_prefix', 'created_at')))
            conn.commit()

    def knowledge_indexes(self, document_id=None, config_id=None):
        with self._get_conn() as conn:
            return [dict(r) for r in conn.execute('''SELECT i.*, d.title, c.digest,
                c.dimensions, c.preset, (SELECT count(*) FROM knowledge_chunks WHERE index_id=i.id) AS chunks
                FROM knowledge_indexes i JOIN knowledge_documents d ON d.id=i.document_id
                JOIN embedding_configs c ON c.id=i.config_id
                WHERE (? IS NULL OR i.document_id=?) AND (? IS NULL OR i.config_id=?)
                ORDER BY i.created_at DESC''', (document_id, document_id, config_id, config_id))]

    def begin_knowledge_index(self, index, require_existing=False):
        with self._get_conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            # Retrying starts afresh, retaining other complete indexes.
            existing = conn.execute('SELECT config_id, document_id FROM knowledge_indexes WHERE id=?', (index['id'],)).fetchone()
            if require_existing and existing is None:
                raise ValueError(_('The reserved embedding index was deleted.'))
            if existing and (existing['config_id'] != index['config_id'] or existing['document_id'] != index['document_id']):
                raise ValueError(_('An index cannot change its document or embedding configuration.'))
            self._delete_index_vectors(conn, index['id'])
            conn.execute('DELETE FROM knowledge_chunks WHERE index_id=?', (index['id'],))
            conn.execute('''INSERT INTO knowledge_indexes
                (id, document_id, config_id, host, model, chunk_size, overlap, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'indexing', ?)
                ON CONFLICT(id) DO UPDATE SET status='indexing', error=NULL, completed_at=NULL,
                    host=excluded.host, model=excluded.model''', tuple(index[k] for k in
                    ('id', 'document_id', 'config_id', 'host', 'model', 'chunk_size', 'overlap', 'created_at')))
            conn.commit()

    def save_embedding_batch(self, index_id, config_id, dimensions, chunks):
        validate_dimensions(dimensions)
        for chunk in chunks:
            if len(vector_values(chunk['vector'])) != dimensions:
                raise ValueError(_('Embedding dimensions do not match the batch.'))
        with self._get_conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            index = conn.execute("SELECT config_id FROM knowledge_indexes WHERE id=? AND status='indexing'", (index_id,)).fetchone()
            if index is None:
                return  # A deleted index must never reappear through a delayed write.
            if index['config_id'] != config_id:
                raise ValueError(_('Embedding configuration does not match the index.'))
            row = conn.execute('SELECT dimensions FROM embedding_configs WHERE id=?', (config_id,)).fetchone()
            if row['dimensions'] is not None and row['dimensions'] != dimensions:
                raise ValueError(_('Embedding dimensions changed during indexing'))
            conn.execute('UPDATE embedding_configs SET dimensions=? WHERE id=?', (dimensions, config_id))
            name = create_vector_table(conn, config_id, dimensions)
            for chunk in chunks:
                old = conn.execute('SELECT index_id FROM knowledge_chunks WHERE id=?', (chunk['id'],)).fetchone()
                if old and old['index_id'] != index_id:
                    raise ValueError(_('A chunk cannot move between indexes.'))
                conn.execute(f'DELETE FROM {name} WHERE chunk_id=?', (chunk['id'],))
                conn.execute('''INSERT INTO knowledge_chunks(id, index_id, ordinal, start, end) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET ordinal=excluded.ordinal, start=excluded.start, end=excluded.end''',
                    (chunk['id'], index_id, chunk['ordinal'], chunk['start'], chunk['end']))
                conn.execute(f'INSERT INTO {name}(chunk_id, index_id, embedding) VALUES (?, ?, ?)',
                             (chunk['id'], index_id, chunk['vector']))
            conn.commit()

    def finish_knowledge_index(self, id, status, error=None):
        with self._get_conn() as conn:
            conn.execute('UPDATE knowledge_indexes SET status=?, error=?, completed_at=? WHERE id=?',
                         (status, error, time.time() if status == 'complete' else None, id))
            conn.commit()

    def interrupt_knowledge_indexes(self):
        with self._get_conn() as conn:
            conn.execute("UPDATE knowledge_indexes SET status='interrupted' WHERE status='indexing'")
            conn.commit()

    def delete_knowledge_index(self, id):
        with self._get_conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            self._delete_index_vectors(conn, id)
            conn.execute('DELETE FROM knowledge_indexes WHERE id=?', (id,))
            conn.commit()

    def model_embedding_usage(self, digest):
        with self._get_conn() as conn:
            row = conn.execute('''SELECT count(DISTINCT i.document_id) AS documents,
                count(DISTINCT i.id) AS indexes, count(k.id) AS vectors
                FROM knowledge_indexes i JOIN embedding_configs c ON c.id=i.config_id
                LEFT JOIN knowledge_chunks k ON k.index_id=i.id WHERE c.digest=?''', (digest,)).fetchone()
            result = dict(row)
            result['collections'] = conn.execute('''SELECT count(*) FROM knowledge_collections c
                JOIN embedding_configs e ON e.id=c.config_id WHERE e.digest=?''', (digest,)).fetchone()[0]
            return result

    def delete_model_embeddings(self, digest):
        with self._get_conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            for row in conn.execute('''SELECT i.id FROM knowledge_indexes i JOIN embedding_configs c
                ON c.id=i.config_id WHERE c.digest=?''', (digest,)).fetchall():
                self._delete_index_vectors(conn, row['id'])
            conn.execute('''DELETE FROM knowledge_indexes WHERE config_id IN
                (SELECT id FROM embedding_configs WHERE digest=?)''', (digest,))
            conn.commit()

    def knowledge_chunk_page(self, index_id, offset=0, limit=100):
        with self._get_conn() as conn:
            return [dict(r) for r in conn.execute('''SELECT k.id, k.ordinal, k.start, k.end,
                substr(d.text, k.start+1, k.end-k.start) AS text FROM knowledge_chunks k
                JOIN knowledge_indexes i ON i.id=k.index_id JOIN knowledge_documents d ON d.id=i.document_id
                WHERE k.index_id=? ORDER BY k.ordinal LIMIT ? OFFSET ?''', (index_id, limit, offset))]

    def knowledge_vector(self, chunk_id):
        with self._get_conn() as conn:
            index = conn.execute('''SELECT i.config_id FROM knowledge_chunks k
                JOIN knowledge_indexes i ON i.id=k.index_id WHERE k.id=?''', (chunk_id,)).fetchone()
            if index is None:
                return None
            row = conn.execute(f'SELECT embedding FROM {vector_table(index["config_id"])} WHERE chunk_id=?', (chunk_id,)).fetchone()
            return row['embedding'] if row else None

    def knowledge_chunk_ids(self, index_id):
        with self._get_conn() as conn:
            return {r['id'] for r in conn.execute('SELECT id FROM knowledge_chunks WHERE index_id=?', (index_id,))}

    def search_knowledge(self, config_id, selection, vector, count=6, budget=8000, minimum=None, check_cancel=lambda: None,
                         collection_ids=(), source_snapshot=None, metric='cosine', maximum=None):
        """Validate sources, then rank selected vectors within one read snapshot."""
        if not selection and not collection_ids:
            raise ValueError(_('Select at least one indexed document or chunk.'))
        validate_search_metric(metric, minimum, maximum)
        if not 1 <= count <= 20 or budget < 1:
            raise ValueError(_('Invalid search limits.'))
        check_cancel()
        query = vector_blob(vector)
        name = vector_table(config_id)
        cancelled = []

        def progress():
            try:
                check_cancel()
                return 0
            except Exception as exc:
                cancelled.append(exc)
                return 1

        with self._get_conn() as conn:
            conn.set_progress_handler(progress, 1000)
            try:
                conn.execute('BEGIN')
                selection, collections = self.resolve_collection_sources(conn, config_id, selection, collection_ids, check_cancel)
                config = conn.execute('SELECT dimensions FROM embedding_configs WHERE id=?', (config_id,)).fetchone()
                if config is None or config['dimensions'] != len(query) // 4:
                    raise ValueError(_('Query dimensions differ from the stored vectors. Rebuild the index.'))
                indexes, whole, partial = {}, [], []
                for index_id, chosen in selection.items():
                    check_cancel()
                    index = conn.execute('''SELECT i.*, d.title, d.pages FROM knowledge_indexes i
                        JOIN knowledge_documents d ON d.id=i.document_id WHERE i.id=?''', (index_id,)).fetchone()
                    if index is None or index['status'] != 'complete' or index['config_id'] != config_id:
                        raise ValueError(_('A selected index is missing, incomplete, or incompatible. Choose sources again.'))
                    indexes[index_id] = index
                    if chosen is None:
                        found = conn.execute('SELECT 1 FROM knowledge_chunks WHERE index_id=? LIMIT 1', (index_id,)).fetchone()
                        if not found:
                            raise ValueError(_('Selected chunks are missing. Choose sources again.'))
                        whole.append(index_id)
                    else:
                        wanted = set(chosen)
                        if not wanted or any(not isinstance(id, str) for id in wanted):
                            raise ValueError(_('A selected document has no selected chunks.'))
                        found = conn.execute('''SELECT count(*) FROM knowledge_chunks WHERE index_id=?
                            AND id IN (SELECT value FROM json_each(?))''', (index_id, json.dumps(sorted(wanted)))).fetchone()[0]
                        if found != len(wanted):
                            raise ValueError(_('Selected chunks are missing. Choose sources again.'))
                        partial.extend(wanted)
                if not table_exists(conn, config_id):
                    raise ValueError(_('Stored vectors are missing. Rebuild this index.'))

                candidates = []
                # vec0 handles each IN constraint during KNN, rather than filtering a global top-k.
                for column, ids in (('index_id', whole), ('chunk_id', partial)):
                    if not ids:
                        continue
                    check_cancel()
                    encoded = json.dumps(sorted(ids))
                    where = f'{column} IN (SELECT value FROM json_each(?))'
                    if metric == 'cosine':
                        rows = conn.execute(f'''SELECT chunk_id, distance FROM {name}
                            WHERE embedding MATCH ? AND k=? AND {where}''', (query, count + 1, encoded)).fetchall()
                    else:
                        # Rank every selected vector using the requested metric.
                        # Re-ranking cosine's top-k would miss valid L1 neighbors.
                        rows = distance_candidates(conn, config_id, column, ids, query, count, metric, check_cancel)
                    if not rows:
                        raise ValueError(_('Stored vectors are missing. Rebuild this index.'))
                    if any(r['distance'] is None or not math.isfinite(r['distance']) for r in rows):
                        raise ValueError(_('Stored vectors are damaged. Rebuild this index.'))
                    rows = sorted(rows, key=lambda r: (-r['distance'], r['chunk_id']), reverse=True)
                    if metric == 'cosine' and len(rows) > count and rows[count - 1]['distance'] == rows[count]['distance']:
                        # vec0's internal tie order is not our public chunk-ID order. Resolve
                        # boundary ties exactly, including tied rows outside the KNN result.
                        rows = distance_candidates(conn, config_id, column, ids, query, count, metric, check_cancel)
                    candidates.extend(rows[:count])
                check_cancel()
                hits = []
                for hit in sorted(candidates, key=lambda r: (-r['distance'], r['chunk_id']), reverse=True)[:count]:
                    check_cancel()
                    score = 1 - hit['distance'] if metric == 'cosine' else hit['distance']
                    if (minimum is not None and score < minimum) or (maximum is not None and score > maximum):
                        continue
                    if budget <= 0:
                        break
                    row = conn.execute('SELECT * FROM knowledge_chunks WHERE id=?', (hit['chunk_id'],)).fetchone()
                    if row is None or row['index_id'] not in indexes:
                        raise ValueError(_('Stored vectors do not match their source. Rebuild this index.'))
                    index = indexes[row['index_id']]
                    length = min(row['end'] - row['start'], budget)
                    text = conn.execute('SELECT substr(text, ?, ?) FROM knowledge_documents WHERE id=?',
                                        (row['start'] + 1, length, index['document_id'])).fetchone()[0]
                    pages = [p['page'] for p in json.loads(index['pages'])
                             if p['end'] > row['start'] and p['start'] < row['start'] + length]
                    hits.append({'id': row['id'], 'index_id': index['id'], 'document_id': index['document_id'],
                                 'title': index['title'], 'ordinal': row['ordinal'], 'start': row['start'],
                                 'end': row['start'] + length, 'pages': pages, 'score': score, 'text': text,
                                 'truncated': length < row['end'] - row['start']})
                    source = self._web_source(conn, index['document_id'])
                    if source:
                        hits[-1]['web_source'] = source
                    budget -= length
                check_cancel()
                if source_snapshot is not None:
                    source_snapshot.update(collections=collections, selection=selection, metric=metric)
                return hits
            except sqlite3.Error as exc:
                if cancelled:
                    raise cancelled[0] from exc
                raise ValueError(_('Knowledge search failed: {0}').format(exc)) from exc
            finally:
                conn.set_progress_handler(None, 0)
