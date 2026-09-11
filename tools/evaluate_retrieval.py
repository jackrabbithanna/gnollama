#!/usr/bin/env python3
"""Evaluate recorded retrieval, or explicitly opt into an Ollama host/model."""
import argparse
import gettext
import json
import math
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
gettext.install('gnollama')
from src.database import DatabaseManager
from src.knowledge import make_document, new_config, augmented_messages, model_identity
from src.vectors import vector_blob
from src import ollama


def evaluate(host=None, model=None, answer_model=None):
    fixture = json.loads((ROOT / 'tests/fixtures/retrieval_reference.json').read_text())
    identity = model_identity(host, model) if host else {'digest': 'recorded-fixture-v1'}
    def vectors(texts, recorded):
        return ollama.embed(host, model, texts)['embeddings'] if host else recorded
    passages = fixture['passages']
    embeddings = vectors([p['text'] for p in passages], [p['vector'] for p in passages])
    queries = vectors([c['query'] for c in fixture['cases']], [c['vector'] for c in fixture['cases']])
    reports = []
    with tempfile.TemporaryDirectory(prefix='gnollama-evaluation-') as directory:
        db = DatabaseManager(str(Path(directory) / 'evaluation.db'))
        config = new_config(model or 'recorded', str(identity), dimensions=len(embeddings[0]))
        db.add_embedding_config(config)
        for passage, vector in zip(passages, embeddings):
            document = make_document(passage['id'], passage['text'])
            db.add_knowledge_document(document)
            db.begin_knowledge_index(dict(id=passage['id'], document_id=document['id'], config_id=config['id'],
                host=host or 'http://localhost:11434', model=model or 'recorded', chunk_size=1600, overlap=0, created_at=0))
            db.save_embedding_batch(passage['id'], config['id'], len(vector), [dict(id=passage['id'], ordinal=0,
                start=0, end=len(passage['text']), vector=vector_blob(vector))])
            db.finish_knowledge_index(passage['id'], 'complete')
        for case, query in zip(fixture['cases'], queries):
            hits = db.search_knowledge(config['id'], {p['id']: None for p in passages}, query)
            ids, expected = [h['id'] for h in hits], set(case['expected'])
            ranks = [ids.index(id) + 1 for id in expected if id in ids]
            report = dict(kind=case['kind'], query=case['query'], expected=case['expected'], expected_answer=case['answer'],
                retrieved=ids, passages=hits, recall_at_6=len(ranks) / len(expected) if expected else None,
                reciprocal_rank=1 / min(ranks) if ranks else 0 if expected else None,
                citation_support='requires human review', answer_correctness='requires human review')
            if answer_model:
                messages = augmented_messages([dict(role='user', content=case['query'])], dict(hits=hits, query=case['query']))
                report['answer'] = ''.join(chunk.get('message', {}).get('content', '') for chunk in ollama.chat(host, answer_model, messages))
            reports.append(report)
    scored = [r for r in reports if r['recall_at_6'] is not None]
    return dict(mode='live' if host else 'recorded', embedding_identity=identity,
        answer_identity=model_identity(host, answer_model) if answer_model else None,
        recall_at_6=sum(r['recall_at_6'] for r in scored) / len(scored),
        mean_reciprocal_rank=sum(r['reciprocal_rank'] for r in scored) / len(scored), cases=reports)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live-host')
    parser.add_argument('--embedding-model')
    parser.add_argument('--answer-model')
    parser.add_argument('--output', default='retrieval-evaluation.json')
    args = parser.parse_args()
    if bool(args.live_host) != bool(args.embedding_model) or args.answer_model and not args.live_host:
        parser.error('Live evaluation requires --live-host and --embedding-model together.')
    result = evaluate(args.live_host, args.embedding_model, args.answer_model)
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    if not args.live_host:
        assert result['recall_at_6'] == 1 and result['mean_reciprocal_rank'] == 1, result
