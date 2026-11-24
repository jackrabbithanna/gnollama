"""Remove newer tables when constructing an older-schema migration fixture."""
def remove_workspace(conn):
    for name in ('chat_search_insert', 'chat_search_update', 'chat_search_delete',
                 'message_search_insert', 'message_search_update', 'message_search_delete'):
        conn.execute('DROP TRIGGER IF EXISTS ' + name)
    for name in ('model_conversation_attempts', 'model_conversation_runs', 'message_search', 'title_search', 'comparison_targets', 'draft_images', 'drafts'):
        conn.execute('DROP TABLE IF EXISTS ' + name)
    for name in ('messages_uid', 'messages_order', 'chats_recent'):
        conn.execute('DROP INDEX IF EXISTS ' + name)
    for table, column in (('messages', 'uid'), ('messages', 'extra'), ('chats', 'kind')):
        if column in {row[1] for row in conn.execute('PRAGMA table_info(' + table + ')')}:
            conn.execute('ALTER TABLE ' + table + ' DROP COLUMN ' + column)


def downgrade_to_version_three(conn):
    """Leave a current database as the version 3 schema stored it."""
    for column in ('response_metadata', 'tool_calls', 'tool_name', 'tool_call_id'):
        conn.execute('ALTER TABLE messages DROP COLUMN ' + column)
    for table in ('knowledge_web_sources', 'knowledge_collection_documents', 'knowledge_collections', 'knowledge_chunks', 'knowledge_indexes', 'embedding_configs', 'knowledge_documents'):
        conn.execute('DROP TABLE ' + table)
    conn.execute('ALTER TABLE hosts DROP COLUMN provider')
    conn.execute('ALTER TABLE hosts DROP COLUMN credential_id')
    remove_workspace(conn)
    conn.execute('PRAGMA user_version=3')
    conn.commit()
