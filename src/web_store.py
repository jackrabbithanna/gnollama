"""Web provenance and atomic replacement of documents and their vector indexes."""
import json
from gettext import gettext as _

MIGRATION = '''
CREATE TABLE knowledge_web_sources (
    document_id TEXT PRIMARY KEY REFERENCES knowledge_documents(id) ON DELETE CASCADE,
    source_url TEXT NOT NULL UNIQUE, final_url TEXT NOT NULL, fetched_at REAL NOT NULL,
    content_type TEXT NOT NULL, selector TEXT NOT NULL, extractor TEXT NOT NULL,
    edited INTEGER NOT NULL DEFAULT 0
);
'''
FIELDS = ('source_url', 'final_url', 'fetched_at', 'content_type', 'selector', 'extractor', 'edited')


class WebSourceDatabase:
    def _web_source(self, conn, document_id):
        # Older-schema fixtures and upgrade preflight must still be readable.
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='knowledge_web_sources'").fetchone():
            return None
        row = conn.execute('SELECT * FROM knowledge_web_sources WHERE document_id=?', (document_id,)).fetchone()
        return dict(row) if row else None

    def web_document(self, source_url):
        with self._get_conn() as conn:
            row = conn.execute('SELECT d.* FROM knowledge_documents d JOIN knowledge_web_sources w '
                               'ON w.document_id=d.id WHERE w.source_url=?', (source_url,)).fetchone()
            if row:
                result = dict(row)
                result['pages'] = json.loads(result['pages'])
                result['web_source'] = self._web_source(conn, result['id'])
                return result

    def save_web_document(self, document, source, collection_id, expected_id=None, expected_hash=None):
        with self._get_conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if not conn.execute('SELECT 1 FROM knowledge_collections WHERE id=?', (collection_id,)).fetchone():
                raise ValueError(_('The destination collection was deleted. Choose a collection again.'))
            old = conn.execute('SELECT d.* FROM knowledge_documents d JOIN knowledge_web_sources w '
                               'ON d.id=w.document_id WHERE w.source_url=?', (source['source_url'],)).fetchone()
            if ((old is None) != (expected_id is None) or
                    old is not None and (old['id'] != expected_id or old['content_hash'] != expected_hash)):
                raise ValueError(_('This document changed since the preview was opened. Fetch it again before saving.'))
            changed = old is not None and old['content_hash'] != document['content_hash']
            id = old['id'] if old else document['id']
            indexes = []
            if changed:
                indexes = [dict(r) for r in conn.execute('SELECT * FROM knowledge_indexes WHERE document_id=?', (id,))]
                for index in indexes:
                    self._delete_index_vectors(conn, index['id'])
                    conn.execute('DELETE FROM knowledge_chunks WHERE index_id=?', (index['id'],))
                conn.execute("UPDATE knowledge_indexes SET status='interrupted', error=?, completed_at=NULL WHERE document_id=?",
                             (_('Source content changed. Rebuild required.'), id))
            if old:
                conn.execute('UPDATE knowledge_documents SET title=?, filename=?, text=?, pages=?, content_hash=?, file_hash=? WHERE id=?',
                             (document['title'], document['filename'], document['text'], json.dumps(document['pages']),
                              document['content_hash'], document['file_hash'], id))
            else:
                conn.execute('INSERT INTO knowledge_documents VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                             (id, document['title'], document['filename'], document['text'], json.dumps(document['pages']),
                              document['content_hash'], document['file_hash'], document['created_at']))
            conn.execute('''INSERT INTO knowledge_web_sources VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(document_id) DO UPDATE SET final_url=excluded.final_url, fetched_at=excluded.fetched_at,
                content_type=excluded.content_type, selector=excluded.selector, extractor=excluded.extractor, edited=excluded.edited''',
                         (id,) + tuple(source[key] for key in FIELDS))
            conn.execute('INSERT OR IGNORE INTO knowledge_collection_documents(collection_id, document_id) VALUES (?, ?)',
                         (collection_id, id))
            collections = [r['collection_id'] for r in conn.execute(
                'SELECT collection_id FROM knowledge_collection_documents WHERE document_id=?', (id,))]
            conn.commit()
            return dict(id=id, changed=changed, indexes=indexes, collections=collections)
