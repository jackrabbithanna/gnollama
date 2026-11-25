"""Image sessions. A run is its prompt as a user message, holding the reference images, and one row here.

Once it ends with images or text, an assistant message holds that text and the images made; `output_id`
names it. `message_images` has no type column, so `result` keeps, as JSON,
`{"media_types": [one per output image, in order], "cost": float | null, "extra": {...}}`:
`ImageResult` without its images and text, which live in that message.
"""
import base64
import json
import time
import uuid

from .image_generation import media_type
from .store_common import Repository, append_messages, consume_draft

# Version 15: Image runs; the session is a chat of kind 'image'.
MIGRATION = '''
CREATE TABLE image_runs(
    id TEXT PRIMARY KEY REFERENCES messages(uid) ON DELETE CASCADE,
    session_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    position INTEGER NOT NULL, output_id TEXT REFERENCES messages(uid) ON DELETE SET NULL,
    options TEXT NOT NULL, target TEXT NOT NULL, status TEXT NOT NULL,
    result TEXT, error TEXT, created_at REAL NOT NULL, finished_at REAL,
    UNIQUE(session_id, position));
CREATE INDEX image_runs_output ON image_runs(output_id);
'''

ENDED = ('done', 'failed', 'cancelled')


def session_title(prompt):
    """The first line of the first prompt."""
    return next(iter(prompt.strip().splitlines()), '')[:60] or 'Image'


def _images(conn, message_id):
    if message_id is None:
        return []
    return [base64.b64encode(row[0]).decode('ascii') for row in conn.execute(
        'SELECT image_data FROM message_images WHERE message_id=? ORDER BY id', (message_id,))]


def read_runs(conn, session_id, limit=None, offset=0, id=None):
    runs = []
    for row in conn.execute('''SELECT r.*, m.id AS message_id, m.content, o.id AS output_message, o.content AS text
            FROM image_runs r JOIN messages m ON m.uid=r.id LEFT JOIN messages o ON o.uid=r.output_id
            WHERE r.session_id=? AND (? IS NULL OR r.id=?)
            ORDER BY r.position LIMIT ? OFFSET ?''', (session_id, id, id, -1 if limit is None else limit, offset)):
        result = json.loads(row['result']) if row['result'] else None
        kinds = (result or {}).get('media_types') or []
        outputs = _images(conn, row['output_message'])
        runs.append(dict(id=row['id'], session_id=row['session_id'], position=row['position'],
                         prompt=row['content'], references=_images(conn, row['message_id']),
                         options=json.loads(row['options']), target=json.loads(row['target']),
                         status=row['status'], output_id=row['output_id'],
                         # A type that was not recorded is told from the image itself.
                         images=[dict(data=data, media_type=kinds[index] if index < len(kinds) else media_type(data))
                                 for index, data in enumerate(outputs)],
                         text=row['text'] or '', result=result, error=row['error'],
                         created_at=row['created_at'], finished_at=row['finished_at']))
    return runs


def image_snapshot(conn, result, limit=None, offset=0):
    result['runs'] = read_runs(conn, result['id'], limit, offset)


def run_offset(conn, session_id, uid):
    """How many runs come before the one whose prompt or output is the message `uid`, or None."""
    row = conn.execute('SELECT position FROM image_runs WHERE session_id=? AND (id=? OR output_id=?)',
                       (session_id, uid, uid)).fetchone()
    if row is None:
        return None
    return conn.execute('SELECT count(*) FROM image_runs WHERE session_id=? AND position<?',
                        (session_id, row['position'])).fetchone()[0]


def _next_message(conn, session_id):
    return conn.execute('SELECT coalesce(max(order_index),-1)+1 FROM messages WHERE chat_id=?',
                        (session_id,)).fetchone()[0]


class ImageDatabase(Repository):
    def create_image_run(self, run):
        """Save a run before it is sent. The first one creates its session; none revives a deleted one."""
        session = run['session_id']
        with self._get_conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            saved = read_runs(conn, session, id=run['id'])
            if saved:
                return saved[0]
            now = time.time()
            options, target = run.get('options') or {}, run.get('target') or {}
            # The shape of ImageSettings.to_stored(), which the next run starts from.
            settings = dict(options=options, **({'target': target} if target else {}))
            if conn.execute('SELECT 1 FROM chats WHERE id=?', (session,)).fetchone():
                conn.execute('UPDATE chats SET options=?, updated_at=? WHERE id=?', (json.dumps(settings), now, session))
            elif run.get('new_session'):
                conn.execute('''INSERT INTO chats(id,title,created_at,updated_at,kind,options)
                    VALUES (?,?,?,?, 'image',?)''', (session, session_title(run['prompt']), now, now, json.dumps(settings)))
            else:
                return None
            position = conn.execute('SELECT coalesce(max(position),-1)+1 FROM image_runs WHERE session_id=?',
                                    (session,)).fetchone()[0]
            append_messages(conn, session, [dict(uid=run['id'], role='user', content=run['prompt'],
                                                 images=run.get('references') or [])], _next_message(conn, session))
            conn.execute('''INSERT INTO image_runs(id,session_id,position,options,target,status,created_at)
                VALUES (?,?,?,?,?, 'running',?)''', (run['id'], session, position, json.dumps(options), json.dumps(target), now))
            consume_draft(conn, run.get('draft_id'), run.get('draft_revision'))
            saved = read_runs(conn, session, id=run['id'])
            conn.commit()
            return saved[0]

    def finish_image_run(self, id, status, result=None, error=None):
        """Record how a running run ended, with the `ImageResult` it got, if any, whatever the status.

        A run that has ended, or was deleted, is left as it is. Raises ValueError for an image
        that is not base64, saving nothing.
        """
        if status not in ENDED:
            raise ValueError(status)
        with self._get_conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT session_id, status, target FROM image_runs WHERE id=?', (id,)).fetchone()
            if row is None:
                return None
            if row['status'] == 'running':
                now = time.time()
                images = tuple(result.images) if result is not None else ()
                text = result.text if result is not None else ''
                output = None
                if images or text:
                    output = str(uuid.uuid4())
                    append_messages(conn, row['session_id'], [dict(
                        uid=output, role='assistant', content=text, model=json.loads(row['target']).get('model'),
                        images=[image.data for image in images])], _next_message(conn, row['session_id']))
                stored = None if result is None else json.dumps(dict(
                    media_types=[image.media_type for image in images], cost=result.cost, extra=result.extra))
                conn.execute('''UPDATE image_runs SET status=?, output_id=?, result=?, error=?, finished_at=?
                    WHERE id=?''', (status, output, stored,
                                    str(error) if status == 'failed' and error is not None else None, now, id))
                conn.execute('UPDATE chats SET updated_at=? WHERE id=?', (now, row['session_id']))
            saved = read_runs(conn, row['session_id'], id=id)
            conn.commit()
            return saved[0]

    def image_runs(self, session_id, limit=None, offset=0):
        with self._get_conn() as conn:
            return read_runs(conn, session_id, limit, offset)

    def interrupt_image_runs(self):
        with self._get_conn() as conn:
            conn.execute("UPDATE image_runs SET status='interrupted' WHERE status='running'")
            conn.commit()
