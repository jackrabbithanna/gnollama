#!/usr/bin/env python3
"""Reproducible local stress fixture; never opens the user's database."""
import argparse
import base64
import gettext
import json
import platform
from pathlib import Path
import resource
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
gettext.install('gnollama')
from src.database import DatabaseManager
from src.knowledge import make_document, new_config
from src.vectors import vector_blob


def run(output, scale=1):
    results = dict(python=platform.python_version(), machine=platform.machine(), timings_ms={})
    def measure(name, fn):
        start = time.perf_counter()
        result = fn()
        results['timings_ms'][name] = round((time.perf_counter() - start) * 1000, 3)
        return result
    with tempfile.TemporaryDirectory(prefix='gnollama-benchmark-') as directory:
        db = DatabaseManager(str(Path(directory) / 'fixture.db'))
        count = 10000 * scale
        with db._get_conn() as conn:
            conn.executemany('INSERT INTO chats(id,title,created_at,updated_at) VALUES (?,?,0,?)',
                [(str(i), 'Conversation benchmark ' + str(i), i) for i in range(count)])
            docs = [make_document('Document ' + str(i), 'Passage ' + str(i) + '\n' + 'Reference text. ' * 20) for i in range(count)]
            conn.executemany('INSERT INTO knowledge_documents(id,title,filename,text,pages,content_hash,created_at) VALUES (?,?,?,?,?,?,0)',
                [(d['id'], d['title'], '', d['text'], '[]', d['content_hash']) for d in docs])
            conn.commit()
        image = base64.b64encode(b'attachment-fixture' * 4096).decode()
        messages = [dict(uid='m' + str(i), role='user' if i % 2 == 0 else 'assistant',
                         content='Message ' + str(i) + '\n' + 'Saved message text. ' * 100,
                         **({'images': [image]} if i % 10 == 0 else {})) for i in range(1000)]
        measure('seed_1000_messages_with_images', lambda: db.append_messages('0', messages, 0))
        page = measure('history_page_100', lambda: db.list_history(limit=100))
        assert len(page) == 100
        page = measure('messages_page_50', lambda: db.get_messages('0', limit=50))
        assert len(page) == 50
        measure('search_10000_conversations', lambda: db.list_history('benchmark 999', limit=100))
        page = measure('documents_page_100', lambda: db.document_page(limit=100))
        assert len(page) == 100
        with db._get_conn() as conn:
            conn.execute("CREATE TRIGGER bounded_write BEFORE UPDATE ON messages WHEN old.uid!='new' BEGIN SELECT RAISE(ABORT, 'old message rewritten'); END")
            conn.commit()
        measure('append_one_after_1000', lambda: db.append_messages('0', [dict(uid='new', role='assistant', content='delta')], 1000))
        config = new_config('recorded-fixture', 'fixture-v1', dimensions=8)
        db.add_embedding_config(config)
        db.begin_knowledge_index(dict(id='vectors', document_id=docs[0]['id'], config_id=config['id'],
            host='http://localhost:11434', model=config['model'], chunk_size=1600, overlap=0, created_at=0))
        for offset in range(0, 50000 * scale, 1000):
            chunks = [dict(id='v' + str(i), ordinal=i, start=0, end=100,
                vector=vector_blob([1, i / (50000 * scale), .1, .2, .3, .4, .5, .6])) for i in range(offset, offset + 1000)]
            db.save_embedding_batch('vectors', config['id'], 8, chunks)
        db.finish_knowledge_index('vectors', 'complete')
        measure('search_50000_vectors', lambda: db.search_knowledge(config['id'], {'vectors': None}, [1, 0, .1, .2, .3, .4, .5, .6]))
        results['fixtures'] = dict(conversations=count, documents=count, messages=1001, vectors=50000 * scale)
        results['peak_rss_kib'] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        results['database_bytes'] = Path(db.db_path).stat().st_size
    Path(output).write_text(json.dumps(results, indent=2) + '\n')
    print(json.dumps(results, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default='workspace-benchmark.json')
    parser.add_argument('--scale', type=int, default=1)
    args = parser.parse_args()
    run(args.output, args.scale)
