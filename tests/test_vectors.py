"""Upgrade, transactional vector storage, and exact filtered retrieval regressions."""
import copy
import json
import math
import random
import sqlite3
import struct
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from src import database
from src.database import DatabaseManager, DatabaseUpgradeError
from src.knowledge import make_document, new_config, PRESETS
from src.vectors import distance_candidates, migrate_vectors, vector_blob, vector_table, vector_values


def add_index(db, id='index', vectors=None, config=None, status='complete'):
    vectors = vectors or [[1, 0], [0, 1]]
    config = config or new_config('qwen3-embedding:0.6b', 'digest')
    document = make_document(id, ''.join('Passage %03d. ' % n for n in range(len(vectors))))
    db.add_knowledge_document(document)
    db.add_embedding_config(config)
    db.begin_knowledge_index(dict(id=id, document_id=document['id'], config_id=config['id'],
                                 host='http://localhost:11434', model=config['model'], chunk_size=1600,
                                 overlap=200, created_at=0))
    chunks = [dict(id=id + '-%05d' % n, ordinal=n, start=n*13, end=(n+1)*13,
                   vector=vector_blob(v)) for n, v in enumerate(vectors)]
    db.save_embedding_batch(id, config['id'], len(vectors[0]), chunks)
    db.finish_knowledge_index(id, status)
    return document, config, chunks


def legacy_database(path, dimensions=2, raw=None):
    # Create the real pre-vec0 schema, rather than stripping a current schema.
    with patch.object(database, 'MIGRATIONS', database.MIGRATIONS[:5]):
        db = DatabaseManager(str(path))
    doc = make_document('Legacy source', 'The library opens at nine. Second saved passage.')
    config = new_config('old-embedding', 'old-digest', 'custom', query_prefix='Find: ')
    config['dimensions'] = dimensions
    db.add_knowledge_document(doc)
    db.add_embedding_config(config)
    for id, status in [('complete', 'complete'), ('partial', 'interrupted')]:
        db.begin_knowledge_index(dict(id=id, document_id=doc['id'], config_id=config['id'],
                                     host='http://old:11434', model=config['model'], chunk_size=1600,
                                     overlap=200, created_at=0))
        with db._get_conn() as conn:
            conn.execute('INSERT INTO knowledge_chunks VALUES (?, ?, 0, 0, 25, ?)',
                         (id+'-chunk', id, raw if raw is not None else vector_blob([1, 0])))
            conn.commit()
        db.finish_knowledge_index(id, status)
    options = {'knowledge': {'config_id': config['id'], 'selection': {'complete': ['complete-chunk']}},
               'tools_text': '[]', 'temperature': .2}
    db.create_chat('old', 'Keep chat', 1, 2, 'chat-model')
    db.update_chat('old', 'chat-model', options, None, None, 2)
    messages = [dict(role='assistant', content='Saved answer [S1]', response_metadata={'retrieval': {'hits': [{'text': doc['text']}]}}),
                dict(role='assistant', content='', tool_calls=[{'function': {'name': 'read_file', 'arguments': {'path': 'test'}}}]),
                dict(role='tool', content='saved result', tool_name='read_file')]
    db.save_messages('old', messages)
    return db, config, copy.deepcopy(db.get_chat('old'))


class VectorStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'data.db'

    def test_upgrade_preserves_vectors_snapshots_partial_indexes_and_wal_backup(self):
        old, config, chat = legacy_database(self.path)
        # Keep WAL data live so the backup must include more than the main file.
        wal = sqlite3.connect(self.path)
        self.addCleanup(wal.close)
        wal.execute('PRAGMA wal_autocheckpoint=0')
        wal.execute("INSERT INTO hosts VALUES ('wal-host', 'Saved in WAL', 'http://wal:11434', 0)")
        wal.commit()
        progress = []
        upgraded = DatabaseManager(str(self.path), progress=progress.append)
        self.assertEqual(upgraded.get_chat('old'), chat)
        self.assertEqual(upgraded.knowledge_vector('complete-chunk'), vector_blob([1, 0]))
        self.assertEqual(upgraded.knowledge_vector('partial-chunk'), vector_blob([1, 0]))
        self.assertEqual({i['status'] for i in upgraded.knowledge_indexes()}, {'complete', 'interrupted'})
        self.assertEqual(upgraded.embedding_config(config['id']), config)
        with upgraded._get_conn() as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 9)
            self.assertNotIn('vector', [r['name'] for r in conn.execute('PRAGMA table_info(knowledge_chunks)')])
            self.assertEqual(conn.execute(f'SELECT count(*) FROM {vector_table(config["id"])}').fetchone()[0], 2)
        with sqlite3.connect(upgraded.backup_path) as backup:
            self.assertEqual(backup.execute('PRAGMA user_version').fetchone()[0], 6)
            self.assertEqual(backup.execute("SELECT name FROM hosts WHERE id='wal-host'").fetchone()[0], 'Saved in WAL')
            self.assertEqual(backup.execute('SELECT vector FROM knowledge_chunks LIMIT 1').fetchone()[0], vector_blob([1, 0]))
        self.assertTrue(progress)
        self.assertIsNone(DatabaseManager(str(self.path)).backup_path)
        hits = upgraded.search_knowledge(config['id'], {'complete': None}, [1, 0])
        self.assertEqual([h['id'] for h in hits], ['complete-chunk'])

    def test_failed_upgrade_rolls_back_virtual_tables_and_old_column_then_retries(self):
        old, config, chat = legacy_database(self.path)
        def fail(conn, progress=None):
            migrate_vectors(conn)
            conn.execute('INSERT INTO nonexistent VALUES (1)')
        with patch.object(database, 'MIGRATIONS', database.MIGRATIONS[:5] + [fail]):
            with self.assertRaises(DatabaseUpgradeError) as caught:
                DatabaseManager(str(self.path))
        self.assertTrue(Path(caught.exception.backup_path).exists())
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 6)
            self.assertEqual(conn.execute('SELECT vector FROM knowledge_chunks LIMIT 1').fetchone()[0], vector_blob([1, 0]))
            self.assertEqual(conn.execute("SELECT name FROM sqlite_master WHERE name LIKE 'knowledge_vec_%'").fetchall(), [])
        self.assertEqual(DatabaseManager(str(self.path)).get_chat('old'), chat)

    def test_backup_failure_and_future_version_do_not_change_the_database(self):
        old, config, chat = legacy_database(self.path)
        with patch.object(database.os, 'open', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(DatabaseUpgradeError, 'disk full'):
                DatabaseManager(str(self.path))
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 6)
            conn.execute('PRAGMA user_version=99')
        with self.assertRaisesRegex(DatabaseUpgradeError, 'newer'):
            DatabaseManager(str(self.path))
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 99)
        self.assertEqual(list(self.path.parent.glob('*.bak')), [])

    def test_bad_legacy_vectors_and_dimension_limit_preserve_original_bytes(self):
        for label, dim, raw in [('large', 8193, struct.pack('<8193f', *([1] * 8193))),
                                ('nan', 2, struct.pack('<2f', float('nan'), 1)),
                                ('zero', 2, struct.pack('<2f', 0, 0)),
                                ('wrong-size', 2, b'bad')]:
            with self.subTest(label=label):
                path = self.path.parent / (label+'.db')
                legacy_database(path, dim, raw)
                with self.assertRaises(DatabaseUpgradeError):
                    DatabaseManager(str(path))
                with sqlite3.connect(path) as conn:
                    self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 6)
                    self.assertEqual(conn.execute('SELECT vector FROM knowledge_chunks LIMIT 1').fetchone()[0], raw)

    def test_batch_failure_and_parent_delete_failure_restore_both_stores(self):
        db = DatabaseManager(str(self.path))
        doc, config, chunks = add_index(db, status='indexing')
        altered = dict(chunks[0], vector=vector_blob([0, 1]))
        conflict = dict(chunks[1], id='different-id')  # Duplicate ordinal fails after the first write.
        with self.assertRaises(sqlite3.IntegrityError):
            db.save_embedding_batch('index', config['id'], 2, [altered, conflict])
        self.assertEqual(db.knowledge_vector(chunks[0]['id']), chunks[0]['vector'])
        self.assertIsNone(db.knowledge_vector('different-id'))
        original = db._get_conn
        @contextmanager
        def deny_parent_delete():
            with original() as conn:
                conn.set_authorizer(lambda action, table, *args: sqlite3.SQLITE_DENY
                                    if action == sqlite3.SQLITE_DELETE and table == 'knowledge_documents' else sqlite3.SQLITE_OK)
                yield conn
        with patch.object(db, '_get_conn', deny_parent_delete):
            with self.assertRaises(sqlite3.DatabaseError):
                db.delete_knowledge_document(doc['id'])
        self.assertEqual(db.knowledge_vector(chunks[0]['id']), chunks[0]['vector'])
        self.assertIsNotNone(db.knowledge_document(doc['id']))
        db.delete_knowledge_document(doc['id'])
        with db._get_conn() as conn:
            self.assertEqual(conn.execute(f'SELECT count(*) FROM {vector_table(config["id"])}').fetchone()[0], 0)
        self.assertIsNone(DatabaseManager(str(self.path)).knowledge_vector(chunks[0]['id']))

    def test_mixed_selection_ties_and_configuration_isolation(self):
        db = DatabaseManager(str(self.path))
        doc, config, chunks = add_index(db, 'whole', [[1, 0]] * 30)
        _, _, partial = add_index(db, 'partial', [[1, 0]] * 30)
        add_index(db, 'excluded', [[1, 0]] * 30)
        add_index(db, 'other-config', [[1, 0, 0]], new_config('other-model', 'other-digest'))
        chosen = [c['id'] for c in partial[:3]]
        expected = sorted([c['id'] for c in chunks] + chosen, reverse=True)[:6]
        hits = db.search_knowledge(config['id'], {'whole': None, 'partial': chosen}, [1, 0])
        self.assertEqual([h['id'] for h in hits], expected)
        # More than k excluded vectors are closer: filtering after global KNN would fail.
        _, _, weak = add_index(db, 'weak', [[.6, .8]])
        for chosen in (None, [weak[0]['id']]):
            filtered = db.search_knowledge(config['id'], {'weak': chosen}, [1, 0], count=1)
            self.assertEqual([h['id'] for h in filtered], [weak[0]['id']])
        for selection in ({'whole': None, 'excluded': ['missing']}, {'other-config': None}):
            with self.assertRaises(ValueError):
                db.search_knowledge(config['id'], selection, [1, 0])
        self.assertEqual(db.search_knowledge(config['id'], {'partial': [partial[0]['id']]}, [0, 1], minimum=.5), [])
        short = db.search_knowledge(config['id'], {'whole': None}, [1, 0], budget=4)
        self.assertEqual(len(short[0]['text']), 4)
        self.assertTrue(short[0]['truncated'])

    def test_random_ranking_matches_exact_cosine_and_cancellation(self):
        db = DatabaseManager(str(self.path))
        rng = random.Random(42)
        vectors = [[rng.uniform(-1, 1) for _ in range(7)] for n in range(200)]
        _, config, chunks = add_index(db, vectors=vectors)
        query = list(vector_values(vector_blob([rng.uniform(-1, 1) for _ in range(7)])))
        def cosine(chunk):
            values = vector_values(chunk['vector'])
            return math.fsum(a*b for a,b in zip(values,query)) / math.sqrt(math.fsum(a*a for a in values) * math.fsum(a*a for a in query))
        expected = sorted(chunks, key=lambda c: (cosine(c), c['id']), reverse=True)[:6]
        actual = db.search_knowledge(config['id'], {'index': None}, query)
        self.assertEqual([h['id'] for h in actual], [c['id'] for c in expected])
        for hit, chunk in zip(actual, expected):
            self.assertAlmostEqual(hit['score'], cosine(chunk), places=6)
        calls = []
        def cancel():
            calls.append(1)
            if len(calls) >= 3:
                raise RuntimeError('cancelled search')
        with self.assertRaisesRegex(RuntimeError, 'cancelled search'):
            db.search_knowledge(config['id'], {'index': [c['id'] for c in chunks]}, query, check_cancel=cancel)
        self.assertEqual(len(db.search_knowledge(config['id'], {'index': None}, query)), 6)

    def test_native_extension_disabled_after_loading_and_qwen_format(self):
        db = DatabaseManager(str(self.path))
        with db._get_conn() as conn:
            self.assertEqual(conn.execute('SELECT vec_version()').fetchone()[0], 'v0.1.9')
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("SELECT load_extension('/not-a-library')")
        config = new_config('qwen3-embedding:0.6b', 'qwen', 'qwen3', dimensions=256)
        self.assertEqual(config['document_prefix'], '')
        self.assertEqual(config['query_prefix'], PRESETS['qwen3'][1])
        self.assertTrue(config['query_prefix'].endswith('\nQuery:'))
        with self.assertRaises(ValueError):
            new_config('qwen3-embedding:0.6b', 'qwen', dimensions=8193)

    def test_distance_measures_rank_all_selected_vectors_and_apply_maximum(self):
        db = DatabaseManager(str(self.path))
        _, config, chunks = add_index(db, vectors=[[.8, math.sqrt(.18), math.sqrt(.18)], [.7, math.sqrt(.51), 0]])
        # A globally better neighbor must never leak into a selected source search.
        add_index(db, id='excluded', vectors=[[1, 0, 0]])
        selection, query = {'index': None}, [1, 0, 0]
        cosine = db.search_knowledge(config['id'], selection, query, count=1)
        euclidean = db.search_knowledge(config['id'], selection, query, count=1, metric='euclidean')
        manhattan = db.search_knowledge(config['id'], selection, query, count=1, metric='manhattan')
        self.assertEqual(cosine[0]['id'], chunks[0]['id'])
        self.assertEqual(euclidean[0]['id'], chunks[0]['id'])
        self.assertEqual(manhattan[0]['id'], chunks[1]['id'])
        self.assertAlmostEqual(euclidean[0]['score'], math.sqrt(.4), places=6)
        self.assertAlmostEqual(manhattan[0]['score'], .3 + math.sqrt(.51), places=6)
        for metric, maximum, expected in [('euclidean', .7, chunks[0]), ('manhattan', 1.03, chunks[1])]:
            hits = db.search_knowledge(config['id'], selection, query, metric=metric, maximum=maximum)
            self.assertEqual([h['id'] for h in hits], [expected['id']])
            self.assertEqual(db.search_knowledge(config['id'], selection, query, metric=metric, maximum=0), [])
            restricted = db.search_knowledge(config['id'], {'index': [chunks[1]['id']]}, query, metric=metric, budget=4)
            self.assertEqual(restricted[0]['id'], chunks[1]['id'])
            self.assertEqual(len(restricted[0]['text']), 4)

    def test_distance_search_matches_reference_ties_and_cancellation(self):
        db = DatabaseManager(str(self.path))
        rng = random.Random(192)
        vectors = [[rng.uniform(-1, 1) for _ in range(7)] for _ in range(200)]
        _, config, chunks = add_index(db, vectors=vectors)
        query = vector_values(vector_blob([.1, -.2, .7, .4, .3, .2, -.1]))
        for metric in ('euclidean', 'manhattan'):
            def distance(chunk):
                differences = [a-b for a, b in zip(vector_values(chunk['vector']), query)]
                return math.sqrt(math.fsum(d*d for d in differences)) if metric == 'euclidean' else math.fsum(abs(d) for d in differences)
            expected = sorted(chunks, key=lambda c: (-distance(c), c['id']), reverse=True)[:6]
            hits = db.search_knowledge(config['id'], {'index': None}, query, metric=metric)
            self.assertEqual([h['id'] for h in hits], [c['id'] for c in expected])
            for hit, chunk in zip(hits, expected):
                self.assertAlmostEqual(hit['score'], distance(chunk), places=6)
            calls = []
            def cancel():
                calls.append(1)
                if len(calls) >= 4:
                    raise RuntimeError('cancelled distance search')
            with self.assertRaisesRegex(RuntimeError, 'cancelled distance search'):
                db.search_knowledge(config['id'], {'index': None}, query, metric=metric, check_cancel=cancel)
            self.assertEqual(len(db.search_knowledge(config['id'], {'index': None}, query, metric=metric)), 6)
        _, tied_config, tied = add_index(db, id='ties', vectors=[[1, 0], [0, 1], [-1, 0]],
                                         config=new_config('other', 'other'))
        for metric in ('euclidean', 'manhattan'):
            actual = db.search_knowledge(tied_config['id'], {'ties': None}, [0, -1], count=1, metric=metric)
            self.assertEqual(actual[0]['id'], tied[2]['id'])

    def test_metric_and_threshold_validation_rejects_mismatched_units(self):
        db = DatabaseManager(str(self.path))
        _, config, chunks = add_index(db)
        invalid = [dict(metric='unknown'), dict(metric='cosine', maximum=1),
                   dict(metric='manhattan', minimum=.5), dict(metric='euclidean', maximum=-1),
                   dict(metric='euclidean', maximum=float('inf')), dict(metric='manhattan', maximum=float('nan')),
                   dict(metric='manhattan', maximum=True), dict(minimum='invalid')]
        for kwargs in invalid:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                db.search_knowledge(config['id'], {'index': None}, [1, 0], **kwargs)

    def test_distance_reader_handles_reused_slots_and_matches_public_api(self):
        db = DatabaseManager(str(self.path))
        add_index(db, id='removed')
        _, config, kept = add_index(db, id='kept')
        db.delete_knowledge_index('removed')
        _, _, added = add_index(db, id='added', vectors=[[-1, 0], [1, 1]])
        for metric in ('cosine', 'euclidean', 'manhattan'):
            with db._get_conn() as conn:
                conn.execute('BEGIN')
                fast = distance_candidates(conn, config['id'], 'index_id', ['kept', 'added'], vector_blob([1, 0]), 6, metric, lambda: None)
                class FutureVersion:
                    def execute(self, sql, params=()):
                        return conn.execute("SELECT 'future-version'") if sql == 'SELECT vec_version()' else conn.execute(sql, params)
                fallback = distance_candidates(FutureVersion(), config['id'], 'index_id', ['kept', 'added'],
                                               vector_blob([1, 0]), 6, metric, lambda: None)
                self.assertEqual(fast, [dict(r) for r in fallback])
                self.assertEqual({r['chunk_id'] for r in fast}, {c['id'] for c in kept + added})

    def test_distance_reader_detects_invalid_vector_slot_mapping(self):
        db = DatabaseManager(str(self.path))
        _, config, chunks = add_index(db)
        with db._get_conn() as conn:
            conn.execute(f'UPDATE {vector_table(config["id"])}_rowids SET chunk_offset=999 WHERE id=?', (chunks[0]['id'],))
            conn.commit()
        with self.assertRaisesRegex(ValueError, 'do not match their source'):
            db.search_knowledge(config['id'], {'index': None}, [1, 0], metric='manhattan')
        self.assertEqual(len(vector_values(vector_blob([1e300, 1e300]))), 2)
