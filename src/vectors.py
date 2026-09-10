"""Portable float32 serialization and the sqlite-vec storage contract."""
import hashlib
import heapq
import json
import math
import struct
from gettext import gettext as _


MAX_DIMENSIONS = 8192
SEARCH_METRICS = ('cosine', 'euclidean', 'manhattan')


def validate_search_metric(metric, minimum=None, maximum=None):
    if metric not in SEARCH_METRICS:
        raise ValueError(_('Choose a supported similarity measure.'))
    if metric == 'cosine':
        if maximum is not None:
            raise ValueError(_('Cosine similarity uses a minimum similarity, not a maximum distance.'))
        if minimum is not None and (type(minimum) not in (int, float) or not math.isfinite(minimum) or not -1 <= minimum <= 1):
            raise ValueError(_('Minimum cosine similarity must be between -1 and 1, or blank.'))
    else:
        if minimum is not None:
            raise ValueError(_('Distance measures use a maximum distance. Clear the minimum similarity.'))
        if maximum is not None and (type(maximum) not in (int, float) or not math.isfinite(maximum) or maximum < 0):
            raise ValueError(_('Maximum distance must be a finite, nonnegative number, or blank.'))


def validate_dimensions(dimensions):
    if type(dimensions) is not int or not 1 <= dimensions <= MAX_DIMENSIONS:
        raise ValueError(_('Vector dimensions must be between 1 and 8,192. Choose a supported dimension or embedding model.'))


def vector_values(raw):
    if not isinstance(raw, bytes) or not raw or len(raw) % 4:
        raise ValueError(_('Stored vector data is damaged. Rebuild this index.'))
    values = struct.unpack('<%df' % (len(raw) // 4), raw)
    if not all(math.isfinite(v) for v in values) or not any(values):
        raise ValueError(_('Stored vectors must be finite and nonzero. Rebuild this index.'))
    return values


def vector_blob(vector):
    try:
        values = [float(v) for v in vector]
        validate_dimensions(len(values))
        scale = max(abs(v) for v in values)
        if not scale or not all(math.isfinite(v) for v in values):
            raise ValueError()
        scaled = [v / scale for v in values]
        norm = math.sqrt(math.fsum(v * v for v in scaled))
        return struct.pack('<%df' % len(values), *(v / norm for v in scaled))
    except (TypeError, OverflowError, ValueError) as exc:
        raise ValueError(_('Invalid embedding vector; expected 1–8,192 finite, nonzero dimensions.')) from exc


def load_extension(conn):
    try:
        import sqlite_vec
        conn.enable_load_extension(True)
        try:
            sqlite_vec.load(conn)
        finally:
            conn.enable_load_extension(False)
    except Exception as exc:
        raise RuntimeError(_('Could not load sqlite-vec. Reinstall Gnollama or install its sqlite-vec dependency.')) from exc


def vector_table(config_id):
    # Never interpolate a database value as an SQL identifier.
    return 'knowledge_vec_' + hashlib.sha256(config_id.encode('utf-8')).hexdigest()


def table_exists(conn, config_id):
    return conn.execute('SELECT 1 FROM sqlite_master WHERE type=\'table\' AND name=?',
                        (vector_table(config_id),)).fetchone() is not None


def distance_candidates(conn, config_id, column, ids, query, count, metric, check_cancel):
    """Exact ranking without reopening vec0's large storage BLOB for every row.

    sqlite-vec 0.1.9 is pinned by the Flatpak and Meson. Its read-only shadow
    layout lets us keep one incremental BLOB handle per block; scalar distance
    arithmetic still runs in sqlite-vec. Other versions use the public API.
    The caller owns the read transaction. Nothing is cached across searches.
    """
    function = {'cosine': 'vec_distance_cosine', 'euclidean': 'vec_distance_L2', 'manhattan': 'vec_distance_L1'}[metric]
    name = vector_table(config_id)
    if column not in ('index_id', 'chunk_id'):
        raise ValueError('Invalid vector selection column')
    encoded = json.dumps(sorted(ids))
    if conn.execute('SELECT vec_version()').fetchone()[0] != 'v0.1.9':
        return conn.execute(f'''SELECT chunk_id, {function}(embedding, ?) AS distance FROM {name}
            WHERE {column} IN (SELECT value FROM json_each(?))
            ORDER BY distance, chunk_id DESC LIMIT ?''', (query, encoded, count)).fetchall()

    field = 'k.index_id' if column == 'index_id' else 'k.id'
    cursor = conn.execute(f'''SELECT k.id, r.rowid AS vector_rowid, r.chunk_id AS block_id, r.chunk_offset
        FROM knowledge_chunks k LEFT JOIN {name}_rowids r ON r.id=k.id
        WHERE {field} IN (SELECT value FROM json_each(?)) ORDER BY r.chunk_id, r.chunk_offset''', (encoded,))
    block_id = blob = None
    size = len(query)
    heap = []
    try:
        for row in cursor:
            check_cancel()
            if row['block_id'] is None or row['chunk_offset'] is None:
                raise ValueError(_('Stored vectors are missing. Rebuild this index.'))
            if row['block_id'] != block_id:
                if blob is not None:
                    blob.close()
                    blob = None
                block_id = row['block_id']
                block = conn.execute(f'SELECT size, validity, rowids FROM {name}_chunks WHERE chunk_id=?', (block_id,)).fetchone()
                if block is None:
                    raise ValueError(_('Stored vectors are missing. Rebuild this index.'))
                slots, validity, rowids = block['size'], block['validity'], block['rowids']
                blob = conn.blobopen(name + '_vector_chunks00', 'vectors', block_id, readonly=True)
                if slots < 1 or len(blob) != slots * size or len(validity) * 8 != slots or len(rowids) != slots * 8:
                    raise ValueError(_('Stored vector blocks are damaged. Rebuild this index.'))
            offset = row['chunk_offset']
            if not 0 <= offset < slots or not (validity[offset // 8] & (1 << (offset % 8))) or \
                    struct.unpack_from('<q', rowids, offset * 8)[0] != row['vector_rowid']:
                raise ValueError(_('Stored vectors do not match their source. Rebuild this index.'))
            blob.seek(offset * size)
            raw = blob.read(size)
            distance = conn.execute(f'SELECT {function}(?, ?)', (raw, query)).fetchone()[0]
            if distance is None or not math.isfinite(distance):
                raise ValueError(_('Stored vectors are damaged. Rebuild this index.'))
            candidate = (-distance, row['id'])
            if len(heap) < count:
                heapq.heappush(heap, candidate)
            else:
                heapq.heappushpop(heap, candidate)
        return [dict(chunk_id=id, distance=-distance) for distance, id in sorted(heap, reverse=True)]
    finally:
        if blob is not None:
            blob.close()
        cursor.close()


def create_vector_table(conn, config_id, dimensions):
    validate_dimensions(dimensions)
    name = vector_table(config_id)
    conn.execute(f'''CREATE VIRTUAL TABLE IF NOT EXISTS {name} USING vec0(
        chunk_id TEXT PRIMARY KEY, embedding float[{dimensions}] distance_metric=cosine,
        index_id TEXT)''')
    return name


def preflight_legacy_vectors(conn):
    """Check limits without changing the existing database or requiring Ollama."""
    for row in conn.execute('''SELECT DISTINCT c.dimensions FROM embedding_configs c
        JOIN knowledge_indexes i ON i.config_id=c.id JOIN knowledge_chunks k ON k.index_id=i.id'''):
        validate_dimensions(row[0])


def migrate_vectors(conn, progress=None):
    """Version 7; the caller owns the transaction, including virtual-table DDL."""
    preflight_legacy_vectors(conn)
    progress = progress or (lambda message: None)
    expected = conn.execute('SELECT count(*) FROM knowledge_chunks').fetchone()[0]
    copied = 0
    if conn.execute('PRAGMA foreign_key_check').fetchone():
        raise ValueError(_('The database contains broken references. Restore a database backup before upgrading.'))
    for config in conn.execute('SELECT id, dimensions FROM embedding_configs').fetchall():
        cursor = conn.execute('''SELECT k.id, k.index_id, k.vector FROM knowledge_chunks k
            JOIN knowledge_indexes i ON i.id=k.index_id WHERE i.config_id=?''', (config['id'],))
        name = None
        total = 0
        while rows := cursor.fetchmany(256):
            if name is None:
                name = create_vector_table(conn, config['id'], config['dimensions'])
            for row in rows:
                if len(vector_values(row['vector'])) != config['dimensions']:
                    raise ValueError(_('Stored vector dimensions do not match their configuration. Rebuild the index before upgrading.'))
            conn.executemany(f'INSERT INTO {name}(chunk_id, index_id, embedding) VALUES (?, ?, ?)',
                             [(r['id'], r['index_id'], r['vector']) for r in rows])
            total += len(rows)
            copied += len(rows)
            progress(_('Migrating vectors: {0} of {1}').format(copied, expected))
        if name is not None:
            progress(_('Verifying migrated vectors…'))
            if conn.execute(f'SELECT count(*) FROM {name}').fetchone()[0] != total:
                raise ValueError(_('Vector migration verification failed.'))
            # Verify every ID, document index, and float32 byte before dropping the old copy.
            if conn.execute(f'''SELECT 1 FROM knowledge_chunks k
                JOIN knowledge_indexes i ON i.id=k.index_id
                LEFT JOIN {name} v ON v.chunk_id=k.id
                WHERE i.config_id=? AND (v.chunk_id IS NULL OR v.index_id IS NOT k.index_id
                    OR v.embedding IS NOT k.vector) LIMIT 1''', (config['id'],)).fetchone():
                raise ValueError(_('Vector migration verification failed.'))
    progress(_('Finishing the database upgrade…'))
    conn.execute('ALTER TABLE knowledge_chunks DROP COLUMN vector')
