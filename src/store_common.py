"""What the repositories share: the connection provider and rows read by several of them."""
import base64
import json
import uuid


class Repository:
    """Persistence for one area of the schema, over the shared connections."""

    def __init__(self, connections):
        self.connections = connections

    def _get_conn(self):
        return self.connections.connection()


def read_messages(conn, chat_id, limit=None, offset=0, include_ids=False):
    messages = []
    cursor = conn.execute("""
        SELECT id, role, content, model, thinking_content, api_details, response_metadata,
               tool_calls, tool_name, tool_call_id, uid, extra
        FROM messages
        WHERE chat_id = ?
        ORDER BY order_index ASC LIMIT ? OFFSET ?
    """, (chat_id, -1 if limit is None else limit, offset))
    rows = cursor.fetchall()
    for row in rows:
        msg_id = row["id"]
        msg = {
            "role": row["role"],
            "content": row["content"]
        }
        if row["model"] is not None:
            msg["model"] = row["model"]
        if row["thinking_content"] is not None:
            msg["thinking_content"] = row["thinking_content"]
        if row["api_details"] is not None:
            try:
                msg["api_details"] = json.loads(row["api_details"])
            except Exception:
                pass

        if row["response_metadata"]:
            msg["response_metadata"] = json.loads(row["response_metadata"])
        if row['tool_calls'] is not None:
            msg['tool_calls'] = json.loads(row['tool_calls'])
        for key in ('tool_name', 'tool_call_id'):
            if row[key] is not None:
                msg[key] = row[key]

        # Fetch attached images
        img_cursor = conn.execute("SELECT image_data FROM message_images WHERE message_id = ?", (msg_id,))
        img_rows = img_cursor.fetchall()
        if img_rows:
            images_b64 = []
            for img_row in img_rows:
                img_bin = img_row["image_data"]
                img_b64 = base64.b64encode(img_bin).decode("utf-8")
                images_b64.append(img_b64)
            msg["images"] = images_b64

        msg.update(json.loads(row['extra'] or '{}'))
        if include_ids:
            msg['uid'] = row['uid']
        messages.append(msg)
    return messages


def append_messages(conn, chat_id, messages, start):
    fields = ('role', 'content', 'model', 'thinking_content', 'api_details',
              'response_metadata', 'tool_calls', 'tool_name', 'tool_call_id', 'extra')
    json_fields = ('api_details', 'response_metadata', 'tool_calls')
    known = set(fields) | {'images', 'uid'}
    for index, message in enumerate(messages, start):
        uid = message.get('uid')
        old = conn.execute('SELECT * FROM messages WHERE chat_id=? AND (uid=? OR order_index=?)',
                           (chat_id, uid, index)).fetchone()
        values = {k: message.get(k) for k in fields}
        values['content'] = message.get('content', '')
        values['extra'] = json.dumps({k: v for k, v in message.items() if k not in known})
        for key in json_fields:
            values[key] = json.dumps(message[key]) if key in message and message[key] is not None else None
        if old is None:
            cursor = conn.execute('INSERT INTO messages(chat_id,uid,order_index,' + ','.join(fields)
                + ') VALUES (' + ','.join('?' for _ in range(len(fields)+3)) + ')',
                (chat_id, uid or str(uuid.uuid4()), index, *(values[k] for k in fields)))
            id = cursor.lastrowid
        else:
            id = old['id']
            if any(old[k] != values[k] for k in fields):
                conn.execute('UPDATE messages SET ' + ','.join(k+'=?' for k in fields) + ' WHERE id=?',
                             (*(values[k] for k in fields), id))
        images = [base64.b64decode(image.split(',', 1)[-1], validate=True) for image in message.get('images', [])]
        previous = [r[0] for r in conn.execute('SELECT image_data FROM message_images WHERE message_id=? ORDER BY id', (id,))] if old else []
        if images != previous:
            conn.execute('DELETE FROM message_images WHERE message_id=?', (id,))
            conn.executemany('INSERT INTO message_images(message_id,image_data) VALUES (?,?)', [(id, raw) for raw in images])


def save_messages(conn, chat_id, messages):
    append_messages(conn, chat_id, messages, 0)
    conn.execute('DELETE FROM messages WHERE chat_id=? AND order_index>=?', (chat_id, len(messages)))


def read_draft(conn, id):
    row = conn.execute('SELECT * FROM drafts WHERE id=?', (id,)).fetchone()
    if row is None:
        return None
    draft = dict(row)
    draft['settings'] = json.loads(draft['settings'])
    draft['targets'] = json.loads(draft['targets'])
    draft['images'] = [base64.b64encode(r[0]).decode('ascii') for r in conn.execute(
        'SELECT image_data FROM draft_images WHERE draft_id=? ORDER BY position', (id,))]
    return draft


def consume_draft(conn, id, revision=None):
    if id:
        conn.execute('DELETE FROM drafts WHERE id=? AND (? IS NULL OR revision<=?)', (id, revision, revision))
