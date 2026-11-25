"""Image sessions in the database: runs, outputs, drafts, history, startup recovery, and export."""
import base64
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import database, history
from src.database import DatabaseManager
from src.export import export_json, export_markdown
from src.image_generation import GeneratedImage, ImageResult
from src.records import ImageSettings, ModelTarget, has_saved_work
from src.storage import ChatStorage

PNG = base64.b64encode(b'\x89PNG\r\n\x1a\n image').decode()
JPEG = base64.b64encode(b'\xff\xd8\xff photo').decode()
GIF = base64.b64encode(b'GIF89a moving').decode()
WEBP = base64.b64encode(b'RIFF\0\0\0\0WEBPVP8 still').decode()
SVG = base64.b64encode(b'<svg xmlns="http://www.w3.org/2000/svg"></svg>').decode()
SETTINGS = ImageSettings(ModelTarget('host-1', 'https://openrouter.ai/api', 'painter/brush-2', 'Router'),
                         {'aspect_ratio': '16:9', 'n': 2, 'seed': 7})
RESULT = ImageResult((GeneratedImage(WEBP, 'image/webp'), GeneratedImage(SVG, 'image/svg+xml')),
                     'Here is a lighthouse.', 0.0425, {'id': 'gen-1', 'usage': {'cost': 0.0425}})


def new_run(id='run-1', session='session', prompt='A lighthouse at dusk\nin watercolour', new=True, **values):
    return dict(SETTINGS.to_stored(), id=id, session_id=session, new_session=new, prompt=prompt, **values)


class ImageStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / 'gnollama.db')
        self.db = DatabaseManager(self.path)
        self.addCleanup(lambda: self.db.close())
        self.store = self.db.images

    def count(self, table):
        with self.db._get_conn() as conn:
            return conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0]

    def test_a_new_database_is_at_version_15_with_the_runs_table(self):
        with self.db._get_conn() as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 15)
            self.assertEqual([row[1] for row in conn.execute('PRAGMA table_info(image_runs)')],
                             ['id', 'session_id', 'position', 'output_id', 'options', 'target', 'status',
                              'result', 'error', 'created_at', 'finished_at'])
        self.assertIsNone(self.db.backup_path)

    def test_upgrade_from_version_14_keeps_what_was_saved(self):
        path = str(Path(self.temp.name) / 'old.db')
        with patch.object(database, 'MIGRATIONS', database.MIGRATIONS[:13]):
            old = DatabaseManager(path)
        old.chats.create_chat('chat', 'Saved chat', 1, 2, 'model')
        messages = [dict(uid='question', role='user', content='kept words', images=[PNG]),
                    dict(uid='answer', role='assistant', content='reply', response_metadata={'status': 'complete'})]
        old.chats.save_chat('chat', messages, options={'temperature': 0.5})
        old.decisions.create_decision_run(dict(id='decision', session_id='decided', new_session=True, state='a state',
                                               questions={'spam': {'type': 'noul'}}, target={}))
        old.workspace.save_draft(dict(id='draft', mode='chat', text='unsent', settings={}, revision=1))
        with old._get_conn() as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 14)
            self.assertIsNone(conn.execute("SELECT 1 FROM sqlite_master WHERE name='image_runs'").fetchone())
        old.close()
        upgraded = DatabaseManager(path)
        self.addCleanup(upgraded.close)
        self.assertIn('.pre-v15-', upgraded.backup_path)
        with sqlite3.connect(upgraded.backup_path) as backup:
            self.assertEqual(backup.execute('PRAGMA user_version').fetchone()[0], 14)
            self.assertEqual(backup.execute("SELECT content FROM messages WHERE uid='question'").fetchone()[0], 'kept words')
        with upgraded._get_conn() as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 15)
        self.assertEqual(upgraded.chats.get_messages('chat', include_ids=True), messages)
        self.assertEqual(upgraded.decisions.decision_runs('decided')[0]['state'], 'a state')
        self.assertEqual(upgraded.workspace.get_draft('draft')['text'], 'unsent')
        self.assertEqual(upgraded.workspace.list_history('kept')[0]['match_uid'], 'question')
        self.assertEqual(upgraded.images.create_image_run(new_run())['status'], 'running')
        reopened = DatabaseManager(path)
        self.addCleanup(reopened.close)
        self.assertIsNone(reopened.backup_path)

    def test_a_failed_upgrade_leaves_version_14_and_a_backup(self):
        path = str(Path(self.temp.name) / 'old.db')
        with patch.object(database, 'MIGRATIONS', database.MIGRATIONS[:13]):
            DatabaseManager(path).close()
        broken = database.MIGRATIONS[:13] + [database.MIGRATIONS[13] + 'CREATE TABLE image_runs(id TEXT);']
        with patch.object(database, 'MIGRATIONS', broken):
            with self.assertRaises(database.DatabaseUpgradeError) as caught:
                DatabaseManager(path)
        self.assertTrue(Path(caught.exception.backup_path).exists())
        with sqlite3.connect(path) as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 14)
            self.assertIsNone(conn.execute("SELECT 1 FROM sqlite_master WHERE name='image_runs'").fetchone())
        DatabaseManager(path).close()

    def test_the_first_run_creates_its_session_with_what_was_entered(self):
        run = self.store.create_image_run(new_run(references=[PNG, 'data:image/jpeg;base64,' + JPEG]))
        self.assertEqual({key: run[key] for key in ('id', 'session_id', 'position', 'prompt', 'references', 'status',
                                                    'images', 'text', 'result', 'error', 'finished_at')},
                         dict(id='run-1', session_id='session', position=0, prompt='A lighthouse at dusk\nin watercolour',
                              references=[PNG, JPEG], status='running', images=[], text='', result=None, error=None,
                              finished_at=None))
        self.assertEqual(ImageSettings.from_stored(run), SETTINGS)
        chat = self.db.chats.get_chat('session')
        self.assertEqual((chat['kind'], chat['title']), ('image', 'A lighthouse at dusk'))
        self.assertEqual(ImageSettings.from_stored(chat['options']), SETTINGS)
        self.assertEqual(chat['created_at'], run['created_at'])
        self.assertEqual(self.db.chats.get_messages('session', include_ids=True),
                         [dict(uid='run-1', role='user', content=run['prompt'], images=[PNG, JPEG])])
        # Saving the same run again changes nothing.
        self.assertEqual(self.store.create_image_run(new_run(prompt='changed')), run)
        self.assertEqual((self.count('messages'), self.count('image_runs')), (1, 1))

    def test_a_run_without_a_target_keeps_settings_without_one(self):
        self.store.create_image_run(dict(ImageSettings(options={'n': 1}).to_stored(), id='run-1', session_id='session',
                                         new_session=True, prompt='  \n'))
        chat = self.db.chats.get_chat('session')
        self.assertEqual((chat['title'], chat['options']), ('Image', dict(options={'n': 1})))
        self.assertEqual(ImageSettings.from_stored(chat['options']), ImageSettings(options={'n': 1}))

    def test_a_finished_run_holds_its_images_and_text_in_an_answer(self):
        self.store.create_image_run(new_run())
        done = self.store.finish_image_run('run-1', 'done', RESULT)
        self.assertEqual((done['status'], done['error'], done['text']), ('done', None, 'Here is a lighthouse.'))
        self.assertEqual(done['images'], [dict(data=WEBP, media_type='image/webp'), dict(data=SVG, media_type='image/svg+xml')])
        self.assertEqual(done['result'], dict(media_types=['image/webp', 'image/svg+xml'], cost=0.0425,
                                              extra={'id': 'gen-1', 'usage': {'cost': 0.0425}}))
        self.assertGreaterEqual(done['finished_at'], done['created_at'])
        messages = self.db.chats.get_messages('session', include_ids=True)
        self.assertEqual([(m['role'], m['content'], m.get('model'), m.get('images')) for m in messages],
                         [('user', 'A lighthouse at dusk\nin watercolour', None, None),
                          ('assistant', 'Here is a lighthouse.', 'painter/brush-2', [WEBP, SVG])])
        with self.db._get_conn() as conn:
            self.assertEqual(conn.execute('SELECT output_id FROM image_runs').fetchone()[0], messages[1]['uid'])
        # A later outcome does not replace the first.
        self.assertEqual(self.store.finish_image_run('run-1', 'failed', error='late'), done)
        self.assertEqual(self.count('messages'), 2)
        self.assertIsNone(self.store.finish_image_run('unknown', 'done', RESULT))
        with self.assertRaises(ValueError):
            self.store.finish_image_run('run-1', 'running')

    def test_a_run_without_images_keeps_what_the_model_said(self):
        for id in ('text', 'failed', 'cancelled', 'empty'):
            self.store.create_image_run(new_run(id, new=id == 'text'))
        said = self.store.finish_image_run('text', 'failed', ImageResult((), 'I cannot draw that.', None),
                                           'The server did not return an image.')
        self.assertEqual((said['status'], said['images'], said['text'], said['error'], said['result']),
                         ('failed', [], 'I cannot draw that.', 'The server did not return an image.',
                          dict(media_types=[], cost=None, extra={})))
        failed = self.store.finish_image_run('failed', 'failed', error='HTTP 402: Payment required.')
        self.assertEqual((failed['text'], failed['result'], failed['error']), ('', None, 'HTTP 402: Payment required.'))
        cancelled = self.store.finish_image_run('cancelled', 'cancelled', error='ignored')
        self.assertEqual((cancelled['status'], cancelled['error']), ('cancelled', None))
        empty = self.store.finish_image_run('empty', 'done', ImageResult(()))
        self.assertEqual((empty['images'], empty['text']), ([], ''))
        # Only the run whose model wrote something has an answer.
        self.assertEqual(self.count('messages'), 5)
        self.assertEqual([run['status'] for run in self.store.image_runs('session')], ['failed', 'failed', 'cancelled', 'done'])

    def test_images_that_are_not_base64_save_nothing(self):
        self.store.create_image_run(new_run())
        with self.assertRaises(ValueError):
            self.store.finish_image_run('run-1', 'done', ImageResult((GeneratedImage('!', 'image/png'),)))
        self.assertEqual(self.store.image_runs('session')[0]['status'], 'running')
        self.assertEqual(self.count('messages'), 1)
        with self.assertRaises(ValueError):
            self.store.create_image_run(new_run('bad', new=False, references=['!']))
        self.assertEqual((self.count('messages'), self.count('image_runs')), (1, 1))

    def test_an_image_whose_type_was_not_recorded_is_told_from_its_bytes(self):
        self.store.create_image_run(new_run())
        self.store.finish_image_run('run-1', 'done', ImageResult((GeneratedImage(GIF, 'image/gif'), GeneratedImage(JPEG, 'image/jpeg'))))
        with self.db._get_conn() as conn:
            conn.execute('''UPDATE image_runs SET result='{"media_types": ["image/gif"], "cost": null, "extra": {}}' ''')
            conn.commit()
        self.assertEqual([image['media_type'] for image in self.store.image_runs('session')[0]['images']],
                         ['image/gif', 'image/jpeg'])

    def test_later_runs_join_the_session_in_order_and_update_its_settings(self):
        first = self.store.create_image_run(new_run())
        self.store.finish_image_run('run-1', 'done', RESULT)
        changed = ImageSettings(ModelTarget('host-2', 'https://ai-gateway.vercel.sh', 'other/model', 'Gateway'),
                                {'resolution': '2K'})
        second = self.store.create_image_run(dict(changed.to_stored(), id='run-2', session_id='session', prompt='second'))
        third = self.store.create_image_run(new_run('run-3', new=False, prompt='third'))
        self.assertEqual([run['position'] for run in (first, second, third)], [0, 1, 2])
        runs = self.store.image_runs('session')
        self.assertEqual([(run['id'], run['prompt']) for run in runs],
                         [('run-1', first['prompt']), ('run-2', 'second'), ('run-3', 'third')])
        self.assertEqual(ImageSettings.from_stored(runs[1]), changed)
        self.assertEqual(self.store.image_runs('session', limit=1, offset=1), [second])
        chat = self.db.chats.get_chat('session')
        self.assertEqual(chat['title'], 'A lighthouse at dusk')
        self.assertEqual(ImageSettings.from_stored(chat['options']), SETTINGS)
        self.assertEqual([m['role'] for m in self.db.chats.get_messages('session')], ['user', 'assistant', 'user', 'user'])
        self.assertEqual(self.count('chats'), 1)

    def test_a_deleted_session_is_never_recreated(self):
        self.store.create_image_run(new_run())
        self.db.chats.delete_chat('session')
        self.assertIsNone(self.store.create_image_run(new_run('run-2', new=False)))
        self.assertIsNone(self.store.finish_image_run('run-1', 'done', RESULT))
        self.assertEqual([self.count(table) for table in ('chats', 'messages', 'image_runs')], [0, 0, 0])
        self.assertIsNone(self.store.create_image_run(new_run('run-3', 'other', new=False)))
        self.assertEqual(self.count('chats'), 0)

    def test_a_late_run_keeps_its_draft_when_the_session_is_gone(self):
        self.db.workspace.save_draft(dict(id='draft', mode='image', text='kept', settings={}, revision=1))
        self.assertIsNone(self.store.create_image_run(new_run(new=False, draft_id='draft', draft_revision=1)))
        self.assertEqual(self.db.workspace.get_draft('draft')['text'], 'kept')

    def test_deleting_a_session_removes_its_runs_messages_and_images(self):
        self.store.create_image_run(new_run(references=[PNG]))
        self.store.finish_image_run('run-1', 'done', RESULT)
        self.store.create_image_run(new_run('run-2', new=False))
        self.store.create_image_run(new_run('other-run', 'other', references=[JPEG]))
        self.store.finish_image_run('other-run', 'done', ImageResult((GeneratedImage(GIF, 'image/gif'),)))
        self.db.workspace.save_draft(dict(id='session', mode='image', chat_id='session', text='next', settings={}, revision=1))
        self.db.chats.delete_chat('session')
        self.assertEqual([self.count(t) for t in ('chats', 'messages', 'message_images', 'image_runs', 'drafts')],
                         [1, 2, 2, 1, 0])
        self.assertEqual(self.store.image_runs('session'), [])
        other = self.store.image_runs('other')[0]
        self.assertEqual((other['references'], other['images']), ([JPEG], [dict(data=GIF, media_type='image/gif')]))

    def test_deleting_an_answer_leaves_its_run_without_outputs(self):
        self.store.create_image_run(new_run(references=[PNG]))
        output = self.store.finish_image_run('run-1', 'done', RESULT)
        with self.db._get_conn() as conn:
            conn.execute("DELETE FROM messages WHERE role='assistant'")
            conn.commit()
            self.assertIsNone(conn.execute('SELECT output_id FROM image_runs').fetchone()[0])
        run = self.store.image_runs('session')[0]
        self.assertEqual((run['status'], run['references'], run['images'], run['text'], run['result']),
                         ('done', [PNG], [], '', output['result']))
        self.assertEqual(self.count('message_images'), 1)
        # Deleting the prompt removes the run with it.
        with self.db._get_conn() as conn:
            conn.execute("DELETE FROM messages WHERE uid='run-1'")
            conn.commit()
        self.assertEqual((self.count('image_runs'), self.count('message_images')), (0, 0))

    def test_runs_left_running_are_interrupted_at_startup(self):
        self.store.create_image_run(new_run())
        self.store.create_image_run(new_run('run-2', new=False))
        self.store.finish_image_run('run-1', 'done', RESULT)
        self.db.close()
        storage = ChatStorage(self.temp.name)
        def stop():
            storage.writer.flush().result(5)
            # A future completes immediately before the drain thread becomes idle.
            storage.writer._executor.submit(lambda: None).result(5)
            storage.writer.shutdown()
            storage.close()
        self.addCleanup(stop)
        runs = storage.library.image_runs('session')
        self.assertEqual([(run['status'], run['finished_at'] is None) for run in runs], [('done', False), ('interrupted', True)])
        # An interrupted run stays so; the images it missed are not recorded later.
        late = storage.library.finish_image_run('run-2', 'done', RESULT)
        self.assertEqual((late['status'], late['images']), ('interrupted', []))
        saved = storage.submit(storage.library.create_image_run, new_run('run-3', new=False)).result(5)
        self.assertEqual((saved['position'], saved['status']), (2, 'running'))
        self.assertEqual(storage.submit(storage.library.finish_image_run, 'run-3', 'cancelled').result(5)['status'], 'cancelled')

    def test_a_page_holds_the_session_settings_and_its_runs(self):
        self.store.create_image_run(new_run(references=[PNG]))
        self.store.finish_image_run('run-1', 'done', RESULT)
        self.store.create_image_run(new_run('run-2', new=False, prompt='second'))
        self.store.finish_image_run('run-2', 'failed', error='No such model.')
        page = self.db.workspace.conversation_page('session')
        self.assertEqual((page['kind'], page['title'], page['messages']), ('image', 'A lighthouse at dusk', []))
        self.assertEqual((page['message_count'], page['message_offset'], page['paged']), (2, 0, True))
        self.assertEqual(ImageSettings.from_stored(page['options']), SETTINGS)
        self.assertEqual(page['runs'], self.store.image_runs('session'))
        snapshot = self.db.workspace.export_snapshot('session')
        self.assertEqual((snapshot['runs'], snapshot['messages'], snapshot['targets']), (page['runs'], [], []))

    def test_a_long_session_is_paged_by_run(self):
        for index in range(55):
            self.store.create_image_run(new_run(f'run-{index}', new=index == 0, prompt=f'prompt {index}'))
            self.store.finish_image_run(f'run-{index}', 'done', ImageResult((), f'answer {index}'))
        self.assertEqual(self.count('messages'), 110)
        page = self.db.workspace.conversation_page('session')
        self.assertEqual((page['message_count'], page['message_offset'], len(page['runs'])), (55, 5, 50))
        self.assertEqual((page['runs'][0]['id'], page['runs'][-1]['id']), ('run-5', 'run-54'))
        page = self.db.workspace.conversation_page('session', 'run-20')
        self.assertEqual((page['message_offset'], page['runs'][10]['id']), (10, 'run-20'))
        # A match in what the model wrote opens its run.
        answer = self.db.workspace.list_history('answer 30')[0]['match_uid']
        page = self.db.workspace.conversation_page('session', answer)
        self.assertEqual((page['message_offset'], page['runs'][10]['id']), (20, 'run-30'))
        self.assertEqual(self.db.workspace.conversation_page('session', 'missing')['message_offset'], 5)
        self.assertEqual(len(self.db.workspace.export_snapshot('session')['runs']), 55)

    def test_a_draft_with_a_prompt_or_references_is_saved_work_and_an_untouched_one_is_not(self):
        settings = SETTINGS.to_stored()
        untouched = dict(id='untouched', mode='image', text='  ', settings=settings, revision=1)
        written = dict(id='written', mode='image', text='a red fox', settings=settings, revision=1)
        pictured = dict(id='pictured', mode='image', text='', settings=settings, images=[PNG], revision=1)
        self.assertFalse(has_saved_work(draft=untouched))
        for draft in (written, pictured):
            self.assertTrue(has_saved_work(draft=draft))
        for draft in (untouched, written, pictured):
            self.db.workspace.save_draft(draft)
        self.assertEqual({row['id'] for row in self.db.workspace.list_drafts()}, {'written', 'pictured'})
        groups = history.sidebar_snapshot(self.db.connections)['groups']
        self.assertEqual({(row['id'], row['mode'], row['title']) for row in groups['drafts']},
                         {('written', 'image', 'a red fox'), ('pictured', 'image', '')})
        self.assertEqual(ImageSettings.from_stored(self.db.workspace.get_draft('written')['settings']), SETTINGS)
        self.assertEqual(self.db.workspace.get_draft('pictured')['images'], [PNG])
        self.assertEqual([row['id'] for row in history.sidebar_snapshot(self.db.connections, 'red f')['groups']['drafts']],
                         ['written'])

    def test_a_run_consumes_the_draft_it_was_made_from(self):
        draft = dict(id='draft', mode='image', text='prompt', settings=SETTINGS.to_stored(), images=[PNG], revision=3)
        self.db.workspace.save_draft(draft)
        self.store.create_image_run(new_run(draft_id='draft', draft_revision=2))
        self.assertEqual(self.db.workspace.get_draft('draft')['images'], [PNG])
        self.store.create_image_run(new_run('run-2', new=False, draft_id='draft', draft_revision=3))
        self.assertIsNone(self.db.workspace.get_draft('draft'))
        # A draft for the next run belongs to the session, and is not listed apart from it.
        self.db.workspace.save_draft(dict(draft, id='session', chat_id='session', text='next prompt'))
        self.assertEqual(self.db.workspace.get_draft('session')['text'], 'next prompt')
        self.assertEqual(self.db.workspace.list_drafts(), [])

    def test_search_finds_a_run_by_its_prompt_or_what_the_model_said(self):
        self.store.create_image_run(new_run())
        self.store.finish_image_run('run-1', 'done', RESULT)
        self.store.create_image_run(new_run('run-2', new=False, prompt='A zeppelin over the bay'))
        self.db.chats.create_chat('chat', 'New Chat', 0, 0, '')
        found = self.db.workspace.list_history('zeppel')
        self.assertEqual([(row['id'], row['kind'], row['match_uid']) for row in found], [('session', 'image', 'run-2')])
        self.assertIn('zeppelin', found[0]['snippet'])
        self.assertEqual(self.db.workspace.list_history('watercolour')[0]['match_uid'], 'run-1')
        self.assertEqual(self.db.workspace.list_history('here is')[0]['id'], 'session')

    def test_history_lists_a_session_under_its_own_kind(self):
        self.store.create_image_run(new_run())
        self.db.chats.create_chat('chat', 'A chat', 0, 0, '')
        self.assertEqual({(row['id'], row['kind'], row['title']) for row in self.db.workspace.list_history()},
                         {('session', 'image', 'A lighthouse at dusk'), ('chat', 'chat', 'A chat')})
        # The sidebar is not given the kind until it has a section for it.
        groups = history.sidebar_snapshot(self.db.connections)['groups']
        self.assertEqual([row['id'] for row in groups['chat']], ['chat'])

    def test_cleanup_keeps_sessions_with_runs_and_removes_those_without(self):
        self.store.create_image_run(new_run())
        self.store.create_image_run(new_run('other-run', 'emptied'))
        self.store.create_image_run(new_run('pinned-run', 'pinned'))
        self.db.chats.update_chat_pinned('pinned', True)
        with self.db._get_conn() as conn:
            conn.execute("DELETE FROM messages WHERE uid IN ('other-run', 'pinned-run')")
            conn.commit()
        self.assertEqual(self.count('image_runs'), 1)
        self.db.chats.cleanup_empty_chats('session')
        self.db.chats.cleanup_empty_chats()
        self.assertEqual({chat['id'] for chat in self.db.chats.get_all_chats()}, {'session', 'pinned'})
        self.assertEqual(len(self.store.image_runs('session')), 1)


class ImageExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = DatabaseManager(str(Path(self.temp.name) / 'gnollama.db'))
        self.addCleanup(self.db.close)
        store = self.db.images
        store.create_image_run(new_run(references=[PNG, JPEG]))
        store.finish_image_run('run-1', 'done', ImageResult(
            RESULT.images + (GeneratedImage(GIF, 'image/gif'),), RESULT.text, RESULT.cost, RESULT.extra))
        store.create_image_run(dict(ImageSettings(SETTINGS.target).to_stored(), id='run-2', session_id='session',
                                    prompt='second'))
        store.finish_image_run('run-2', 'failed', error='HTTP 404: No such model.')
        store.create_image_run(new_run('run-3', new=False, prompt='stopped'))
        store.finish_image_run('run-3', 'cancelled')
        self.snapshot = self.db.workspace.export_snapshot('session')

    def test_markdown_shows_each_run_with_its_prompt_settings_and_images(self):
        text = export_markdown(self.snapshot)
        lines = text.splitlines()
        self.assertEqual(lines[0], '# A lighthouse at dusk')
        self.assertEqual([line for line in lines if line.startswith('## ')],
                         [f'## Run {n} · painter/brush-2 · Router' for n in (1, 2, 3)])
        self.assertEqual([line for line in lines if line.startswith('Status: ')],
                         ['Status: done', 'Status: failed', 'Status: cancelled'])
        self.assertIn('### Prompt\n\nA lighthouse at dusk\nin watercolour\n\n'
                      f'![Reference 1](data:image/png;base64,{PNG})\n\n![Reference 2](data:image/jpeg;base64,{JPEG})\n', text)
        self.assertIn('### Settings\n\n- Aspect ratio: 16:9\n- Number of images: 2\n- Seed: 7\n', text)
        self.assertIn(f'### Result\n\nHere is a lighthouse.\n\n![Image 1](data:image/webp;base64,{WEBP})\n\n'
                      f'![Image 2](data:image/svg+xml;base64,{SVG})\n\n![Image 3](data:image/gif;base64,{GIF})\n', text)
        self.assertEqual([line for line in lines if line.startswith('Cost: ')], ['Cost: $0.042500'])
        self.assertEqual([line for line in lines if line.startswith('Error: ')], ['Error: HTTP 404: No such model.'])
        self.assertEqual((text.count('### Settings'), text.count('### Result')), (2, 1))

    def test_json_keeps_runs_whole(self):
        exported = json.loads(export_json(self.snapshot))['conversation']
        self.assertEqual(exported['kind'], 'image')
        self.assertEqual(exported['runs'], self.snapshot['runs'])
        self.assertEqual(exported['options'], self.snapshot['options'])
        self.assertEqual(exported['runs'][0]['images'][1], dict(data=SVG, media_type='image/svg+xml'))
        self.assertEqual(exported['runs'][0]['references'], [PNG, JPEG])


class ImageSettingsTests(unittest.TestCase):
    def test_settings_survive_storage_and_read_what_is_missing_as_defaults(self):
        self.assertEqual(ImageSettings.from_stored(json.loads(json.dumps(SETTINGS.to_stored()))), SETTINGS)
        self.assertEqual(ImageSettings().to_stored(), dict(options={}))
        self.assertEqual(ImageSettings.from_stored(None), ImageSettings())
        self.assertEqual(ImageSettings.from_stored(dict(options=[], target='x')), ImageSettings())
        stored = SETTINGS.to_stored()
        stored['options']['n'] = 4
        self.assertEqual(SETTINGS.options['n'], 2)


if __name__ == '__main__':
    unittest.main()
