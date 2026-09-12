"""Drafts, searchable history, and saved comparison runs."""
import base64
import json
import re
import time
import uuid


def migrate_workspace(conn, progress=None):
    conn.execute('ALTER TABLE messages ADD COLUMN uid TEXT')
    conn.execute('ALTER TABLE messages ADD COLUMN extra TEXT')
    for row in conn.execute('SELECT id FROM messages').fetchall():
        conn.execute('UPDATE messages SET uid=? WHERE id=?', (str(uuid.uuid4()), row['id']))
    conn.execute('CREATE UNIQUE INDEX messages_uid ON messages(uid)')
    conn.execute('CREATE UNIQUE INDEX messages_order ON messages(chat_id, order_index)')
    conn.execute("ALTER TABLE chats ADD COLUMN kind TEXT NOT NULL DEFAULT 'chat'")
    conn.execute('CREATE INDEX chats_recent ON chats(is_pinned DESC, updated_at DESC, id)')
    conn.execute('''CREATE TABLE drafts(id TEXT PRIMARY KEY, mode TEXT NOT NULL,
        chat_id TEXT REFERENCES chats(id) ON DELETE CASCADE, text TEXT NOT NULL,
        settings TEXT NOT NULL, targets TEXT NOT NULL, revision INTEGER NOT NULL, updated_at REAL NOT NULL)''')
    conn.execute('''CREATE TABLE draft_images(draft_id TEXT REFERENCES drafts(id) ON DELETE CASCADE,
        position INTEGER NOT NULL, image_data BLOB NOT NULL, PRIMARY KEY(draft_id, position))''')
    conn.execute('''CREATE TABLE comparison_targets(id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE, position INTEGER NOT NULL,
        settings TEXT NOT NULL, request TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
        UNIQUE(run_id, position))''')
    conn.execute("CREATE VIRTUAL TABLE title_search USING fts5(chat_id UNINDEXED, title, tokenize='unicode61')")
    conn.execute("CREATE VIRTUAL TABLE message_search USING fts5(chat_id UNINDEXED, message_uid UNINDEXED, content, tokenize='unicode61')")
    conn.execute('INSERT INTO title_search(rowid,chat_id,title) SELECT rowid,id,title FROM chats')
    conn.execute("INSERT INTO message_search(rowid,chat_id,message_uid,content) SELECT id,chat_id,uid,content FROM messages WHERE role IN ('user','assistant')")
    triggers = {
        'chat_search_insert': 'AFTER INSERT ON chats BEGIN INSERT INTO title_search(rowid,chat_id,title) VALUES (new.rowid,new.id,new.title); END',
        'chat_search_update': 'AFTER UPDATE OF title ON chats BEGIN UPDATE title_search SET title=new.title WHERE rowid=old.rowid; END',
        'chat_search_delete': 'AFTER DELETE ON chats BEGIN DELETE FROM title_search WHERE rowid=old.rowid; END',
        'message_search_insert': "AFTER INSERT ON messages WHEN new.role IN ('user','assistant') BEGIN INSERT INTO message_search(rowid,chat_id,message_uid,content) VALUES (new.id,new.chat_id,new.uid,new.content); END",
        'message_search_update': "AFTER UPDATE ON messages BEGIN DELETE FROM message_search WHERE rowid=old.id; INSERT INTO message_search(rowid,chat_id,message_uid,content) SELECT new.id,new.chat_id,new.uid,new.content WHERE new.role IN ('user','assistant'); END",
        'message_search_delete': 'AFTER DELETE ON messages BEGIN DELETE FROM message_search WHERE rowid=old.id; END',
    }
    for name, sql in triggers.items():
        conn.execute(f'CREATE TRIGGER {name} {sql}')


class WorkspaceDatabase:
    def chat_title(self, id):
        with self._get_conn() as conn:
            row = conn.execute('SELECT title FROM chats WHERE id=?', (id,)).fetchone()
            return row['title'] if row else None

    def conversation_page(self, id, match_uid=None):
        with self._get_conn() as conn:
            conn.execute('BEGIN')
            row = conn.execute('SELECT * FROM chats WHERE id=?', (id,)).fetchone()
            if row is None:
                return None
            count = conn.execute('SELECT count(*) FROM messages WHERE chat_id=?', (id,)).fetchone()[0]
            offset = max(0, count - 50)
            if match_uid:
                match = conn.execute('SELECT order_index FROM messages WHERE chat_id=? AND uid=?', (id, match_uid)).fetchone()
                if match:
                    offset = max(0, match[0] - 10)
            result = dict(row, options=json.loads(row['options'] or '{}'), system=row['system_prompt'],
                host=row['host_id'], messages=self._read_messages(conn, id, 50, offset, True),
                message_count=count, message_offset=offset, paged=True)
            if row['kind'] == 'model_conversation':
                self._model_conversation_snapshot(conn, result)
            return result

    def export_snapshot(self, id):
        # Read all tables through one SQLite snapshot, including images and target requests.
        with self._get_conn() as conn:
            conn.execute('BEGIN')
            row = conn.execute('SELECT * FROM chats WHERE id=?', (id,)).fetchone()
            if row is None:
                return None
            result = dict(row, options=json.loads(row['options'] or '{}'), system=row['system_prompt'])
            result['messages'] = self._read_messages(conn, id, include_ids=True)
            result['targets'] = [dict(r, settings=json.loads(r['settings']), request=json.loads(r['request']))
                for r in conn.execute('SELECT * FROM comparison_targets WHERE run_id=? ORDER BY position', (id,))]
            if row['kind'] == 'model_conversation':
                self._model_conversation_snapshot(conn, result)
            return result

    def save_draft(self, draft):
        with self._get_conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if draft.get('chat_id') and not conn.execute('SELECT 1 FROM chats WHERE id=?', (draft['chat_id'],)).fetchone():
                return
            old = conn.execute('SELECT revision FROM drafts WHERE id=?', (draft['id'],)).fetchone()
            if old and old['revision'] > draft.get('revision', 0):
                return
            conn.execute('''INSERT INTO drafts VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                mode=excluded.mode, chat_id=excluded.chat_id, text=excluded.text, settings=excluded.settings,
                targets=excluded.targets, revision=excluded.revision, updated_at=excluded.updated_at''',
                (draft['id'], draft['mode'], draft.get('chat_id'), draft.get('text', ''),
                 json.dumps(draft.get('settings', {})), json.dumps(draft.get('targets', [])), draft.get('revision', 0), time.time()))
            images = [base64.b64decode(image.split(',', 1)[-1], validate=True) for image in draft.get('images', [])]
            previous = [r[0] for r in conn.execute('SELECT image_data FROM draft_images WHERE draft_id=? ORDER BY position', (draft['id'],))]
            if images != previous:
                conn.execute('DELETE FROM draft_images WHERE draft_id=?', (draft['id'],))
                conn.executemany('INSERT INTO draft_images VALUES (?,?,?)', [(draft['id'], i, raw) for i, raw in enumerate(images)])
            conn.commit()

    def get_draft(self, id):
        with self._get_conn() as conn:
            row = conn.execute('SELECT * FROM drafts WHERE id=?', (id,)).fetchone()
            if row is None:
                return None
            draft = dict(row)
            draft['settings'] = json.loads(draft['settings'])
            draft['targets'] = json.loads(draft['targets'])
            draft['images'] = [base64.b64encode(r[0]).decode('ascii') for r in conn.execute(
                'SELECT image_data FROM draft_images WHERE draft_id=? ORDER BY position', (id,))]
            return draft

    def list_drafts(self, limit=100, offset=0):
        with self._get_conn() as conn:
            return [dict(r) for r in conn.execute('''SELECT d.id,d.mode,d.chat_id,substr(d.text,1,80) AS title,d.updated_at
                FROM drafts d WHERE (length(trim(d.text))>0 OR d.targets!='[]'
                OR EXISTS (SELECT 1 FROM draft_images i WHERE i.draft_id=d.id)
                OR json_extract(d.settings,'$._configured')=1)
                AND (d.chat_id IS NULL OR NOT EXISTS (SELECT 1 FROM messages m WHERE m.chat_id=d.chat_id))
                ORDER BY d.updated_at DESC,d.id LIMIT ? OFFSET ?''', (limit, offset))]

    def delete_draft(self, id, revision=None):
        with self._get_conn() as conn:
            self._consume_draft(conn, id, revision)
            conn.commit()

    def _consume_draft(self, conn, id, revision=None):
        if id:
            conn.execute('DELETE FROM drafts WHERE id=? AND (? IS NULL OR revision<=?)', (id, revision, revision))

    def list_history(self, query='', limit=100, offset=0):
        terms = re.findall(r'\w+', query, re.UNICODE)
        expression = ' AND '.join('"' + term.replace('"', '""') + '"*' for term in terms)
        with self._get_conn() as conn:
            params = []
            if expression:
                sql = '''WITH matches AS MATERIALIZED (
                    SELECT chat_id,NULL AS match_uid,title AS snippet FROM title_search WHERE title_search MATCH ?
                    UNION ALL SELECT chat_id,message_uid,snippet(message_search,2,'','', '…',20)
                    FROM message_search WHERE message_search MATCH ?)
                    SELECT c.*, m.match_uid, m.snippet FROM chats c JOIN
                    (SELECT chat_id,match_uid,snippet FROM matches GROUP BY chat_id) m ON m.chat_id=c.id'''
                params.extend((expression, expression))
            else:
                sql = "SELECT c.*,NULL AS match_uid,'' AS snippet FROM chats c"
            sql += ' ORDER BY c.is_pinned DESC,c.updated_at DESC,c.id LIMIT ? OFFSET ?'
            rows = conn.execute(sql, (*params, limit, offset)).fetchall()
            return [dict(r, options=json.loads(r['options'] or '{}'), messages=[],
                         system=r['system_prompt'], host=r['host_id']) for r in rows]

    def create_comparison(self, run):
        with self._get_conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            now = time.time()
            conn.execute('''INSERT INTO chats(id,title,created_at,updated_at,kind,options)
                VALUES (?,?,?,?, 'comparison',?)''', (run['id'], run['prompt'].splitlines()[0][:60], now, now, json.dumps(run['settings'])))
            self._append_messages(conn, run['id'], [{'uid': run['message_uid'], 'role': 'user', 'content': run['prompt'],
                'images': run.get('images', []), 'response_metadata': {'retrieval': run.get('retrieval')}}], 0)
            for position, target in enumerate(run['targets']):
                conn.execute('INSERT INTO comparison_targets(id,run_id,position,settings,request,status) VALUES (?,?,?,?,?,?)',
                    (target['id'], run['id'], position, json.dumps(target['settings']), json.dumps(target['request']), 'queued'))
            self._consume_draft(conn, run.get('draft_id'), run.get('draft_revision'))
            conn.commit()

    def comparison(self, id):
        chat = self.get_chat(id)
        if chat is None or chat.get('kind') != 'comparison':
            return None
        with self._get_conn() as conn:
            chat['targets'] = [dict(r, settings=json.loads(r['settings']), request=json.loads(r['request']))
                for r in conn.execute('SELECT * FROM comparison_targets WHERE run_id=? ORDER BY position', (id,))]
        return chat

    def start_comparison_target(self, id):
        with self._get_conn() as conn:
            conn.execute("UPDATE comparison_targets SET status='running' WHERE id=? AND status='queued'", (id,))
            conn.commit()

    def finish_comparison_target(self, id, message):
        with self._get_conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            target = conn.execute('SELECT * FROM comparison_targets WHERE id=?', (id,)).fetchone()
            if target is None:
                return
            self._append_messages(conn, target['run_id'], [message], target['position'] + 1)
            conn.execute('UPDATE comparison_targets SET status=? WHERE id=?', (message['response_metadata']['status'], id))
            conn.execute('UPDATE chats SET updated_at=? WHERE id=?', (time.time(), target['run_id']))
            conn.commit()

    def interrupt_comparisons(self):
        with self._get_conn() as conn:
            conn.execute("UPDATE comparison_targets SET status='interrupted' WHERE status IN ('queued','running')")
            conn.commit()
