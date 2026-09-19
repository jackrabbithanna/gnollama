"""Lightweight, independently limited sidebar queries and title completions."""
import re
import unicodedata


CATEGORIES = ('drafts', 'pinned', 'chat', 'comparison', 'model_conversation')


def category(record):
    if record.get('kind') == 'drafts':
        return 'drafts'
    if record.get('is_pinned'):
        return 'pinned'
    return record.get('kind') if record.get('kind') in CATEGORIES[2:] else 'chat'


def _words(text):
    normalized = unicodedata.normalize('NFD', text.casefold())
    return re.findall(r'\w+', ''.join(c for c in normalized if not unicodedata.combining(c)))


def sidebar_snapshot(database, query='', expanded=()):
    """One read snapshot; limits apply after searching, separately per section."""
    terms = re.findall(r'\w+', query, re.UNICODE)
    expression = ' AND '.join('"' + term.replace('"', '""') + '"*' for term in terms)
    searching = bool(query.strip())
    with database._get_conn() as conn:
        conn.execute('BEGIN')
        prefix, join, params = '', '', []
        if expression:
            prefix = '''WITH matches AS MATERIALIZED (
                SELECT chat_id,NULL AS match_uid,title AS snippet,0 AS priority,rowid AS ordinal
                    FROM title_search WHERE title_search MATCH ?
                UNION ALL SELECT chat_id,message_uid,snippet(message_search,2,'','', '…',20),1,rowid
                    FROM message_search WHERE message_search MATCH ?),
                ranked AS (SELECT *,row_number() OVER (PARTITION BY chat_id ORDER BY priority,ordinal) AS rank FROM matches) '''
            join = ' JOIN ranked m ON m.chat_id=c.id AND m.rank=1 '
            params = [expression, expression]
        fields = 'm.match_uid,m.snippet' if expression else "NULL AS match_uid,'' AS snippet"
        sql = prefix + 'SELECT c.id,c.title,c.kind,c.is_pinned,c.updated_at,' + fields + ' FROM chats c' + join
        groups = {}
        for key in CATEGORIES[1:]:
            condition = 'c.is_pinned=1' if key == 'pinned' else 'c.is_pinned=0 AND c.kind=?'
            values = [] if key == 'pinned' else [key]
            if searching and not expression:
                condition += ' AND 0'
            rows = conn.execute(sql + ' WHERE ' + condition + ' ORDER BY c.updated_at DESC,c.id LIMIT ?',
                                (*params, *values, -1 if key in expanded else 6)).fetchall()
            groups[key] = [dict(row) for row in rows]

        # Drafts are not in the persistent message FTS index. Match their full
        # text on the worker, before applying the per-section limit.
        needles = _words(query)
        def matches(text):
            words = _words(text or '')
            return bool(needles) and all(any(word.startswith(needle) for word in words) for needle in needles)
        conn.create_function('draft_matches', 1, matches, deterministic=True)
        draft_sql = '''SELECT d.id,d.mode,d.chat_id,substr(d.text,1,80) AS title,d.updated_at
            FROM drafts d WHERE (length(trim(d.text))>0 OR d.targets!='[]'
            OR EXISTS (SELECT 1 FROM draft_images i WHERE i.draft_id=d.id)
            OR json_extract(d.settings,'$._configured')=1)
            AND (d.chat_id IS NULL OR NOT EXISTS (SELECT 1 FROM messages m WHERE m.chat_id=d.chat_id))'''
        groups['drafts'] = [dict(row) for row in conn.execute(draft_sql +
            (' AND draft_matches(d.text)' if searching else '') +
            ' ORDER BY d.updated_at DESC,d.id LIMIT ?', (-1 if 'drafts' in expanded else 6,))]

        suggestions = []
        if expression:
            suggestions = [dict(row) for row in conn.execute('''SELECT c.title,c.kind,c.is_pinned,c.updated_at
                FROM title_search t JOIN chats c ON c.id=t.chat_id WHERE title_search MATCH ?
                GROUP BY c.title ORDER BY max(c.updated_at) DESC,c.title LIMIT 8''', (expression,))]
            for row in conn.execute(draft_sql + ' AND draft_matches(substr(d.text,1,80)) ORDER BY d.updated_at DESC,d.id LIMIT 8'):
                suggestions.append(dict(title=row['title'], kind='drafts', is_pinned=False, updated_at=row['updated_at']))
        unique = {}
        for item in sorted(suggestions, key=lambda r: (-r['updated_at'], r['title'])):
            unique.setdefault(item['title'], item)
        return dict(groups=groups, suggestions=list(unique.values())[:8])
