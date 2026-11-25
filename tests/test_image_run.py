"""Image runs without widgets: what blocks one, and how one is saved from start to end."""
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
from src.image_run import REFERENCE_FORMATS, ImageController, prepare
from src.providers import OllamaProvider, ProviderError, RequestCancelled
from src.providers.registry import PROVIDERS
from src.records import ImageSettings, ModelTarget
from provider_fixture import LIMITED, limited_host, provider_patch

ANSWER = json.loads((Path(__file__).parent / 'fixtures' / 'images_openrouter.json').read_text())['answer']
PNG = ANSWER['data'][0]['b64_json']
# An image model as a host lists it: every setting, and up to two references.
ENTRY = {'name': 'painter', 'capabilities': ['image', 'vision'], 'image_parameters': {
    'aspect_ratio': {'type': 'enum', 'values': ['1:1', '16:9']},
    'output_format': {'type': 'enum', 'values': ['png', 'webp']},
    'n': {'type': 'range', 'min': 1, 'max': 4}, 'seed': {'type': 'boolean'},
    'input_references': {'type': 'range', 'min': 0, 'max': 2}}}
# One that takes nothing but a prompt.
PLAIN = {'name': 'plain', 'capabilities': ['image'], 'image_parameters': {}}
# Ollama's capabilities with image generation, as the gateways have it.
IMAGING = replace(OllamaProvider.capabilities, image_generation=True, image_references=True)


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


class ImageRunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.context = AppContext(self.temp.name)
        self.storage = self.context.storage
        self.host = self.storage.get_all_hosts()[0]
        capabilities = patch.object(OllamaProvider, 'capabilities', IMAGING)
        capabilities.start()
        patcher = provider_patch('generate_image', return_value=ANSWER)
        self.generate = patcher.start()
        self.addCleanup(self.close)
        self.addCleanup(patcher.stop)
        self.addCleanup(capabilities.stop)
        self.events = []
        self.controller = ImageController(
            self.context, started=lambda run: self.events.append(('started', run['status'])),
            finished=lambda run: self.events.append(('finished', run['status'])))

    def close(self):
        self.controller.stop()
        self.settle()
        self.context.shutdown()
        self.temp.cleanup()

    def settle(self):
        pump_until(lambda: self.controller.request is None and self.context.idle and self.storage.writer.idle)

    def settings(self, options=None, model='painter', host=None):
        return ImageSettings(ModelTarget((host or self.host)['id'], model=model), dict(options or {}))

    def start(self, prompt='A lighthouse', options=None, references=(), entry=ENTRY, **kwargs):
        return self.controller.start(self.host, self.settings(options), prompt, references, entry, **kwargs)

    def runs(self):
        return self.storage.library.image_runs(self.controller.session_id)

    def test_a_request_is_resolved_with_png_output_or_refused_with_the_reason(self):
        request = prepare(self.context, self.host, self.settings({'n': 2}), 'A lighthouse', [PNG], ENTRY)
        target = request.settings.target
        self.assertEqual((target.host_id, target.url, target.model, target.host_name),
                         (self.host['id'], 'http://localhost:11434', 'painter', self.host['name']))
        # A model that offers PNG is asked for it unless another format was chosen.
        self.assertEqual(request.settings.options, {'n': 2, 'output_format': 'png'})
        self.assertEqual((request.prompt, request.references, request.parameters.references), ('A lighthouse', [PNG], 2))
        self.assertEqual(prepare(self.context, self.host, self.settings({'output_format': 'webp'}), 'A', (),
                                 ENTRY).settings.options, {'output_format': 'webp'})
        self.assertEqual(prepare(self.context, self.host, self.settings(), 'A', (), PLAIN).settings.options, {})
        refused = [
            (dict(host=None), 'Select an available model'),
            (dict(settings=self.settings(model=None)), 'Select an available model'),
            (dict(settings=ImageSettings(None)), 'Select an available model'),
            (dict(prompt=' \n'), 'Enter a prompt'),
            (dict(references=[PNG], entry=PLAIN), 'does not take reference images'),
            (dict(references=[PNG] * 3), 'at most 2 reference images'),
            (dict(settings=self.settings({'quality': 'high'})), 'does not take the setting Quality'),
            (dict(settings=self.settings({'n': 5})), 'that value for Number of images'),
            (dict(settings=self.settings({'seed': 'soon'})), 'that value for Seed'),
            (dict(entry=None, settings=self.settings({'n': 2})), 'does not take the setting'),
        ]
        for changes, reason in refused:
            values = dict(dict(host=self.host, settings=self.settings(), prompt='A lighthouse', entry=ENTRY), **changes)
            with self.subTest(reason=reason), self.assertRaisesRegex(ValueError, reason):
                prepare(self.context, **values)
        with patch.object(OllamaProvider, 'capabilities', replace(IMAGING, image_references=False)):
            with self.assertRaisesRegex(ValueError, 'does not take reference images'):
                prepare(self.context, self.host, self.settings(), 'A', [PNG], ENTRY)
        with patch.dict(PROVIDERS, LIMITED):
            limited = limited_host(self.storage)
            with self.assertRaisesRegex(ValueError, 'does not offer image generation'):
                prepare(self.context, limited, self.settings(host=limited), 'A', (), ENTRY)

    def test_a_run_is_saved_before_it_is_sent_and_again_when_it_ends(self):
        seen = []
        def generate(provider, model, prompt, options, references=None, cancellable=None, **kwargs):
            seen.append((self.runs()[0]['status'], self.context.services.models.busy(self.host, model)))
            return ANSWER
        self.generate.side_effect = generate
        self.storage.save_draft(dict(id='draft', mode='image', text='written', settings={}, revision=3))
        request = self.start('A lighthouse\nat dusk', {'aspect_ratio': '16:9'}, draft_id='draft', draft_revision=3)
        self.assertIs(self.controller.request, request)
        self.assertIsNone(self.start('again'))
        self.settle()
        self.assertEqual(seen, [('running', True)])
        self.assertEqual(self.events, [('started', 'running'), ('finished', 'done')])
        self.assertEqual(self.generate.call_args.args[1:],
                         ('painter', 'A lighthouse\nat dusk', {'aspect_ratio': '16:9', 'output_format': 'png'}))
        self.assertIsNone(self.generate.call_args.kwargs['references'])
        run, = self.runs()
        self.assertEqual((run['id'], run['status'], run['prompt'], run['error'], run['text']),
                         (request.id, 'done', 'A lighthouse\nat dusk', None, ''))
        # What was sent is what is stored, the PNG default included.
        self.assertEqual(run['options'], {'aspect_ratio': '16:9', 'output_format': 'png'})
        self.assertEqual(run['images'], [dict(data=PNG, media_type='image/png')])
        self.assertEqual(run['result']['cost'], 0)
        self.assertEqual(run['target'], dict(host='http://localhost:11434', host_id=self.host['id'],
                                             host_name=self.host['name'], model='painter'))
        self.assertEqual(self.controller.title, 'A lighthouse')
        self.assertEqual(self.storage.get_chat(self.controller.session_id)['kind'], 'image')
        self.assertIsNone(self.storage.get_draft('draft'))
        self.assertFalse(self.context.services.models.busy(self.host, 'painter'))
        # The next run joins the session.
        session = self.controller.session_id
        self.start('Second')
        self.settle()
        self.assertEqual((self.controller.session_id, self.controller.title), (session, 'A lighthouse'))
        self.assertEqual([(run['position'], run['prompt']) for run in self.runs()], [(0, 'A lighthouse\nat dusk'),
                                                                                    (1, 'Second')])

    def test_nothing_is_sent_without_a_prompt_or_past_a_refusal(self):
        self.assertIsNone(self.start(' \n'))
        with self.assertRaises(ValueError):
            self.start(options={'quality': 'high'})
        self.settle()
        self.assertIsNone(self.controller.session_id)
        self.assertEqual(self.events, [])
        self.generate.assert_not_called()
        self.assertEqual(self.storage.get_all_chats(), [])

    def test_failures_answers_without_images_cancellation_and_a_model_in_use_end_the_run_as_such(self):
        self.generate.side_effect = ProviderError('No such model.')
        self.start()
        self.settle()
        self.generate.side_effect = None
        self.generate.return_value = {'choices': [{'message': {'content': 'I cannot draw that.'}}]}
        self.start()
        self.settle()
        self.generate.return_value = ['not an answer']
        self.start()
        self.settle()
        self.generate.return_value = {'data': [{'b64_json': 'not base64!', 'media_type': 'image/png'}]}
        self.start()
        self.settle()
        self.generate.return_value = ANSWER
        self.assertTrue(self.context.services.models.reserve(self.host, 'painter'))
        self.start()
        self.settle()
        self.context.services.models.release(self.host, 'painter')
        entered = threading.Event()
        def generate(provider, model, prompt, options, references=None, cancellable=None, **kwargs):
            entered.set()
            while not cancellable.is_cancelled():
                time.sleep(0.005)
            raise RequestCancelled()
        self.generate.side_effect = generate
        self.start()
        pump_until(entered.is_set)
        self.controller.stop()
        self.settle()
        self.assertEqual([(run['status'], run['error'], run['text'], run['images']) for run in self.runs()], [
            ('failed', 'No such model.', '', []),
            # What the model wrote instead is kept.
            ('failed', 'The model answered without an image.', 'I cannot draw that.', []),
            ('failed', 'The server did not return an image.', '', []),
            ('failed', 'The server returned an image that cannot be read.', '', []),
            ('failed', 'Wait for the model operation to finish.', '', []),
            ('cancelled', None, '', [])])
        self.assertIsNotNone(self.runs()[1]['result'])
        self.assertEqual(self.events[-2:], [('started', 'running'), ('finished', 'cancelled')])
        self.assertEqual(self.controller.error, '')

    def test_references_are_converted_on_a_worker_before_the_run_is_saved(self):
        calls = []
        def convert(images, formats):
            calls.append((list(images), formats, threading.current_thread() is threading.main_thread(),
                          self.controller.session_id))
            return ['cG5n' for image in images]
        self.controller.convert = convert
        request = self.start(references=['d2VicA=='])
        self.assertIs(self.controller.request, request)
        self.settle()
        # Nothing was saved until the references were ready.
        self.assertEqual(calls, [(['d2VicA=='], REFERENCE_FORMATS, False, None)])
        self.assertEqual(REFERENCE_FORMATS, ('png', 'jpeg'))
        self.assertEqual(self.generate.call_args.kwargs['references'], ['cG5n'])
        self.assertEqual((self.runs()[0]['references'], self.runs()[0]['status']), (['cG5n'], 'done'))
        # A run without references has nothing to convert.
        self.start()
        self.settle()
        self.assertEqual(len(calls), 1)
        # One that cannot be converted is sent as it is, for the server to refuse.
        self.controller.convert = lambda images, formats: 1 / 0
        self.start(references=['d2VicA=='])
        self.settle()
        self.assertEqual(self.generate.call_args.kwargs['references'], ['d2VicA=='])
        self.assertEqual(self.runs()[2]['references'], ['d2VicA=='])

    def test_a_run_stopped_while_its_references_are_converted_is_neither_saved_nor_sent(self):
        entered, release = threading.Event(), threading.Event()
        def convert(images, formats):
            entered.set()
            release.wait(4)
            return images
        self.controller.convert = convert
        self.start(references=['d2VicA=='])
        pump_until(entered.is_set)
        self.assertIsNone(self.start('again'))
        self.controller.stop()
        release.set()
        self.settle()
        self.assertEqual((self.events, self.controller.session_id, self.controller.error), ([], None, ''))
        self.generate.assert_not_called()
        self.assertEqual(self.storage.get_all_chats(), [])

    def test_images_that_arrive_after_stop_are_kept_and_anything_else_is_not(self):
        # Images were made and paid for by then; an answer without them ends the run as stopped.
        for answer, status, count in ((ANSWER, 'done', 1), ({'data': []}, 'cancelled', 0)):
            with self.subTest(status=status):
                entered, release = threading.Event(), threading.Event()
                def generate(provider, model, prompt, options, references=None, cancellable=None, answer=answer,
                             entered=entered, release=release, **kwargs):
                    entered.set()
                    release.wait(4)
                    return answer
                self.generate.side_effect = generate
                self.start()
                pump_until(entered.is_set)
                self.controller.stop()
                release.set()
                self.settle()
                run = self.runs()[-1]
                self.assertEqual((run['status'], len(run['images'])), (status, count))

    def test_a_deleted_session_is_not_created_again(self):
        self.start()
        self.settle()
        session = self.controller.session_id
        self.storage.delete_chat(session)
        self.events.clear()
        self.generate.reset_mock()
        self.start()
        self.settle()
        self.assertEqual((self.events, self.controller.error), ([], 'Conversation no longer exists.'))
        self.generate.assert_not_called()
        self.assertIsNone(self.storage.conversation_page(session))


if __name__ == '__main__':
    unittest.main()
