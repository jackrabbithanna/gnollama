"""Decision requests of every host type that offers them, against a loopback server with recorded answers."""
import base64
import json
import threading
import unittest
from dataclasses import replace
from unittest.mock import patch

from gi.repository import Gio
from src import decision
from src.providers import (Capabilities, InferenceManagerProvider, LMStudioProvider, OllamaCloudProvider, OllamaProvider,
                           OpenAICompatibleProvider, OpenRouterProvider, ProviderError, RequestCancelled,
                           generates)
from src.providers import systemone
from src.providers.openai import fingerprint
from src.providers.openai_platform import OpenAIProvider
from src.providers.transport import error_detail
from src.providers.vercel import VercelProvider
from src.services import Services
from src.widgets.chat_input import ChatInput
from test_inference_manager import CLEF, FIXTURES, NOMIC, QWEN, SMOL, served
from test_lmstudio import HostFixture, Loopback, recorded
from test_openai import GIF, JPEG, PNG, WEBP, Handler, Keys
from test_ui import pump_until

STATE = 'I was charged twice this month. Please look into it today.'
QUESTIONS = {
    'billing': {'type': 'noul', 'instructions': 'Is this about billing?'},
    'tone': {'type': 'choice', 'criteria': {'angry': None, 'calm': None, 'excited': None}},
    'urgency': {'type': 'score', 'criteria': ['Can wait', 'Needs attention this week', 'Needs attention today']}}
BOX = {'where': {'type': 'box', 'instructions': 'Where is the total?'}}
ERRORS = recorded('systemone_errors.json')
ELEPHANT, JEV, LUNA = 'elephant-1-decision', 'typesafe/jev-1.14', 'openai/gpt-6-luna-decisions:free'
GATEWAY_JEV, GATEWAY_LUNA = 'typesafe-ai/jev-1.14.0', 'openai/gpt-6-luna-decisions'
TAGS = {'models': [
    {'name': 'd3:latest', 'model': 'd3:latest', 'digest': 'd3d3', 'capabilities': ['decision', 'vision']},
    {'name': 'd3-text:latest', 'model': 'd3-text:latest', 'digest': 'd3d4', 'capabilities': ['decision']},
    {'name': 'llama:latest', 'model': 'llama:latest', 'digest': 'aaaa', 'capabilities': ['completion', 'vision']},
    {'name': 'embedder:latest', 'model': 'embedder:latest', 'digest': 'bbbb', 'capabilities': ['embedding']},
    # An older server says nothing in the list, and a model may list what no rule here knows.
    {'name': 'silent:latest', 'model': 'silent:latest', 'digest': 'cccc'},
    {'name': 'odd:latest', 'model': 'odd:latest', 'digest': 'dddd', 'capabilities': ['vision']}]}


class Decider(Handler):
    """Answers a decision with the recorded answer, and lists the models of every type."""
    def do_POST(self):
        server = self.server
        raw = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        body = json.loads(raw) if raw else None
        server.received.append((self.path, body, self.headers.get('Authorization')))
        server.headers.append({name.lower(): value for name, value in self.headers.items()})
        server.started.set()
        if server.stall:
            server.release.wait(5)
        self.send_response(server.status)
        self.end_headers()
        if server.status != 200:
            answer = server.body
        elif self.path.endswith(('/systemone', '/decisions')):
            answer = server.answer
        elif self.path.endswith('/models?output_modalities=decisions'):
            answer = server.deciders
        elif self.path.endswith('/embeddings/models'):
            answer = server.embedders
        elif self.path.endswith('/api/tags'):
            answer = TAGS
        elif self.path.endswith('/api/show'):
            answer = {'capabilities': next(model for model in TAGS['models']
                                           if model['name'] == body['model']).get('capabilities', ['completion'])}
        else:
            answer = server.models
        try:
            self.wfile.write(answer if isinstance(answer, bytes) else json.dumps(answer).encode())
        except (BrokenPipeError, ConnectionResetError):
            server.dropped.set()


class Served(Loopback):
    """A provider under test on a server that decides."""
    ANSWER = 'systemone_ollama.json'

    def setUp(self):
        super().setUp()
        self.server.RequestHandlerClass = Decider
        self.server.headers = []
        self.server.answer = self.recorded = recorded(self.ANSWER)
        self.server.deciders = recorded('openrouter_decisions.json')


class Deciding(Served):
    """What every type that decides must do alike."""
    PATH = '/v1/systemone'
    DECIDER = ''
    FAILURES: tuple = ()
    KEYED = True
    TONE, INPUT_TOKENS = 'calm', 449  # What the recorded answer says.
    BAD_GATEWAY = 'HTTP Error 502'  # A 502 with no words, as this type reads it.

    def decide(self, state=STATE, questions=QUESTIONS, provider=None, **kwargs):
        return (provider or self.provider()).decide(self.DECIDER, state, questions, **kwargs)

    def sent(self):
        return self.server.received[-1][1]

    def keyed(self):
        return self.provider(host_id='host', credential_id='reference', credentials=Keys({'reference': 'sk-secret'}))

    def test_request_goes_to_the_decision_endpoint_and_the_answer_comes_back_whole(self):
        questions = json.loads(json.dumps(QUESTIONS))
        answer = self.decide(questions=questions)
        self.assertEqual(answer, self.recorded)
        self.assertEqual(self.server.received, [
            (self.PATH, {'model': self.DECIDER, 'state': STATE, 'questions': QUESTIONS}, None)])
        self.assertNotIn(systemone.IMAGES_HEADER.lower(), self.server.headers[-1])
        self.assertEqual(questions, QUESTIONS)
        # What came back is what the core module reads.
        result = decision.parse_result(answer, decision.parse_questions(QUESTIONS))
        self.assertEqual([item.type for item in result.answers], ['noul', 'choice', 'score'])
        self.assertEqual((result.answers[1].value, result.input_tokens), (self.TONE, self.INPUT_TOKENS))
        # State may be JSON of any shape.
        for state in ({'ticket': 7, 'text': STATE}, [STATE, 'second'], ''):
            self.decide(state=state)
            self.assertEqual(self.sent()['state'], state)

    def test_an_answer_that_is_not_one_is_refused(self):
        for answer in ({}, {'answers': []}, {'model': self.DECIDER}, b''):
            with self.subTest(answer=answer):
                self.server.answer = answer
                with self.assertRaisesRegex(ProviderError, 'The response has no answers'):
                    self.decide()
        self.server.answer = b'[1]'
        with self.assertRaisesRegex(ProviderError, 'Expected a JSON object'):
            self.decide()
        self.server.answer = {'error': {'message': 'model crashed'}}
        with self.assertRaisesRegex(ProviderError, '^model crashed$'):
            self.decide()

    def test_every_error_shape_reads_as_what_the_server_said(self):
        self.assertTrue(self.FAILURES)
        for name in self.FAILURES + ('typesafe',):
            with self.subTest(error=name):
                error = ERRORS[name]
                self.server.status, self.server.body = error['status'], json.dumps(error['body']).encode()
                with self.assertRaises(ProviderError) as caught:
                    self.decide(provider=self.keyed() if self.KEYED else None)
                self.assertEqual((str(caught.exception), caught.exception.status), (error['says'], error['status']))
        # Without words in the body, the status says what happened.
        self.server.status, self.server.body = 502, b'<html>Bad Gateway</html>'
        with self.assertRaisesRegex(ProviderError, self.BAD_GATEWAY) as caught:
            self.decide()
        self.assertEqual(caught.exception.status, 502)

    def test_key_goes_with_the_request_and_is_kept_out_of_errors(self):
        if not self.KEYED:
            self.skipTest('This type sends no key')
        self.decide(provider=self.keyed())
        self.assertEqual(self.server.received[-1][2], 'Bearer sk-secret')
        self.server.status, self.server.body = 400, b'{"error": {"message": "HTTP 400: {\\"detail\\": \\"no sk-secret\\"}"}}'
        with self.assertRaises(ProviderError) as caught:
            self.decide(provider=self.keyed())
        self.assertEqual(str(caught.exception), 'no [redacted]')
        self.server.status = 401
        with self.assertRaisesRegex(ProviderError, 'rejected the API key, or requires one'):
            self.decide(provider=self.keyed())

    def test_cancellation_stops_a_request_that_waits_and_one_not_yet_sent(self):
        stopped = Gio.Cancellable()
        stopped.cancel()
        with self.assertRaises(RequestCancelled):
            self.decide(cancellable=stopped)
        self.assertEqual(self.server.received, [])
        self.server.stall = True
        cancel, errors = Gio.Cancellable(), []

        def run():
            try:
                self.decide(cancellable=cancel)
            except Exception as exc:
                errors.append(exc)
        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(self.server.started.wait(3))
        cancel.cancel()
        thread.join(3)
        self.server.release.set()
        self.assertFalse(thread.is_alive())
        self.assertEqual([type(error) for error in errors], [RequestCancelled])

    def test_decision_models_stay_out_of_the_chat_and_embedding_lists(self):
        provider = self.provider()
        deciders = provider.list_decision_models()
        self.assertTrue(deciders)
        for entry in deciders:
            self.assertIn('decision', entry['capabilities'])
            self.assertFalse(generates(entry))
            self.assertFalse(generates(provider.show_model(entry['name'])))
        names = {entry['name'] for entry in deciders}
        chat = [entry for entry in provider.list_models() if generates(entry)]
        self.assertTrue(chat)
        self.assertFalse(names & {entry['name'] for entry in chat})
        self.assertFalse(names & {entry['name'] for entry in provider.list_embedding_models()})


class AssetChecks:
    """Images as assets, each named by a placeholder that the state holds."""
    def placeholders(self, count=1):
        assets = self.sent()['assets']
        self.assertEqual(len(assets), count)
        for name in assets:
            self.assertRegex(name, '^img-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
        return list(assets)

    def test_images_are_assets_that_the_state_names(self):
        self.decide(images=[PNG, JPEG])
        first, second = self.placeholders(2)
        self.assertEqual(self.sent(), {
            'model': self.DECIDER, 'state': f'{STATE}\n{first}\n{second}', 'questions': QUESTIONS,
            'assets': {first: {'type': 'image', 'format': 'png', 'data_base64': PNG},
                       second: {'type': 'image', 'format': 'jpeg', 'data_base64': JPEG}}})
        self.assertNotIn('images', self.sent())
        cases = (('', lambda name: name), (None, lambda name: name),
                 ([STATE, {'page': 2}], lambda name: [STATE, {'page': 2}, name]),
                 ({'ticket': 7}, lambda name: [{'ticket': 7}, name]), (7, lambda name: [7, name]))
        for state, expected in cases:
            with self.subTest(state=state):
                original = json.loads(json.dumps(state))
                self.decide(state=state, images=['data:image/png;base64,' + PNG])
                name, = self.placeholders()
                self.assertEqual(self.sent()['state'], expected(name))
                self.assertEqual(self.sent()['assets'][name]['data_base64'], PNG)
                self.assertEqual(state, original)
        self.decide(images=[])
        self.assertNotIn('assets', self.sent())

    def test_images_that_are_not_png_or_jpeg_are_refused_before_sending(self):
        for image in (GIF, WEBP, 'not base64!', ''):
            with self.subTest(image=image), self.assertRaisesRegex(ProviderError, 'must be PNG or JPEG'):
                self.decide(images=[PNG, image])
        self.assertEqual(self.server.received, [])

    def test_the_formats_taken_are_declared_and_told_apart(self):
        self.assertEqual(self.provider().capabilities.decision_image_formats, ('png', 'jpeg'))
        kinds = [systemone.image_kind(image) for image in (PNG, 'data:image/jpeg;base64,' + JPEG, GIF, WEBP, 'no!', '')]
        self.assertEqual(kinds, ['png', 'jpeg', None, None, None, None])

    def test_keep_alive_is_refused(self):
        with self.assertRaisesRegex(ProviderError, 'does not accept: keep_alive'):
            self.decide(keep_alive=60)
        self.assertEqual(self.server.received, [])

    def test_boxes_are_offered_and_only_a_model_that_sees_images_keeps_them(self):
        provider = self.provider()
        self.assertEqual(decision.check_request(decision.parse_questions(BOX), [PNG], provider.capabilities), '')
        seeing, blind = provider.list_decision_models()
        for details in (seeing, provider.show_model(seeing['name']), None, {}):
            self.assertIs(provider.decision_capabilities(details), provider.capabilities)
        for details in (blind, provider.show_model(blind['name'])):
            self.assertEqual(provider.decision_capabilities(details),
                             replace(provider.capabilities, decision_images=False, decision_boxes=False))


class OllamaTests(Deciding, unittest.TestCase):
    KIND = OllamaProvider
    ANSWER = 'systemone_ollama.json'
    DECIDER = 'd3'
    FAILURES = ('ollama',)
    KEYED = False

    def test_images_go_bare_in_a_list_and_never_as_assets(self):
        self.decide(images=[PNG, 'data:image/jpeg;base64,' + JPEG, GIF])
        self.assertEqual(self.sent(), {'model': 'd3', 'state': STATE, 'questions': QUESTIONS,
                                       'images': [PNG, JPEG, GIF]})
        self.decide(state={'ticket': 7}, images=[PNG])
        self.assertEqual(self.sent()['state'], {'ticket': 7})
        self.assertNotIn('assets', self.sent())
        self.decide(images=[])
        self.assertNotIn('images', self.sent())

    def test_keep_alive_is_sent_once_valid(self):
        self.decide(keep_alive=300)
        self.assertEqual(self.sent()['keep_alive'], 300)
        self.decide(keep_alive=0)
        self.assertEqual(self.sent()['keep_alive'], 0)
        with self.assertRaisesRegex(ProviderError, 'Keep-alive must be'):
            self.decide(keep_alive='5m')
        self.assertEqual(len(self.server.received), 2)

    def test_models_are_those_the_list_says_decide(self):
        provider = self.provider()
        self.assertEqual(provider.list_decision_models(), TAGS['models'][:2])
        self.assertEqual(self.server.received[-1][0], '/api/tags')
        capabilities = provider.capabilities
        self.assertEqual((capabilities.decisions, capabilities.decision_images, capabilities.decision_boxes,
                          capabilities.decision_question_limit), (True, True, False, 64))
        # It reads whatever image it is sent.
        self.assertEqual(capabilities.decision_image_formats, ())
        seeing, blind = provider.list_decision_models()
        self.assertIs(provider.decision_capabilities(seeing), capabilities)
        self.assertIs(provider.decision_capabilities(provider.show_model('d3:latest')), capabilities)
        self.assertFalse(provider.decision_capabilities(blind).decision_images)
        self.assertFalse(provider.decision_capabilities(provider.show_model('d3-text:latest')).decision_images)
        many = decision.parse_questions({f'q{index}': {'type': 'noul'} for index in range(65)})
        self.assertEqual(decision.check_request(many[:64], [PNG], provider.decision_capabilities(seeing)), '')
        self.assertIn('at most 64', decision.check_request(many, [], capabilities))
        self.assertIn('does not accept images', decision.check_request(many[:1], [PNG],
                                                                      provider.decision_capabilities(blind)))
        self.assertIn('box questions', decision.check_request(decision.parse_questions(BOX), [PNG], capabilities))

    def test_types_without_decision_models_offer_nothing(self):
        for kind in (OllamaCloudProvider, OpenAICompatibleProvider):
            with self.subTest(kind=kind.kind):
                provider = kind(self.url)
                self.assertFalse(provider.capabilities.decisions)
                self.assertIn('does not offer decision models',
                              decision.check_request(decision.parse_questions(QUESTIONS), [], provider.capabilities))
                with self.assertRaises(NotImplementedError):
                    provider.list_decision_models()
                with self.assertRaises(NotImplementedError):
                    provider.decide('d3', STATE, QUESTIONS)
        self.assertEqual(self.server.received, [])


class LMStudioTests(Deciding, unittest.TestCase):
    ANSWER = 'systemone_lmstudio.json'
    DECIDER = ELEPHANT
    FAILURES = ('lmstudio',)

    def test_images_go_as_data_urls(self):
        self.decide(images=[PNG, JPEG, 'data:image/png;base64,' + PNG])
        self.assertEqual(self.sent(), {
            'model': ELEPHANT, 'state': STATE, 'questions': QUESTIONS,
            'images': ['data:image/png;base64,' + PNG, 'data:image/jpeg;base64,' + JPEG,
                       'data:image/png;base64,' + PNG]})
        self.assertNotIn('assets', self.sent())
        for suffix in ('/', '/v1', '/v1/'):
            LMStudioProvider(self.url + suffix).decide(ELEPHANT, STATE, QUESTIONS)
            self.assertEqual(self.server.received[-1][0], '/v1/systemone')
        with self.assertRaisesRegex(ProviderError, 'does not accept: keep_alive'):
            self.decide(keep_alive=60)

    def test_models_are_those_of_the_decision_type_and_say_whether_they_see_images(self):
        provider = self.provider()
        self.assertEqual(provider.list_decision_models(), [{
            'name': ELEPHANT, 'size': 6253341408, 'capabilities': ['decision', 'vision'],
            'digest': fingerprint(ELEPHANT, None, 'Q4_K_M', 6253341408),
            'details': {'format': 'gguf', 'family': 'qwen35', 'parameter_size': '9.0B', 'quantization_level': 'Q4_K_M'}}])
        self.assertEqual(self.server.received[-1][0], '/api/v1/models')
        self.assertEqual(provider.show_model(ELEPHANT), {
            'details': {'format': 'gguf', 'family': 'qwen35', 'parameter_size': '9.0B', 'quantization_level': 'Q4_K_M'},
            'capabilities': ['decision', 'vision'], 'max_context_length': 262144})
        capabilities = provider.capabilities
        self.assertEqual((capabilities.decisions, capabilities.decision_images, capabilities.decision_boxes,
                          capabilities.decision_question_limit), (True, True, False, 0))
        self.assertIs(provider.decision_capabilities(provider.show_model(ELEPHANT)), capabilities)
        self.assertIn('box questions', decision.check_request(decision.parse_questions(BOX), [PNG], capabilities))
        # A model that says it has no adapter for images, or says nothing, is not sent any.
        for change in (lambda entry: entry.update(has_vision_adapter=False), lambda entry: entry.pop('has_vision_adapter'),
                       lambda entry: entry.update(capabilities={'vision': False}, has_vision_adapter=True)):
            change(self.entry(ELEPHANT))
            listed, = provider.list_decision_models()
            self.assertEqual(listed['capabilities'], ['decision'])
            for details in (listed, provider.show_model(ELEPHANT)):
                narrowed = provider.decision_capabilities(details)
                self.assertEqual(narrowed, replace(capabilities, decision_images=False))
                self.assertIn('does not accept images', decision.check_request(
                    decision.parse_questions(QUESTIONS), [PNG], narrowed))


class InferenceManagerTests(Deciding, unittest.TestCase):
    """Against the answers clef-flash gave through the service on another computer."""
    RECORDED = 'inference_manager_models.json'
    KIND = InferenceManagerProvider
    ANSWER = 'systemone_inference_manager.json'
    DECIDER = CLEF
    FAILURES = ('inference_manager',)
    KEYED = False
    TONE, INPUT_TOKENS = 'angry', 323
    BAD_GATEWAY = 'provider did not answer'

    def setUp(self):
        super().setUp()
        self.server.models = served(SMOL, QWEN, NOMIC, CLEF)

    def provider(self, **kwargs):
        provider = self.KIND(self.url, **kwargs)
        object.__setattr__(provider, 'catalogue_path', FIXTURES + '/inference_manager_catalogue.json')
        object.__setattr__(provider, 'snaps_path', FIXTURES + '/inference_manager_snaps')
        return provider

    def test_images_go_as_data_urls_and_never_as_assets(self):
        self.decide(images=[PNG, 'data:image/jpeg;base64,' + JPEG])
        self.assertEqual(self.sent(), {'model': CLEF, 'state': STATE, 'questions': QUESTIONS,
                                       'images': ['data:image/png;base64,' + PNG, 'data:image/jpeg;base64,' + JPEG]})
        self.decide(images=[])
        self.assertNotIn('images', self.sent())
        with self.assertRaisesRegex(ProviderError, 'does not accept: keep_alive'):
            self.decide(keep_alive=60)

    def test_models_are_those_the_catalogue_says_decide(self):
        provider = self.provider()
        listed, = provider.list_decision_models()
        self.assertEqual(listed, {'name': CLEF, 'size': 6782 * 2 ** 20, 'capabilities': ['decision', 'vision'],
                                  'digest': fingerprint(CLEF, 'Q4_K_M', '6782M'),
                                  'details': {'family': 'Clef Flash', 'quantization_level': 'Q4_K_M'}})
        self.assertEqual(provider.show_model(CLEF)['capabilities'], ['decision', 'vision'])
        capabilities = provider.capabilities
        self.assertEqual((capabilities.decisions, capabilities.decision_images, capabilities.decision_boxes,
                          capabilities.decision_question_limit, capabilities.decision_image_formats),
                         (True, True, False, 0, ()))
        self.assertIs(provider.decision_capabilities(listed), capabilities)
        self.assertIn('box questions', decision.check_request(decision.parse_questions(BOX), [PNG], capabilities))
        # Without the catalogue nothing is known to decide, so nothing is offered.
        object.__setattr__(provider, 'catalogue_path', FIXTURES + '/missing.json')
        self.assertEqual(provider.list_decision_models(), [])

    def test_the_errors_it_gave(self):
        answers = recorded('systemone_inference_manager_errors.json')
        for name, said in (('no_mmproj', 'start it with `--mmproj`'), ('raw_image', 'must be data URLs'),
                           ('box', 'must be one of: choice, score, noul')):
            with self.subTest(error=name):
                self.server.status, self.server.body = answers[name]['status'], json.dumps(answers[name]['body']).encode()
                with self.assertRaisesRegex(ProviderError, said):
                    self.decide()


class OpenRouterTests(AssetChecks, Deciding, unittest.TestCase):
    RECORDED = 'openrouter_models.json'
    EMBEDDERS = 'openrouter_embeddings.json'
    KIND = OpenRouterProvider
    ANSWER = 'systemone_openrouter.json'
    PATH = '/api/v1/systemone'
    DECIDER = JEV
    FAILURES = ('openrouter', 'openrouter_upstream')

    def locate(self):
        self.url = self.fixture.host + '/api'
        moved = patch.object(OpenRouterProvider, 'fixed_url', self.url)
        moved.start()
        self.addCleanup(moved.stop)

    def test_models_come_from_the_list_of_those_that_decide(self):
        provider = self.provider()
        self.assertEqual(provider.list_decision_models(), [
            {'name': JEV, 'digest': fingerprint('typesafe/jev-1.14-20261102'), 'capabilities': ['decision', 'vision'],
             'context_length': 32768},
            {'name': LUNA, 'digest': fingerprint('openai/gpt-6-luna-decisions'), 'capabilities': ['decision'],
             'context_length': 128000}])
        self.assertEqual(self.server.received, [('/api/v1/models?output_modalities=decisions', None, None)])
        del self.server.received[:]
        self.assertEqual(provider.show_model(JEV), {'capabilities': ['decision', 'vision'], 'context_length': 32768,
                                                    'pricing': {'prompt': '0.000001', 'completion': '0'}})
        # After the two lists a chat or embedding model is found on.
        self.assertEqual([request[0] for request in self.server.received],
                         ['/api/v1/models', '/api/v1/embeddings/models', '/api/v1/models?output_modalities=decisions'])
        with self.assertRaisesRegex(ProviderError, 'no longer available'):
            provider.show_model('gone')

    def test_a_box_answer_and_the_cost_are_read(self):
        questions = dict(QUESTIONS, **BOX)
        answer = self.decide(questions=questions, images=[PNG])
        self.assertNotIn(systemone.IMAGES_HEADER.lower(), self.server.headers[-1])
        result = decision.parse_result(answer, decision.parse_questions(questions))
        self.assertEqual(result.answers[3].box, {'top': 0.16, 'bottom': 0.62, 'left': 0.17, 'right': 0.7})
        self.assertEqual(result.cost, 0.000449)


class VercelTests(AssetChecks, Deciding, unittest.TestCase):
    RECORDED = 'vercel_models.json'
    KIND = VercelProvider
    ANSWER = 'systemone_vercel.json'
    PATH = '/gateway/typesafe/v1/systemone'
    DECIDER = GATEWAY_JEV
    FAILURES = ('vercel', 'vercel_upstream', 'vercel_key')

    def locate(self):
        self.url = self.fixture.host + '/gateway'
        moved = patch.object(VercelProvider, 'fixed_url', self.url)
        moved.start()
        self.addCleanup(moved.stop)

    def test_the_images_header_goes_only_with_an_image_or_a_box_question(self):
        header = systemone.IMAGES_HEADER.lower()
        self.decide()
        self.assertNotIn(header, self.server.headers[-1])
        self.decide(images=[])
        self.assertNotIn(header, self.server.headers[-1])
        self.decide(images=[PNG])
        self.assertEqual(self.server.headers[-1][header], 'true')
        self.decide(questions=dict(QUESTIONS, **BOX))
        self.assertEqual(self.server.headers[-1][header], 'true')
        self.assertNotIn('assets', self.sent())
        self.decide(provider=self.keyed(), images=[JPEG])
        self.assertEqual((self.server.headers[-1][header], self.server.received[-1][2]), ('true', 'Bearer sk-secret'))
        # Other requests of the type never carry it.
        self.keyed().list_decision_models()
        self.assertNotIn(header, self.server.headers[-1])
        self.assertEqual({request[0] for request in self.server.received},
                         {'/gateway/typesafe/v1/systemone', '/gateway/v1/models'})

    def test_only_the_gateway_address_is_sent_to(self):
        with patch.object(VercelProvider, 'fixed_url', 'https://ai-gateway.vercel.sh'), \
                self.assertRaisesRegex(ProviderError, 'requires https://ai-gateway.vercel.sh'):
            self.decide()
        self.assertEqual(self.server.received, [])

    def test_models_are_those_of_the_evaluation_type(self):
        provider = self.provider()
        self.assertEqual(provider.list_decision_models(), [
            {'name': GATEWAY_JEV, 'digest': fingerprint(GATEWAY_JEV, 1793577600), 'capabilities': ['decision', 'vision'],
             'context_length': 32768},
            {'name': GATEWAY_LUNA, 'digest': fingerprint(GATEWAY_LUNA, 1793577600), 'capabilities': ['decision'],
             'context_length': 128000}])
        self.assertEqual(self.server.received, [('/gateway/v1/models', None, None)])
        self.assertEqual(provider.show_model(GATEWAY_JEV), {'capabilities': ['decision', 'vision'],
                                                            'context_length': 32768, 'pricing': {'prompt': '0.000001'}})
        self.assertEqual(provider.show_model(GATEWAY_LUNA)['capabilities'], ['decision'])
        self.assertIsNone(provider.model_tag(GATEWAY_JEV))
        with self.assertRaisesRegex(ProviderError, 'no longer available'):
            provider.show_model('gone')

    def test_the_cost_is_read_from_where_the_gateway_puts_it(self):
        result = decision.parse_result(self.decide(), decision.parse_questions(QUESTIONS))
        self.assertEqual(result.cost, 0.000449)


class OpenAITests(Served, unittest.TestCase):
    """OpenAI's own Decisions API, translated to and from System One's shape."""
    RECORDED = 'openai_models.json'
    KIND = OpenAIProvider
    ANSWER = 'decisions_openai.json'
    DECIDER = 'gpt-6-luna'

    def setUp(self):
        super().setUp()
        self.server.answer = self.recorded['answer']

    def locate(self):
        self.url = self.fixture.host + '/openai'
        moved = patch.object(OpenAIProvider, 'fixed_url', self.url)
        moved.start()
        self.addCleanup(moved.stop)

    def decide(self, state=STATE, questions=QUESTIONS, provider=None, **kwargs):
        return (provider or self.provider()).decide(self.DECIDER, state, questions, **kwargs)

    def sent(self):
        return self.server.received[-1][1]

    def keyed(self):
        return self.provider(host_id='host', credential_id='reference', credentials=Keys({'reference': 'sk-secret'}))

    def test_questions_are_sent_in_the_shape_the_api_takes(self):
        self.decide()
        self.assertEqual(self.server.received[-1][0], '/openai/v1/decisions')
        self.assertEqual(self.sent(), {'model': self.DECIDER, 'input': STATE, 'questions': [
            {'type': 'predicate', 'name': 'billing', 'instructions': 'Is this about billing?'},
            {'type': 'choice', 'name': 'tone', 'instructions': 'tone',  # Without instructions, its name asks.
             'choices': [{'value': 'angry'}, {'value': 'calm'}, {'value': 'excited'}]},
            {'type': 'score', 'name': 'urgency', 'instructions': 'urgency', 'levels': [
                {'label': 'Can wait'}, {'label': 'Needs attention this week'}, {'label': 'Needs attention today'}]}]})
        # Descriptions go with choices, a predicate's meanings into its instructions, and JSON as JSON.
        self.decide(questions={
            'paid': {'type': 'noul', 'instructions': 'Was it paid?', 'criteria': {'true': 'Money moved', 'false': None}},
            'kind': {'type': 'choice', 'instructions': {'ask': 'Which kind?'},
                     'criteria': {'refund': 'Money back', 'other': {'any': 'thing'}}},
            'size': {'type': 'score', 'instructions': 'How big?', 'criteria': ['small', {'label': 'large'}]}})
        self.assertEqual(self.sent()['questions'], [
            {'type': 'predicate', 'name': 'paid', 'instructions': 'Was it paid?\n\ntrue: Money moved'},
            {'type': 'choice', 'name': 'kind',
             'instructions': [{'type': 'input_json', 'data': {'ask': 'Which kind?'}}],
             'choices': [{'value': 'refund', 'description': 'Money back'},
                         {'value': 'other', 'description': '{"any": "thing"}'}]},
            {'type': 'score', 'name': 'size', 'instructions': 'How big?',
             'levels': [{'label': 'small'}, {'label': '{"label": "large"}'}]}])

    def test_state_and_images_become_the_input(self):
        for state, sent in (({'ticket': 7}, [{'type': 'input_json', 'data': {'ticket': 7}}]),
                            ([STATE, 'second'], [{'type': 'input_text', 'text': '["%s", "second"]' % STATE}]),
                            ([{'a': 1}], [{'type': 'input_json', 'data': [{'a': 1}]}])):
            with self.subTest(state=state):
                self.decide(state=state)
                self.assertEqual(self.sent()['input'], [{'role': 'user', 'content': sent}])
        self.decide(state='', images=[PNG, 'data:image/jpeg;base64,' + JPEG])
        self.assertEqual(self.sent()['input'], [{'role': 'user', 'content': [
            {'type': 'input_image', 'image_url': 'data:image/png;base64,' + PNG},
            {'type': 'input_image', 'image_url': 'data:image/jpeg;base64,' + JPEG}]}])
        self.decide(images=[PNG])
        self.assertEqual(self.sent()['input'][0]['content'][0], {'type': 'input_text', 'text': STATE})

    def test_the_answer_reads_as_a_system_one_answer(self):
        answer = self.decide()
        self.assertEqual(answer['answers'], {
            'billing': {'type': 'noul', 'noul': 1.0},
            'tone': {'type': 'choice', 'choice': 'calm', 'confidence': 0.43,
                     'probabilities': {'angry': 0.38, 'calm': 0.62, 'excited': 0.0}},
            'urgency': {'type': 'score', 'score': 1.99, 'confidence': 0.99,
                        'probabilities': {'0': 0.0, '1': 0.01, '2': 0.99},
                        'legend': {'0': 'Can wait', '1': 'Needs attention this week', '2': 'Needs attention today'}}})
        self.assertEqual((answer['model'], answer['usage']), (self.DECIDER, self.recorded['answer']['usage']))
        result = decision.parse_result(answer, decision.parse_questions(QUESTIONS))
        self.assertEqual([(item.type, item.value) for item in result.answers],
                         [('noul', 1.0), ('choice', 'calm'), ('score', 1.99)])
        self.assertEqual((result.input_tokens, result.output_tokens, result.cost), (387, 0, None))
        # An answer without a name is its question's by place; a refusal stays one, and others are kept as they came.
        self.server.answer = {'model': self.DECIDER, 'answers': [
            {'type': 'predicate', 'name': None, 'probability': 0.2}, {'type': 'refusal', 'name': 'tone'},
            {'type': 'later', 'name': 'urgency', 'value': 3}]}
        answer = self.decide()
        self.assertEqual(answer['answers'], {'billing': {'type': 'noul', 'noul': 0.2},
                                             'tone': {'type': 'refusal', 'name': 'tone'},
                                             'urgency': {'type': 'later', 'name': 'urgency', 'value': 3}})
        result = decision.parse_result(answer, decision.parse_questions(QUESTIONS))
        self.assertEqual([item.type for item in result.answers], ['noul', 'refusal', 'invalid'])
        for answer in ({}, {'answers': {}}, {'model': self.DECIDER}):
            with self.subTest(answer=answer):
                self.server.answer = answer
                with self.assertRaisesRegex(ProviderError, 'The response has no answers'):
                    self.decide()

    def test_what_the_api_cannot_take_is_refused_before_sending(self):
        with self.assertRaisesRegex(ProviderError, 'does not accept: box questions'):
            self.decide(questions=BOX, images=[PNG])
        with self.assertRaisesRegex(ProviderError, 'does not accept: keep_alive'):
            self.decide(keep_alive=60)
        self.assertEqual(self.server.received, [])
        capabilities = self.provider().capabilities
        self.assertTrue(capabilities.decisions and capabilities.decision_images)
        self.assertFalse(capabilities.decision_boxes)
        self.assertEqual((capabilities.decision_question_limit, capabilities.decision_image_formats),
                         (400, systemone.ASSET_FORMATS))
        self.assertIn('box questions', decision.check_request(decision.parse_questions(BOX), [PNG], capabilities))
        many = decision.parse_questions({f'q{index}': {'type': 'noul'} for index in range(401)})
        self.assertIn('at most 400', decision.check_request(many, [], capabilities))

    def test_errors_and_the_key(self):
        for name, error in self.recorded['errors'].items():
            with self.subTest(error=name):
                self.server.status, self.server.body = error['status'], json.dumps(error['body']).encode()
                with self.assertRaises(ProviderError) as caught:
                    self.decide(provider=self.keyed())
                self.assertEqual((str(caught.exception), caught.exception.status), (error['says'], error['status']))
        self.server.status, self.server.body = 200, b''
        self.decide(provider=self.keyed())
        self.assertEqual(self.server.received[-1][2], 'Bearer sk-secret')
        self.server.status = 401
        self.server.body = b'{"error": {"message": "Incorrect API key provided: sk-secret."}}'
        with self.assertRaisesRegex(ProviderError, 'rejected the API key, or requires one'):
            self.decide(provider=self.keyed())

    def test_the_one_model_that_decides_is_listed_and_also_chats(self):
        provider = self.provider()
        self.assertEqual(provider.list_decision_models(), [dict(
            provider.model_tag(self.DECIDER), capabilities=['decision', 'vision'])])
        self.assertEqual(self.server.received[-1][0], '/openai/v1/models')
        # It is a chat model as well, so it stays in the chat list.
        self.assertIn(self.DECIDER, [entry['name'] for entry in provider.list_models()])
        self.assertIs(provider.decision_capabilities(provider.list_decision_models()[0]), provider.capabilities)
        self.server.models = {'data': [{'id': 'gpt-6-luna-2027-01-01', 'created': 1}, {'id': 'gpt-6-sol'}]}
        self.assertEqual([entry['name'] for entry in provider.list_decision_models()], ['gpt-6-luna-2027-01-01'])


class HelperTests(unittest.TestCase):
    def test_error_detail_reads_each_place_a_server_puts_it(self):
        self.assertEqual(error_detail({'error': 'text'}), 'text')
        self.assertEqual(error_detail({'error': {'message': 'm'}, 'message': 'other'}), {'message': 'm'})
        self.assertEqual(error_detail({'message': 'm', 'error_type': 'invalid_request'}), 'm')
        self.assertEqual(error_detail({'detail': {'message': 'd'}}), {'message': 'd'})
        for silent in ({}, {'error': None, 'message': ''}, [], 'text', None):
            self.assertIsNone(error_detail(silent))

    def test_explain_unwraps_an_upstream_error_and_leaves_other_text_alone(self):
        explain = systemone.explain
        self.assertEqual(explain(400, {'message': 'HTTP 400: {"detail":{"message":"inner"}}'}), 'inner')
        self.assertEqual(explain(400, '{"error":{"message":"HTTP 422: {\\"detail\\":\\"deep\\"}"}}'), 'deep')
        self.assertEqual(explain(400, 'HTTP 400: {"code": 3}'), 'HTTP 400: {"code": 3}')
        for plain in ('model "x" is not a decision model', 'HTTP 400: {not json}', 'Use {"type": "noul"} here'):
            self.assertEqual(explain(400, plain), plain)
        self.assertIsNone(explain(500, None))
        self.assertEqual(explain(400, {'code': 3}), '{"code": 3}')

    def test_capability_records_combine_with_the_new_fields(self):
        ollama, router = OllamaProvider.capabilities, OpenRouterProvider.capabilities
        both = ollama.both(router)
        self.assertEqual((both.decisions, both.decision_images, both.decision_boxes, both.decision_question_limit),
                         (True, True, False, 64))
        self.assertEqual(router.both(ollama).decision_question_limit, 64)
        self.assertEqual(router.both(router).decision_question_limit, 0)
        # One that takes only some formats limits both.
        self.assertEqual([one.both(other).decision_image_formats for one, other in (
            (ollama, router), (router, ollama), (router, router), (ollama, ollama))],
            [('png', 'jpeg'), ('png', 'jpeg'), ('png', 'jpeg'), ()])
        self.assertEqual(Capabilities().both(ollama).decisions, False)
        self.assertEqual(Capabilities(), Capabilities(decisions=False, decision_question_limit=0))

    def test_only_a_model_said_to_embed_or_decide_is_kept_from_chat(self):
        for details in (None, {}, {'capabilities': None}, {'capabilities': []}, {'capabilities': ['vision']},
                        {'capabilities': ['completion', 'decision']}, {'supports': {'vision': True}},
                        {'capabilities': ['thinking']}, 'name'):
            self.assertTrue(generates(details), details)
        for details in ({'capabilities': ['decision']}, {'capabilities': ['decision', 'vision']},
                        {'capabilities': ['embedding']}):
            self.assertFalse(generates(details), details)

    def test_the_fixture_image_is_one_the_helpers_know(self):
        self.assertEqual((systemone.image_format(PNG), systemone.image_format(JPEG)), ('png', 'jpeg'))
        self.assertEqual(systemone.bare('data:image/png;base64,' + PNG), PNG)
        self.assertTrue(base64.b64decode(PNG).startswith(b'\x89PNG'))


class CatalogTests(Served, unittest.TestCase):
    """The shared catalog reads a decision model list once for everyone who asks."""
    KIND = OllamaProvider

    def setUp(self):
        super().setUp()
        self.services = Services()
        self.addCleanup(self.services.shutdown)

    def test_decision_lists_are_cached_apart_from_chat_lists_and_refreshed_on_request(self):
        catalog, provider = self.services.catalog, self.provider()
        first = catalog.request_decision_models(provider).result(3)
        self.assertEqual([entry['name'] for entry in first], ['d3:latest', 'd3-text:latest'])
        self.assertEqual(first[0]['capabilities'], ['decision', 'vision'])
        self.assertEqual(catalog.request_decision_models(provider).result(3), first)
        self.assertEqual(len(self.server.received), 1)
        self.assertEqual(len(catalog.request_models(provider).result(3)), 6)
        self.assertEqual(len(self.server.received), 2)
        self.assertEqual(catalog.request_decision_models(provider, refresh=True).result(3), first)
        self.assertEqual(len(self.server.received), 3)
        # The details of a listed model are asked for as for any other.
        details = catalog.details(provider, 'd3:latest')
        self.assertFalse(provider.decision_capabilities(first[1]).decision_images)
        self.assertIs(provider.decision_capabilities(details), provider.capabilities)
        stopped = Gio.Cancellable()
        stopped.cancel()
        with self.assertRaises(RequestCancelled):
            catalog.request_decision_models(provider, stopped).result(3)

    def test_an_empty_list_is_asked_for_again_and_a_type_without_one_fails(self):
        catalog, provider = self.services.catalog, self.provider()
        with patch.dict(TAGS, models=TAGS['models'][2:]):
            self.assertEqual(catalog.request_decision_models(provider).result(3), [])
        self.assertEqual(len(catalog.request_decision_models(provider).result(3)), 2)
        with self.assertRaises(NotImplementedError):
            catalog.request_decision_models(OpenAICompatibleProvider(self.url)).result(3)


class PickerTests(HostFixture, unittest.TestCase):
    """A server that lists decision models beside chat models, as Ollama does."""
    KIND = OllamaProvider
    NAME = 'Local'

    def setUp(self):
        super().setUp()
        self.server.RequestHandlerClass = Decider
        self.server.headers = []

    def test_chat_picker_leaves_out_decision_models_and_keeps_those_that_say_nothing(self):
        widget = ChatInput()
        widget.services = self.context.services
        self.addCleanup(widget.cancel_fetches)
        widget.fetch_models(self.context.connection(self.host))
        pump_until(lambda: not widget._models_loading and self.context.services.idle)
        values = widget.model_dropdown.get_model()
        self.assertEqual([values.get_string(index) for index in range(values.get_n_items())],
                         ['llama:latest', 'odd:latest', 'silent:latest'])

    def test_comparisons_and_model_conversations_refuse_a_decision_model(self):
        with self.assertRaisesRegex(ValueError, 'Decision models cannot generate text'):
            self.prepare(self.targets('llama:latest', 'd3:latest'))
        with self.assertRaisesRegex(ValueError, 'Decision models cannot generate text'):
            self.validate(self.targets('llama:latest', 'd3:latest'))
        with self.assertRaisesRegex(ValueError, 'Embedding models cannot generate comparisons'):
            self.prepare(self.targets('llama:latest', 'embedder:latest'))
        self.prepare(self.targets('llama:latest', 'silent:latest'))
        self.validate(self.targets('llama:latest', 'odd:latest'))


if __name__ == '__main__':
    unittest.main()
