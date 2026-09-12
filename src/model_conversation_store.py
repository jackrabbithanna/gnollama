"""Durable sequential runs. Completed turns are the only conversation context."""
import json
import time


def migrate_model_conversations(conn, progress=None):
    conn.execute('''CREATE TABLE model_conversation_runs(
        id TEXT PRIMARY KEY REFERENCES chats(id) ON DELETE CASCADE,
        status TEXT NOT NULL, next_turn INTEGER NOT NULL DEFAULT 0)''')
    conn.execute('''CREATE TABLE model_conversation_attempts(
        id TEXT PRIMARY KEY REFERENCES messages(uid) ON DELETE CASCADE,
        run_id TEXT NOT NULL REFERENCES model_conversation_runs(id) ON DELETE CASCADE,
        turn INTEGER NOT NULL, attempt INTEGER NOT NULL, input_uid TEXT NOT NULL,
        status TEXT NOT NULL, UNIQUE(run_id, turn, attempt))''')
    conn.execute('CREATE UNIQUE INDEX model_conversation_active ON model_conversation_attempts(run_id) WHERE status="running"')
    conn.execute('CREATE UNIQUE INDEX model_conversation_completed ON model_conversation_attempts(run_id,turn) WHERE status="complete"')


class ModelConversationDatabase:
    def create_model_conversation(self, run):
        with self._get_conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if conn.execute('SELECT 1 FROM chats WHERE id=?', (run['id'],)).fetchone():
                return
            now = time.time()
            conn.execute('''INSERT INTO chats(id,title,created_at,updated_at,kind,options)
                VALUES (?,?,?,?, 'model_conversation',?)''',
                (run['id'], run['prompt'].strip().splitlines()[0][:60], now, now, json.dumps(run['settings'])))
            self._append_messages(conn, run['id'], [dict(uid=run['prompt_uid'], role='user', content=run['prompt'])], 0)
            conn.execute('INSERT INTO model_conversation_runs(id,status) VALUES (?,?)', (run['id'], 'running'))
            self._consume_draft(conn, run.get('draft_id'), run.get('draft_revision'))
            conn.commit()

    def _model_conversation_snapshot(self, conn, result):
        row = conn.execute('SELECT * FROM model_conversation_runs WHERE id=?', (result['id'],)).fetchone()
        if row:
            result['run'] = dict(row)
            result['attempts'] = [dict(r) for r in conn.execute(
                'SELECT * FROM model_conversation_attempts WHERE run_id=? ORDER BY turn,attempt', (result['id'],))]

    def set_model_conversation_status(self, id, status):
        with self._get_conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute("UPDATE model_conversation_runs SET status=? WHERE id=? AND status NOT IN ('complete','stopped')", (status, id))
            conn.execute('UPDATE chats SET updated_at=? WHERE id=?', (time.time(), id))
            row = conn.execute('SELECT * FROM model_conversation_runs WHERE id=?', (id,)).fetchone()
            conn.commit()
            return dict(row) if row else None

    def start_model_conversation_attempt(self, id, turn, uid, input_uid, message):
        with self._get_conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            existing = conn.execute('SELECT * FROM model_conversation_attempts WHERE id=?', (uid,)).fetchone()
            if existing:
                return dict(existing)
            run = conn.execute('SELECT * FROM model_conversation_runs WHERE id=?', (id,)).fetchone()
            if not run or run['status'] != 'running' or run['next_turn'] != turn:
                return None
            attempt = conn.execute('SELECT coalesce(max(attempt),0)+1 FROM model_conversation_attempts WHERE run_id=? AND turn=?', (id, turn)).fetchone()[0]
            position = conn.execute('SELECT count(*) FROM messages WHERE chat_id=?', (id,)).fetchone()[0]
            message = dict(message, participant=turn % 2, turn=turn, attempt=attempt)
            self._append_messages(conn, id, [message], position)
            conn.execute('INSERT INTO model_conversation_attempts VALUES (?,?,?,?,?,?)', (uid, id, turn, attempt, input_uid, 'running'))
            conn.commit()
            return dict(id=uid, run_id=id, turn=turn, attempt=attempt, input_uid=input_uid, status='running')

    def finish_model_conversation_attempt(self, uid, message, end_status=None):
        with self._get_conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            attempt = conn.execute('SELECT * FROM model_conversation_attempts WHERE id=?', (uid,)).fetchone()
            if not attempt:
                return None
            id = attempt['run_id']
            if attempt['status'] == 'running':
                position = conn.execute('SELECT order_index FROM messages WHERE uid=?', (uid,)).fetchone()[0]
                status = message['response_metadata']['status']
                self._append_messages(conn, id, [dict(message, participant=attempt['turn'] % 2,
                    turn=attempt['turn'], attempt=attempt['attempt'])], position)
                conn.execute('UPDATE model_conversation_attempts SET status=? WHERE id=?', (status, uid))
                settings = json.loads(conn.execute('SELECT options FROM chats WHERE id=?', (id,)).fetchone()[0])
                next_turn = attempt['turn'] + (status == 'complete')
                run_status = ('complete' if next_turn == 2 * settings['rounds'] else end_status or 'running') if status == 'complete' else end_status or 'failed'
                conn.execute('UPDATE model_conversation_runs SET next_turn=?,status=? WHERE id=?', (next_turn, run_status, id))
                conn.execute('UPDATE chats SET updated_at=? WHERE id=?', (time.time(), id))
            row = conn.execute('SELECT * FROM model_conversation_runs WHERE id=?', (id,)).fetchone()
            conn.commit()
            return dict(row)

    def discard_pending_model_conversation_attempt(self, uid):
        """A pause before dispatch is a boundary, not a failed model response."""
        with self._get_conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute("DELETE FROM messages WHERE uid=? AND content='' AND uid IN (SELECT id FROM model_conversation_attempts WHERE status='running')", (uid,))
            conn.commit()

    def interrupt_model_conversations(self):
        with self._get_conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            for row in conn.execute("SELECT id FROM model_conversation_attempts WHERE status='running'").fetchall():
                message = conn.execute('SELECT response_metadata FROM messages WHERE uid=?', (row['id'],)).fetchone()
                metadata = dict(json.loads(message[0] or '{}'), status='interrupted')
                conn.execute('UPDATE messages SET response_metadata=? WHERE uid=?', (json.dumps(metadata), row['id']))
            conn.execute("UPDATE model_conversation_attempts SET status='interrupted' WHERE status='running'")
            conn.execute("UPDATE model_conversation_runs SET status='interrupted' WHERE status='running'")
            conn.commit()
