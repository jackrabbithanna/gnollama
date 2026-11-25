"""Decision runs without widgets: what blocks one, and how one is saved from start to end."""
import json
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from gi.repository import GLib

from src.app_context import AppContext
from src.decision_run import DecisionController, prepare, read_state
from src.providers import OllamaProvider, ProviderError, RequestCancelled
from src.providers.registry import PROVIDERS
from src.records import DecisionSettings, ModelTarget
from provider_fixture import LIMITED, limited_host, provider_patch

ANSWER = json.loads((Path(__file__).parent / 'fixtures' / 'systemone_ollama.json').read_text())
QUESTIONS = {'billing': {'type': 'noul'},
             'tone': {'type': 'choice', 'criteria': {'angry': None, 'calm': None, 'excited': None}},
             'urgency': {'type': 'score', 'criteria': ['low', 'middle', 'high']}}
BLIND = {'name': 'blind', 'capabilities': ['decision']}


def pump_until(predicate, timeout=4):
    deadline = time.monotonic() + timeout
    context = GLib.MainContext.default()
    while time.monotonic() < deadline:
        while context.pending():
            context.iteration(False)
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError('Timed out waiting for background work')


class DecisionRunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.context = AppContext(self.temp.name)
        self.storage = self.context.storage
        self.host = self.storage.get_all_hosts()[0]
        patcher = provider_patch('decide', return_value=ANSWER)
        self.decide = patcher.start()
        self.addCleanup(self.close)
        self.addCleanup(patcher.stop)
        self.events = []
        self.controller = DecisionController(
            self.context, started=lambda run: self.events.append(('started', run['status'])),
            finished=lambda run: self.events.append(('finished', run['status'])))

    def close(self):
        self.controller.stop()
        self.settle()
        self.context.shutdown()
        self.temp.cleanup()

    def settle(self):
        pump_until(lambda: self.controller.request is None and self.context.idle and self.storage.writer.idle)

    def settings(self, questions=QUESTIONS, model='seeing', mode='text', host=None):
        return DecisionSettings(ModelTarget((host or self.host)['id'], model=model), questions, mode)

    def runs(self):
        return self.storage.library.decision_runs(self.controller.session_id)

    def test_a_request_is_resolved_or_refused_with_the_reason(self):
        request = prepare(self.context, self.host, self.settings(), 'state')
        target = request.settings.target
        self.assertEqual((target.host_id, target.url, target.model, target.host_name),
                         (self.host['id'], 'http://localhost:11434', 'seeing', self.host['name']))
        self.assertEqual((request.state, request.text, request.images, request.settings.questions),
                         ('state', 'state', [], QUESTIONS))
        self.assertEqual([question.name for question in request.questions], list(QUESTIONS))
        self.assertEqual(request.connection.url, 'http://localhost:11434')
        refused = [
            (dict(host=None), 'Select an available model'),
            (dict(settings=self.settings(model=None)), 'Select an available model'),
            (dict(settings=DecisionSettings(None, QUESTIONS)), 'Select an available model'),
            (dict(settings=self.settings({})), 'at least one named question'),
            (dict(settings=self.settings({'a': {'type': 'choice'}})), 'between 1 and 255'),
            (dict(settings=self.settings({f'q{n}': {'type': 'noul'} for n in range(65)})), 'at most 64 questions'),
            (dict(settings=self.settings(dict(QUESTIONS, where={'type': 'box'})), images=['aGVsbG8=']), 'box questions'),
            (dict(images=['aGVsbG8='], entry=BLIND), 'does not accept images'),
            (dict(settings=self.settings(mode='json'), text='{"a": '), 'The state is not valid JSON'),
        ]
        for changes, reason in refused:
            values = dict(dict(host=self.host, settings=self.settings(), text='state'), **changes)
            with self.subTest(reason=reason), self.assertRaisesRegex(ValueError, reason):
                prepare(self.context, **values)
        with patch.dict(PROVIDERS, LIMITED):
            limited = limited_host(self.storage)
            with self.assertRaisesRegex(ValueError, 'does not offer decision models'):
                prepare(self.context, limited, self.settings(host=limited), 'state')
            # The host the settings name is the one that is asked.
            with self.assertRaisesRegex(ValueError, 'Select an available model'):
                prepare(self.context, limited, self.settings(), 'state')

    def test_json_state_is_parsed_and_text_is_sent_as_written(self):
        self.assertEqual(read_state(' {"a": [1, null]} ', 'json'), {'a': [1, None]})
        self.assertEqual(read_state('"text"', 'json'), 'text')
        self.assertEqual(read_state(' {"a": 1} ', 'text'), ' {"a": 1} ')
        for text in ('', '{"a": NaN}', '{"a": 1, "a": 2}'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                read_state(text, 'json')

    def test_a_run_is_saved_before_it_is_sent_and_again_when_it_ends(self):
        seen = []
        def decide(provider, model, state, questions, images=None, cancellable=None, **kwargs):
            seen.append((self.runs()[0]['status'], self.context.services.models.busy(self.host, model)))
            return ANSWER
        self.decide.side_effect = decide
        self.storage.save_draft(dict(id='draft', mode='decision', text='written', settings={}, revision=3))
        request = self.controller.start(self.host, self.settings(mode='json'), '{"n": 1}', draft_id='draft', draft_revision=3)
        self.assertIs(self.controller.request, request)
        self.assertIsNone(self.controller.start(self.host, self.settings(), 'again'))
        self.settle()
        self.assertEqual(seen, [('running', True)])
        self.assertEqual(self.events, [('started', 'running'), ('finished', 'done')])
        self.assertEqual(self.decide.call_args.args[1:], ('seeing', {'n': 1}, QUESTIONS))
        run, = self.runs()
        self.assertEqual((run['id'], run['status'], run['result'], run['state'], run['state_mode'], run['error']),
                         (request.id, 'done', ANSWER, '{"n": 1}', 'json', None))
        self.assertEqual(run['target'], dict(host='http://localhost:11434', host_id=self.host['id'],
                                             host_name=self.host['name'], model='seeing'))
        self.assertEqual(self.controller.title, '{"n": 1}')
        self.assertIsNone(self.storage.get_draft('draft'))
        self.assertFalse(self.context.services.models.busy(self.host, 'seeing'))
        # The next run joins the session.
        session = self.controller.session_id
        self.controller.start(self.host, self.settings({'only': {'type': 'noul'}}), 'Second')
        self.settle()
        self.assertEqual((self.controller.session_id, self.controller.title), (session, '{"n": 1}'))
        self.assertEqual([(run['position'], run['state']) for run in self.runs()], [(0, '{"n": 1}'), (1, 'Second')])
        self.assertEqual(self.runs()[1]['result'], ANSWER)

    def test_nothing_is_sent_without_a_state_or_past_a_refusal(self):
        self.assertIsNone(self.controller.start(self.host, self.settings(), ' \n'))
        with self.assertRaises(ValueError):
            self.controller.start(self.host, self.settings({}), 'state')
        self.settle()
        self.assertIsNone(self.controller.session_id)
        self.assertEqual(self.events, [])
        self.decide.assert_not_called()
        self.assertEqual(self.storage.get_all_chats(), [])

    def test_failures_cancellation_and_a_model_in_use_end_the_run_as_such(self):
        self.decide.side_effect = ProviderError('No such model.')
        self.controller.start(self.host, self.settings(), 'state')
        self.settle()
        self.decide.side_effect = None
        self.decide.return_value = {'answers': 'none'}
        self.controller.start(self.host, self.settings(), 'state')
        self.settle()
        self.assertTrue(self.context.services.models.reserve(self.host, 'seeing'))
        self.controller.start(self.host, self.settings(), 'state')
        self.settle()
        self.context.services.models.release(self.host, 'seeing')
        entered = threading.Event()
        def decide(provider, model, state, questions, images=None, cancellable=None, **kwargs):
            entered.set()
            while not cancellable.is_cancelled():
                time.sleep(0.005)
            raise RequestCancelled()
        self.decide.side_effect = decide
        self.controller.start(self.host, self.settings(), 'state')
        pump_until(entered.is_set)
        self.controller.stop()
        self.settle()
        self.assertEqual([(run['status'], run['error'], run['result']) for run in self.runs()], [
            ('failed', 'No such model.', None), ('failed', 'The response has no answers.', None),
            ('failed', 'Wait for the model operation to finish.', None), ('cancelled', None, None)])
        self.assertEqual(self.events[-2:], [('started', 'running'), ('finished', 'cancelled')])
        self.assertEqual(self.controller.error, '')

    def test_images_are_stored_and_sent_as_given_to_a_host_that_reads_any_format(self):
        calls = []
        self.controller.convert = lambda images, formats: calls.append(formats) or ['changed']
        request = self.controller.start(self.host, self.settings(), 'state', ['aGVsbG8=', 'd29ybGQ='])
        self.assertEqual(request.formats, ())
        self.settle()
        self.assertEqual(calls, [])
        self.assertEqual(self.decide.call_args.kwargs['images'], ['aGVsbG8=', 'd29ybGQ='])
        self.assertEqual(self.runs()[0]['images'], ['aGVsbG8=', 'd29ybGQ='])
        # Images alone are a state.
        self.controller.start(self.host, self.settings(), '', ['aGVsbG8='])
        self.settle()
        self.assertEqual([(run['state'], run['images']) for run in self.runs()][1], ('', ['aGVsbG8=']))

    def test_images_are_converted_on_a_worker_before_the_run_is_saved_for_a_host_that_names_formats(self):
        limited = replace(OllamaProvider.capabilities, decision_image_formats=('png', 'jpeg'))
        calls = []
        def convert(images, formats):
            calls.append((list(images), formats, threading.current_thread() is threading.main_thread(),
                          self.controller.session_id))
            return ['cG5n' for image in images]
        self.controller.convert = convert
        with patch.object(OllamaProvider, 'capabilities', limited):
            request = self.controller.start(self.host, self.settings(), 'state', ['d2VicA=='])
            self.assertEqual(request.formats, ('png', 'jpeg'))
            self.assertIs(self.controller.request, request)
            self.settle()
            # Nothing was saved until the images were ready.
            self.assertEqual(calls, [(['d2VicA=='], ('png', 'jpeg'), False, None)])
            self.assertEqual(self.decide.call_args.kwargs['images'], ['cG5n'])
            self.assertEqual((self.runs()[0]['images'], self.runs()[0]['status']), (['cG5n'], 'done'))
            # A run without images has nothing to convert.
            self.controller.start(self.host, self.settings(), 'state')
            self.settle()
            self.assertEqual(len(calls), 1)
            # One that cannot be converted is sent as it is, for the server to refuse.
            self.controller.convert = lambda images, formats: 1 / 0
            self.controller.start(self.host, self.settings(), 'state', ['d2VicA=='])
            self.settle()
            self.assertEqual(self.decide.call_args.kwargs['images'], ['d2VicA=='])
            self.assertEqual(self.runs()[2]['images'], ['d2VicA=='])
            # Without the means to convert, likewise.
            self.controller.convert = None
            self.controller.start(self.host, self.settings(), 'state', ['d2VicA=='])
            self.settle()
            self.assertEqual(self.runs()[3]['images'], ['d2VicA=='])

    def test_a_run_stopped_while_its_images_are_converted_is_neither_saved_nor_sent(self):
        limited = replace(OllamaProvider.capabilities, decision_image_formats=('png', 'jpeg'))
        entered, release = threading.Event(), threading.Event()
        def convert(images, formats):
            entered.set()
            release.wait(4)
            return images
        self.controller.convert = convert
        with patch.object(OllamaProvider, 'capabilities', limited):
            self.controller.start(self.host, self.settings(), 'state', ['d2VicA=='])
            pump_until(entered.is_set)
            self.assertIsNone(self.controller.start(self.host, self.settings(), 'again'))
            self.controller.stop()
            release.set()
            self.settle()
        self.assertEqual((self.events, self.controller.session_id, self.controller.error), ([], None, ''))
        self.decide.assert_not_called()
        self.assertEqual(self.storage.get_all_chats(), [])

    def test_a_deleted_session_is_not_created_again(self):
        self.controller.start(self.host, self.settings(), 'state')
        self.settle()
        session = self.controller.session_id
        self.storage.delete_chat(session)
        self.events.clear()
        self.decide.reset_mock()
        self.controller.start(self.host, self.settings(), 'state')
        self.settle()
        self.assertEqual((self.events, self.controller.error), ([], 'Conversation no longer exists.'))
        self.decide.assert_not_called()
        self.assertIsNone(self.storage.conversation_page(session))


if __name__ == '__main__':
    unittest.main()
