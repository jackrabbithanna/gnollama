"""Decision sessions. Each run is one message, holding the state and its images, and one row here."""
import base64
import json
import time

from .store_common import Repository, append_messages, consume_draft

# Version 14: Decision runs; the session is a chat of kind 'decision'.
MIGRATION = '''
CREATE TABLE decision_runs(
    id TEXT PRIMARY KEY REFERENCES messages(uid) ON DELETE CASCADE,
    session_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    position INTEGER NOT NULL, state_mode TEXT NOT NULL DEFAULT 'text',
    questions TEXT NOT NULL, target TEXT NOT NULL, status TEXT NOT NULL,
    result TEXT, error TEXT, created_at REAL NOT NULL, finished_at REAL,
    UNIQUE(session_id, position));
'''

ENDED = ('done', 'failed', 'cancelled')


def session_title(state, mode, questions):
    """The first line of the state, or all of a JSON state on one line; the question names when there is none."""
    words = ' '.join(state.split()) if mode == 'json' else next(iter(state.strip().splitlines()), '')
    return (words or ', '.join(questions))[:60] or 'Decision'


def read_runs(conn, session_id, limit=None, offset=0, id=None):
    runs = []
    for row in conn.execute('''SELECT r.*, m.id AS message_id, m.content FROM decision_runs r
            JOIN messages m ON m.uid=r.id WHERE r.session_id=? AND (? IS NULL OR r.id=?)
            ORDER BY r.position LIMIT ? OFFSET ?''', (session_id, id, id, -1 if limit is None else limit, offset)):
        images = [base64.b64encode(image[0]).decode('ascii') for image in conn.execute(
            'SELECT image_data FROM message_images WHERE message_id=? ORDER BY id', (row['message_id'],))]
        runs.append(dict(id=row['id'], session_id=row['session_id'], position=row['position'],
                         state=row['content'], state_mode=row['state_mode'], images=images,
                         questions=json.loads(row['questions']), target=json.loads(row['target']),
                         status=row['status'], result=json.loads(row['result']) if row['result'] else None,
                         error=row['error'], created_at=row['created_at'], finished_at=row['finished_at']))
    return runs


def decision_snapshot(conn, result, limit=None, offset=0):
    result['runs'] = read_runs(conn, result['id'], limit, offset)


class DecisionDatabase(Repository):
    def create_decision_run(self, run):
        """Save a run before it is sent. The first one creates its session; none revives a deleted one."""
        session = run['session_id']
        with self._get_conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            saved = read_runs(conn, session, id=run['id'])
            if saved:
                return saved[0]
            now = time.time()
            mode = 'json' if run.get('state_mode') == 'json' else 'text'
            questions, target = run['questions'], run.get('target') or {}
            options = json.dumps(dict(questions=questions, state_mode=mode, target=target))
            if conn.execute('SELECT 1 FROM chats WHERE id=?', (session,)).fetchone():
                conn.execute('UPDATE chats SET options=?, updated_at=? WHERE id=?', (options, now, session))
            elif run.get('new_session'):
                conn.execute('''INSERT INTO chats(id,title,created_at,updated_at,kind,options)
                    VALUES (?,?,?,?, 'decision',?)''',
                    (session, session_title(run['state'], mode, questions), now, now, options))
            else:
                return None
            position = conn.execute('SELECT coalesce(max(order_index),-1)+1 FROM messages WHERE chat_id=?',
                                    (session,)).fetchone()[0]
            append_messages(conn, session, [dict(uid=run['id'], role='user', content=run['state'],
                                                 images=run.get('images') or [])], position)
            conn.execute('''INSERT INTO decision_runs(id,session_id,position,state_mode,questions,target,status,created_at)
                VALUES (?,?,?,?,?,?, 'running',?)''',
                (run['id'], session, position, mode, json.dumps(questions), json.dumps(target), now))
            consume_draft(conn, run.get('draft_id'), run.get('draft_revision'))
            saved = read_runs(conn, session, id=run['id'])
            conn.commit()
            return saved[0]

    def finish_decision_run(self, id, status, result=None, error=None):
        """Record how a running run ended. A run that has ended, or was deleted, is left as it is."""
        if status not in ENDED:
            raise ValueError(status)
        with self._get_conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT session_id, status FROM decision_runs WHERE id=?', (id,)).fetchone()
            if row is None:
                return None
            if row['status'] == 'running':
                now = time.time()
                conn.execute('UPDATE decision_runs SET status=?, result=?, error=?, finished_at=? WHERE id=?',
                             (status, json.dumps(result) if status == 'done' else None,
                              str(error) if status == 'failed' and error is not None else None, now, id))
                conn.execute('UPDATE chats SET updated_at=? WHERE id=?', (now, row['session_id']))
            saved = read_runs(conn, row['session_id'], id=id)
            conn.commit()
            return saved[0]

    def decision_runs(self, session_id, limit=None, offset=0):
        with self._get_conn() as conn:
            return read_runs(conn, session_id, limit, offset)

    def interrupt_decisions(self):
        with self._get_conn() as conn:
            conn.execute("UPDATE decision_runs SET status='interrupted' WHERE status='running'")
            conn.commit()
