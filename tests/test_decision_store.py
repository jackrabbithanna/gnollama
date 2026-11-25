"""Decision sessions in the database: runs, drafts, history, startup recovery, and export."""
import base64
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import database, history
from src.database import DatabaseManager
from src.decision import parse_questions, parse_result, questions_object
from src.export import export_json, export_markdown
from src.records import DecisionSettings, ModelTarget, has_saved_work
from src.storage import ChatStorage

FIXTURES = Path(__file__).parent / 'fixtures'
PNG = base64.b64encode(b'\x89PNG\r\n\x1a\n image').decode()
JPEG = base64.b64encode(b'\xff\xd8\xff photo').decode()
QUESTIONS = {
    'billing': {'type': 'noul', 'instructions': 'Is this about billing?'},
    'tone': {'type': 'choice', 'criteria': {'angry': 'Hostile', 'calm': None, 'excited': 'Eager'}},
    'urgency': {'type': 'score', 'instructions': 'How soon?',
                'criteria': ['Can wait', 'Needs attention this week', 'Needs attention today']},
    'where': {'type': 'box', 'instructions': 'The logo'},
    'password': {'type': 'noul', 'instructions': 'Does it hold a password?'},
    'late': {'type': 'noul'},
    'odd': {'type': 'noul'},
}
SETTINGS = DecisionSettings(ModelTarget('host-1', 'https://openrouter.ai/api', 'typesafe/jev-1.14', 'Router'), QUESTIONS)


def result():
    raw = json.loads((FIXTURES / 'systemone_openrouter.json').read_text())
    raw['answers'].update(password={'type': 'refusal'}, odd={'type': 'noul', 'noul': 7})
    return raw


def new_run(id='run-1', session='session', state='The invoice was charged twice.\nPlease refund.', new=True, **values):
    return dict(SETTINGS.to_stored(), id=id, session_id=session, new_session=new, state=state, **values)


class DecisionStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / 'gnollama.db')
        self.db = DatabaseManager(self.path)
        self.addCleanup(lambda: self.db.close())
        self.store = self.db.decisions

    def count(self, table):
        with self.db._get_conn() as conn:
            return conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0]

    def test_a_new_database_has_the_runs_table(self):
        with self.db._get_conn() as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 15)
            self.assertEqual([row[1] for row in conn.execute('PRAGMA table_info(decision_runs)')],
                             ['id', 'session_id', 'position', 'state_mode', 'questions', 'target', 'status',
                              'result', 'error', 'created_at', 'finished_at'])
        self.assertIsNone(self.db.backup_path)

    def test_upgrade_from_version_13_keeps_what_was_saved(self):
        path = str(Path(self.temp.name) / 'old.db')
        with patch.object(database, 'MIGRATIONS', database.MIGRATIONS[:12]):
            old = DatabaseManager(path)
        old.chats.create_chat('chat', 'Saved chat', 1, 2, 'model')
        messages = [dict(uid='question', role='user', content='kept words', images=[PNG]),
                    dict(uid='answer', role='assistant', content='reply', response_metadata={'status': 'complete'})]
        old.chats.save_chat('chat', messages, options={'temperature': 0.5})
        old.workspace.save_draft(dict(id='draft', mode='chat', text='unsent', settings={}, revision=1))
        with old._get_conn() as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 13)
            self.assertIsNone(conn.execute("SELECT 1 FROM sqlite_master WHERE name='decision_runs'").fetchone())
        old.close()
        upgraded = DatabaseManager(path)
        self.addCleanup(upgraded.close)
        self.assertIn('.pre-v15-', upgraded.backup_path)
        with sqlite3.connect(upgraded.backup_path) as backup:
            self.assertEqual(backup.execute('PRAGMA user_version').fetchone()[0], 13)
            self.assertEqual(backup.execute("SELECT content FROM messages WHERE uid='question'").fetchone()[0], 'kept words')
        with upgraded._get_conn() as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 15)
        chat = upgraded.chats.get_chat('chat')
        self.assertEqual((chat['title'], chat['kind'], chat['options']), ('Saved chat', 'chat', {'temperature': 0.5}))
        self.assertEqual(upgraded.chats.get_messages('chat', include_ids=True), messages)
        self.assertEqual(upgraded.workspace.get_draft('draft')['text'], 'unsent')
        self.assertEqual(upgraded.workspace.list_history('kept')[0]['match_uid'], 'question')
        self.assertEqual(upgraded.decisions.create_decision_run(new_run())['status'], 'running')
        reopened = DatabaseManager(path)
        self.addCleanup(reopened.close)
        self.assertIsNone(reopened.backup_path)

    def test_a_failed_upgrade_leaves_version_13_and_a_backup(self):
        path = str(Path(self.temp.name) / 'old.db')
        with patch.object(database, 'MIGRATIONS', database.MIGRATIONS[:12]):
            DatabaseManager(path).close()
        broken = database.MIGRATIONS[:12] + [database.MIGRATIONS[12] + 'CREATE TABLE decision_runs(id TEXT);']
        with patch.object(database, 'MIGRATIONS', broken):
            with self.assertRaises(database.DatabaseUpgradeError) as caught:
                DatabaseManager(path)
        self.assertTrue(Path(caught.exception.backup_path).exists())
        with sqlite3.connect(path) as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 13)
            self.assertIsNone(conn.execute("SELECT 1 FROM sqlite_master WHERE name='decision_runs'").fetchone())
        DatabaseManager(path).close()

    def test_the_first_run_creates_its_session_with_what_was_entered(self):
        run = self.store.create_decision_run(new_run(images=[PNG]))
        self.assertEqual({key: run[key] for key in ('id', 'session_id', 'position', 'state', 'state_mode', 'images',
                                                    'status', 'result', 'error', 'finished_at')},
                         dict(id='run-1', session_id='session', position=0,
                              state='The invoice was charged twice.\nPlease refund.', state_mode='text', images=[PNG],
                              status='running', result=None, error=None, finished_at=None))
        self.assertEqual(run['questions'], QUESTIONS)
        self.assertEqual(list(run['questions']), list(QUESTIONS))
        self.assertEqual(run['target'], dict(host='https://openrouter.ai/api', host_id='host-1',
                                             host_name='Router', model='typesafe/jev-1.14'))
        self.assertEqual(DecisionSettings.from_stored(run), SETTINGS)
        self.assertEqual(questions_object(parse_questions(run['questions'])), QUESTIONS)
        chat = self.db.chats.get_chat('session')
        self.assertEqual((chat['kind'], chat['title']), ('decision', 'The invoice was charged twice.'))
        self.assertEqual(DecisionSettings.from_stored(chat['options']), SETTINGS)
        self.assertEqual(chat['created_at'], run['created_at'])
        # Saving the same run again changes nothing.
        self.assertEqual(self.store.create_decision_run(new_run(state='changed')), run)
        self.assertEqual((self.count('messages'), self.count('decision_runs')), (1, 1))

    def test_titles_come_from_the_state_or_the_question_names(self):
        json_state = '{\n  "ticket": 7,\n  "text": "' + 'long ' * 20 + '"\n}'
        self.store.create_decision_run(new_run('a', 'json', json_state, state_mode='json'))
        self.store.create_decision_run(new_run('b', 'image', '  \n', images=[PNG]))
        self.assertEqual(self.db.workspace.chat_title('json'), ' '.join(json_state.split())[:60])
        self.assertEqual(self.db.workspace.chat_title('image'), ', '.join(QUESTIONS)[:60])
        self.assertEqual(self.store.decision_runs('json')[0]['state_mode'], 'json')
        self.assertEqual(self.store.decision_runs('json')[0]['state'], json_state)

    def test_a_run_ends_once_as_done_failed_or_cancelled(self):
        for id in ('done', 'failed', 'cancelled'):
            self.store.create_decision_run(new_run(id, new=id == 'done'))
        raw = result()
        done = self.store.finish_decision_run('done', 'done', result=raw)
        self.assertEqual((done['status'], done['result'], done['error']), ('done', raw, None))
        self.assertGreaterEqual(done['finished_at'], done['created_at'])
        parsed = parse_result(done['result'], parse_questions(done['questions']))
        self.assertEqual([answer.type for answer in parsed.answers],
                         ['noul', 'choice', 'score', 'box', 'refusal', 'missing', 'invalid'])
        self.assertEqual((parsed.cost, parsed.extra['provider']), (0.000449, 'TypeSafe'))
        failed = self.store.finish_decision_run('failed', 'failed', error='The model was not found.')
        self.assertEqual((failed['status'], failed['result'], failed['error']), ('failed', None, 'The model was not found.'))
        cancelled = self.store.finish_decision_run('cancelled', 'cancelled')
        self.assertEqual((cancelled['status'], cancelled['result'], cancelled['error']), ('cancelled', None, None))
        # A later outcome does not replace the first.
        self.assertEqual(self.store.finish_decision_run('done', 'failed', error='late'), done)
        self.assertEqual(self.store.finish_decision_run('failed', 'done', result=raw), failed)
        self.assertEqual(self.store.finish_decision_run('cancelled', 'cancelled'), cancelled)
        self.assertIsNone(self.store.finish_decision_run('unknown', 'done', result=raw))
        with self.assertRaises(ValueError):
            self.store.finish_decision_run('done', 'running')
        self.assertEqual([run['status'] for run in self.store.decision_runs('session')], ['done', 'failed', 'cancelled'])

    def test_images_are_stored_once_as_bytes_and_read_back_in_order(self):
        self.store.create_decision_run(new_run(images=[PNG, 'data:image/jpeg;base64,' + JPEG]))
        self.assertEqual(self.store.decision_runs('session')[0]['images'], [PNG, JPEG])
        with self.db._get_conn() as conn:
            self.assertEqual([row[0] for row in conn.execute('SELECT image_data FROM message_images ORDER BY id')],
                             [base64.b64decode(PNG), base64.b64decode(JPEG)])
        with self.assertRaises(ValueError):
            self.store.create_decision_run(new_run('bad', new=False, images=['!']))
        self.assertEqual((self.count('messages'), self.count('decision_runs')), (1, 1))

    def test_later_runs_join_the_session_in_order_and_update_its_form(self):
        first = self.store.create_decision_run(new_run())
        changed = DecisionSettings(ModelTarget('host-2', 'http://localhost:11434', 'd3', 'Local'),
                                   {'spam': {'type': 'noul'}}, 'json')
        second = self.store.create_decision_run(dict(changed.to_stored(), id='run-2', session_id='session', state='{"a": 1}'))
        third = self.store.create_decision_run(new_run('run-3', new=False, state='third'))
        self.assertEqual([run['position'] for run in (first, second, third)], [0, 1, 2])
        runs = self.store.decision_runs('session')
        self.assertEqual([(run['id'], run['state']) for run in runs],
                         [('run-1', first['state']), ('run-2', '{"a": 1}'), ('run-3', 'third')])
        self.assertEqual(DecisionSettings.from_stored(runs[1]), changed)
        self.assertEqual(self.store.decision_runs('session', limit=1, offset=1), [second])
        chat = self.db.chats.get_chat('session')
        self.assertEqual(chat['title'], 'The invoice was charged twice.')
        self.assertEqual(DecisionSettings.from_stored(chat['options']), SETTINGS)
        self.assertEqual([m['uid'] for m in self.db.chats.get_messages('session', include_ids=True)],
                         ['run-1', 'run-2', 'run-3'])
        self.assertGreaterEqual(chat['updated_at'], third['created_at'])
        self.assertEqual(self.count('chats'), 1)

    def test_a_deleted_session_is_never_recreated(self):
        self.store.create_decision_run(new_run())
        self.db.chats.delete_chat('session')
        self.assertIsNone(self.store.create_decision_run(new_run('run-2', new=False)))
        self.assertIsNone(self.store.finish_decision_run('run-1', 'done', result=result()))
        self.assertEqual([self.count(table) for table in ('chats', 'messages', 'decision_runs')], [0, 0, 0])
        # Nor does a run for a session that was never saved, unless it says it is the first.
        self.assertIsNone(self.store.create_decision_run(new_run('run-3', 'other', new=False)))
        self.assertEqual(self.count('chats'), 0)

    def test_a_late_run_keeps_its_draft_when_the_session_is_gone(self):
        self.db.workspace.save_draft(dict(id='draft', mode='decision', text='kept', settings={}, revision=1))
        self.assertIsNone(self.store.create_decision_run(new_run(new=False, draft_id='draft', draft_revision=1)))
        self.assertEqual(self.db.workspace.get_draft('draft')['text'], 'kept')

    def test_deleting_a_session_removes_its_runs_messages_and_images(self):
        self.store.create_decision_run(new_run(images=[PNG]))
        self.store.create_decision_run(new_run('run-2', new=False))
        self.store.create_decision_run(new_run('other-run', 'other', images=[JPEG]))
        self.db.workspace.save_draft(dict(id='session', mode='decision', chat_id='session', text='next', settings={}, revision=1))
        self.db.chats.delete_chat('session')
        self.assertEqual([self.count(table) for table in ('chats', 'messages', 'message_images', 'decision_runs', 'drafts')],
                         [1, 1, 1, 1, 0])
        self.assertEqual(self.store.decision_runs('session'), [])
        self.assertEqual(self.store.decision_runs('other')[0]['images'], [JPEG])
        self.assertEqual(self.db.workspace.list_history('invoice')[0]['id'], 'other')

    def test_runs_left_running_are_interrupted_at_startup(self):
        self.store.create_decision_run(new_run())
        self.store.create_decision_run(new_run('run-2', new=False))
        self.store.finish_decision_run('run-1', 'done', result=result())
        self.db.close()
        storage = ChatStorage(self.temp.name)
        def stop():
            storage.writer.flush().result(5)
            # A future completes immediately before the drain thread becomes idle.
            storage.writer._executor.submit(lambda: None).result(5)
            storage.writer.shutdown()
            storage.close()
        self.addCleanup(stop)
        runs = storage.library.decision_runs('session')
        self.assertEqual([(run['status'], run['finished_at'] is None) for run in runs], [('done', False), ('interrupted', True)])
        self.assertEqual(runs[0]['result'], result())
        # An interrupted run stays so; the answer it missed is not recorded later.
        self.assertEqual(storage.library.finish_decision_run('run-2', 'done', result=result())['status'], 'interrupted')
        saved = storage.submit(storage.library.create_decision_run, new_run('run-3', new=False)).result(5)
        self.assertEqual((saved['position'], saved['status']), (2, 'running'))
        self.assertEqual(storage.submit(storage.library.finish_decision_run, 'run-3', 'cancelled').result(5)['status'], 'cancelled')

    def test_a_page_holds_the_session_form_and_its_runs(self):
        self.store.create_decision_run(new_run(images=[PNG]))
        self.store.finish_decision_run('run-1', 'done', result=result())
        self.store.create_decision_run(new_run('run-2', new=False, state='[1, 2]', state_mode='json'))
        self.store.finish_decision_run('run-2', 'failed', error='No such model.')
        page = self.db.workspace.conversation_page('session')
        self.assertEqual((page['kind'], page['title'], page['messages']), ('decision', 'The invoice was charged twice.', []))
        self.assertEqual((page['message_count'], page['message_offset'], page['paged']), (2, 0, True))
        self.assertEqual(DecisionSettings.from_stored(page['options']),
                         DecisionSettings(SETTINGS.target, QUESTIONS, 'json'))
        self.assertEqual(page['runs'], self.store.decision_runs('session'))
        first, second = page['runs']
        self.assertEqual((first['state_mode'], first['images'], first['status'], first['result']), ('text', [PNG], 'done', result()))
        self.assertEqual((second['state'], second['state_mode'], second['images'], second['status'], second['error']),
                         ('[1, 2]', 'json', [], 'failed', 'No such model.'))
        snapshot = self.db.workspace.export_snapshot('session')
        self.assertEqual((snapshot['runs'], snapshot['messages'], snapshot['targets']), (page['runs'], [], []))
        self.assertIsNone(self.db.workspace.conversation_page('missing'))

    def test_a_long_session_is_paged_like_messages(self):
        for index in range(55):
            self.store.create_decision_run(new_run(f'run-{index}', new=index == 0, state=f'state {index}'))
        page = self.db.workspace.conversation_page('session')
        self.assertEqual((page['message_count'], page['message_offset'], len(page['runs'])), (55, 5, 50))
        self.assertEqual((page['runs'][0]['id'], page['runs'][-1]['id']), ('run-5', 'run-54'))
        page = self.db.workspace.conversation_page('session', 'run-20')
        self.assertEqual((page['message_offset'], page['runs'][10]['id']), (10, 'run-20'))
        earlier = self.store.decision_runs('session', limit=5, offset=0)
        self.assertEqual([run['position'] for run in earlier], [0, 1, 2, 3, 4])
        self.assertEqual(len(self.db.workspace.export_snapshot('session')['runs']), 55)

    def test_a_draft_with_state_or_questions_is_saved_work_and_an_untouched_one_is_not(self):
        empty = DecisionSettings(SETTINGS.target).to_stored()
        untouched = dict(id='untouched', mode='decision', text='  ', settings=empty, revision=1)
        asked = dict(id='asked', mode='decision', text='', settings=SETTINGS.to_stored(), revision=1)
        written = dict(id='written', mode='decision', text='some state', settings=empty, revision=1)
        pictured = dict(id='pictured', mode='decision', text='', settings=empty, images=[PNG], revision=1)
        self.assertFalse(has_saved_work(draft=untouched))
        for draft in (asked, written, pictured):
            self.assertTrue(has_saved_work(draft=draft))
        for draft in (untouched, asked, written, pictured):
            self.db.workspace.save_draft(draft)
        self.assertEqual({row['id'] for row in self.db.workspace.list_drafts()}, {'asked', 'written', 'pictured'})
        groups = history.sidebar_snapshot(self.db.connections)['groups']
        self.assertEqual({row['id'] for row in groups['drafts']}, {'asked', 'written', 'pictured'})
        self.assertEqual({row['mode'] for row in groups['drafts']}, {'decision'})
        saved = self.db.workspace.get_draft('asked')
        self.assertEqual(DecisionSettings.from_stored(saved['settings']), SETTINGS)
        self.assertEqual(self.db.workspace.get_draft('pictured')['images'], [PNG])
        self.assertEqual([row['id'] for row in history.sidebar_snapshot(self.db.connections, 'some st')['groups']['drafts']],
                         ['written'])

    def test_a_run_consumes_the_draft_it_was_made_from(self):
        draft = dict(id='draft', mode='decision', text='state', settings=SETTINGS.to_stored(), images=[PNG], revision=3)
        self.db.workspace.save_draft(draft)
        self.store.create_decision_run(new_run(draft_id='draft', draft_revision=2))
        self.assertEqual(self.db.workspace.get_draft('draft')['images'], [PNG])
        self.store.create_decision_run(new_run('run-2', new=False, draft_id='draft', draft_revision=3))
        self.assertIsNone(self.db.workspace.get_draft('draft'))
        # A draft for the next run belongs to the session, and is not listed apart from it.
        self.db.workspace.save_draft(dict(draft, id='session', chat_id='session', text='next state'))
        self.assertEqual(self.db.workspace.get_draft('session')['text'], 'next state')
        self.assertEqual(self.db.workspace.list_drafts(), [])

    def test_search_finds_a_run_by_its_state(self):
        self.store.create_decision_run(new_run())
        self.store.create_decision_run(new_run('run-2', new=False, state='A zeppelin landed.'))
        self.db.chats.create_chat('chat', 'New Chat', 0, 0, '')
        found = self.db.workspace.list_history('zeppel')
        self.assertEqual([(row['id'], row['kind'], row['match_uid']) for row in found], [('session', 'decision', 'run-2')])
        self.assertIn('zeppelin', found[0]['snippet'])
        self.assertEqual(self.db.workspace.list_history('please refund')[0]['match_uid'], 'run-1')
        self.db.chats.update_chat_title('session', 'Renamed quartz', 5)
        self.assertEqual(self.db.workspace.list_history('quartz')[0]['id'], 'session')
        self.assertEqual(self.db.workspace.list_history('refund')[0]['title'], 'Renamed quartz')

    def test_cleanup_keeps_sessions_with_runs_and_removes_those_without(self):
        self.store.create_decision_run(new_run())
        self.store.create_decision_run(new_run('other-run', 'emptied'))
        self.store.create_decision_run(new_run('pinned-run', 'pinned'))
        self.db.chats.update_chat_pinned('pinned', True)
        with self.db._get_conn() as conn:
            conn.execute("DELETE FROM messages WHERE uid IN ('other-run', 'pinned-run')")
            conn.commit()
        self.assertEqual(self.count('decision_runs'), 1)
        self.db.chats.cleanup_empty_chats('session')
        self.db.chats.cleanup_empty_chats()
        self.assertEqual({chat['id'] for chat in self.db.chats.get_all_chats()}, {'session', 'pinned'})
        self.assertEqual(len(self.store.decision_runs('session')), 1)

    def test_history_lists_a_session_under_its_own_kind(self):
        self.store.create_decision_run(new_run())
        self.store.create_decision_run(new_run('kept-run', 'kept', state='Kept apart'))
        self.db.chats.create_chat('chat', 'A chat', 0, 0, '')
        self.assertEqual({row['id']: row['kind'] for row in self.db.workspace.list_history()},
                         dict(session='decision', kept='decision', chat='chat'))
        groups = history.sidebar_snapshot(self.db.connections)['groups']
        self.assertEqual([(row['id'], row['kind'], row['title']) for row in groups['decision']],
                         [('kept', 'decision', 'Kept apart'), ('session', 'decision', 'The invoice was charged twice.')])
        self.assertEqual([row['id'] for row in groups['chat']], ['chat'])
        self.assertEqual(history.category(groups['decision'][0]), 'decision')
        found = history.sidebar_snapshot(self.db.connections, 'refund')
        self.assertEqual([(row['id'], row['match_uid']) for row in found['groups']['decision']], [('session', 'run-1')])
        self.assertEqual(history.sidebar_snapshot(self.db.connections, 'kept')['suggestions'][0]['kind'], 'decision')
        self.db.chats.update_chat_pinned('kept', True)
        groups = history.sidebar_snapshot(self.db.connections)['groups']
        self.assertEqual(([row['id'] for row in groups['pinned']], [row['id'] for row in groups['decision']]),
                         (['kept'], ['session']))
        self.assertEqual(history.category(groups['pinned'][0]), 'pinned')


class DecisionExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = DatabaseManager(str(Path(self.temp.name) / 'gnollama.db'))
        self.addCleanup(self.db.close)
        store = self.db.decisions
        store.create_decision_run(new_run(images=[PNG]))
        store.finish_decision_run('run-1', 'done', result=result())
        store.create_decision_run(new_run('run-2', new=False, state='{"a": "x|y"}', state_mode='json'))
        store.finish_decision_run('run-2', 'failed', error='HTTP 404: No such model.')
        store.create_decision_run(new_run('run-3', new=False, state='stopped'))
        store.finish_decision_run('run-3', 'cancelled')
        self.snapshot = self.db.workspace.export_snapshot('session')

    def test_markdown_shows_each_run_with_its_questions_and_answers(self):
        text = export_markdown(self.snapshot)
        lines = text.splitlines()
        self.assertEqual(lines[0], '# The invoice was charged twice.')
        self.assertEqual([line for line in lines if line.startswith('## ')],
                         [f'## Run {n} · typesafe/jev-1.14 · Router' for n in (1, 2, 3)])
        self.assertEqual([line for line in lines if line.startswith('Status: ')],
                         ['Status: done', 'Status: failed', 'Status: cancelled'])
        self.assertIn('The invoice was charged twice.\nPlease refund.\n\n![Attachment 1](data:image/png;base64,' + PNG + ')', text)
        self.assertIn('```json\n{"a": "x|y"}\n```', text)
        self.assertIn('**billing** (noul)\n\nIs this about billing?\n', text)
        self.assertIn('**tone** (choice)\n\n- angry: Hostile\n- calm: null\n- excited: Eager\n', text)
        self.assertIn('**urgency** (score)\n\nHow soon?\n\n- 0: Can wait\n- 1: Needs attention this week\n- 2: Needs attention today\n', text)
        self.assertIn('**where** (box)\n\nThe logo\n', text)
        self.assertEqual([line for line in lines if line.startswith('| ')], [
            '| Question | Type | Answer | Confidence | Details |',
            '| billing | noul | 99.3% yes |  |  |',
            '| tone | choice | calm | 67.8% | Probabilities: calm 89.1%, angry 10.8%, excited 0.2% |',
            '| urgency | score | 1.54 | 26.4% | Legend: 0 = Can wait; 1 = Needs attention this week; 2 = Needs attention today'
            '<br>Probabilities: 2 57.8%, 1 38.4%, 0 3.8% |',
            '| where | box | top 0.160, bottom 0.620, left 0.170, right 0.700 | 76.0% |  |',
            '| password | refusal | Refused |  |  |',
            '| late | missing | No answer |  |  |',
            '| odd | invalid | Invalid answer |  | {"type": "noul", "noul": 7} |'])
        self.assertIn('Model: typesafe/jev-1.14 · Input tokens: 449 · Output tokens: 0 · Cost: $0.000449', text)
        self.assertEqual([line for line in lines if line.startswith('Error: ')], ['Error: HTTP 404: No such model.'])
        self.assertEqual(text.count('### Answers'), 1)

    def test_markdown_reports_a_result_that_cannot_be_read(self):
        self.snapshot['runs'][0]['result'] = {'model': 'x'}
        self.assertIn('Error: The response has no answers.', export_markdown(self.snapshot))

    def test_json_keeps_runs_whole(self):
        exported = json.loads(export_json(self.snapshot))['conversation']
        self.assertEqual(exported['kind'], 'decision')
        self.assertEqual(exported['runs'], self.snapshot['runs'])
        self.assertIn('password', exported['runs'][0]['questions'])
        self.assertIn('password', exported['runs'][0]['result']['answers'])
        self.assertIn('password', exported['options']['questions'])
        self.assertEqual(exported['runs'][0]['images'], [PNG])


class DecisionSettingsTests(unittest.TestCase):
    def test_settings_survive_storage_and_read_what_is_missing_as_defaults(self):
        self.assertEqual(DecisionSettings.from_stored(json.loads(json.dumps(SETTINGS.to_stored()))), SETTINGS)
        self.assertEqual(DecisionSettings().to_stored(), dict(questions={}, state_mode='text'))
        self.assertEqual(DecisionSettings.from_stored(None), DecisionSettings())
        self.assertEqual(DecisionSettings.from_stored(dict(questions=[], state_mode='xml', target='x', _configured=True)),
                         DecisionSettings())
        stored = SETTINGS.to_stored()
        stored['questions']['billing']['type'] = 'box'
        self.assertEqual(SETTINGS.questions['billing']['type'], 'noul')


if __name__ == '__main__':
    unittest.main()
