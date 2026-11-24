"""ChatController without widgets: a recording view stands in for the tab."""
import copy
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from gi.repository import GLib

from provider_fixture import LIMITED, limited_host, provider_patch
from src.providers.registry import PROVIDERS
from src.app_context import AppContext
from src.records import WireOptions
from src.chat_controller import ChatController, ComposerInput
from src.drafts import DraftController
from src.knowledge import DEFAULT_RAG

KNOWLEDGE = dict(copy.deepcopy(DEFAULT_RAG), enabled=True, config_id='config', host='http://localhost:11434',
                 model='embed', collection_ids=['collection'])
SNAPSHOT = {'hits': [{'title': 'Notes', 'text': 'The answer is 42.'}]}


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


def answer(text='hello'):
    return lambda **kw: iter([{'message': {'content': text}, 'done': True}])


def tool_call(name='lookup'):
    return {'function': {'index': 0, 'name': name, 'arguments': {}}}


class RecordingView:
    def __init__(self):
        self.events, self.text, self.pending = [], '', False
        self.options = {'tools_enabled': False, 'tools_text': '', 'knowledge': copy.deepcopy(DEFAULT_RAG)}

    def names(self):
        return [event[0] for event in self.events]

    def local_options(self):
        return copy.deepcopy(self.options)

    def attachments_pending(self):
        return self.pending

    def chunk(self, state, content, thinking, logprobs):
        self.text += content
        self.events.append(('chunk', content))

    def __getattr__(self, name):
        if name.startswith('_'):
            raise AttributeError(name)
        return lambda *args: self.events.append((name,) + args)


class ChatControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='gnollama-controller-')
        self.context = AppContext(self.temp.name)
        self.storage = self.context.storage
        self.host = self.storage.get_all_hosts()[0]
        self.controllers = []

    def tearDown(self):
        for controller in self.controllers:
            controller.stop()
        pump_until(lambda: self.context.idle and self.storage.writer.idle)
        self.context.shutdown(wait=True)
        self.temp.cleanup()

    def controller(self, mode='chat', **kwargs):
        view = RecordingView()
        if mode == 'chat' and 'chat_id' not in kwargs:
            kwargs['chat_id'] = self.storage.create_chat('test')['id']
        controller = ChatController(self.context, view, mode, **kwargs)
        controller.draft = DraftController(self.storage, mode, lambda: dict(text='', images=[], settings={}),
                                           chat_id=kwargs.get('chat_id'))
        self.controllers.append(controller)
        return controller, view

    def input(self, prompt='hi', **kwargs):
        values = dict(prompt=prompt, host=self.host, model='test',
                      request=dict(format=None, tools=None, keep_alive=None), knowledge=copy.deepcopy(DEFAULT_RAG))
        values.update(kwargs)
        return ComposerInput(**values)

    def settle(self, controller):
        pump_until(lambda: controller.request is None and not controller.tool_busy
                   and self.storage.writer.idle and self.context.idle)

    def test_a_request_cannot_carry_what_the_host_lacks(self):
        with patch.dict(PROVIDERS, LIMITED), provider_patch('chat', side_effect=answer()) as chat, \
                provider_patch('generate') as generate:
            host = limited_host(self.storage)
            single, single_view = self.controller('generate')
            with self.assertRaisesRegex(ValueError, 'single responses'):
                single.send(self.input(host=host))
            controller, view = self.controller()
            cases = [(dict(options={'top_k': 5, 'min_p': 0.1}), 'Top K, Min P'),
                     (dict(options={'num_ctx': 4096}), 'Context size'),
                     (dict(logprobs=True), 'Return token probabilities'),
                     (dict(request=dict(format='json')), 'Structured output'),
                     (dict(request=dict(keep_alive=60)), 'Model Retention')]
            for case, name in cases:
                with self.subTest(case=case), self.assertRaisesRegex(ValueError, 'Limited does not support: ' + name):
                    controller.send(self.input(host=host, **case))
            self.assertIsNone(controller.request)
            self.assertEqual(view.events + single_view.events, [])
            # What the host does take still goes through.
            tools = [{'type': 'function', 'function': {'name': 'lookup', 'parameters': {'type': 'object'}}}]
            self.assertTrue(controller.send(self.input(host=host, options={'temperature': 0.2, 'top_p': 0.9},
                                                       request=dict(tools=tools))))
            self.settle(controller)
        self.assertEqual(chat.call_args.kwargs['options'], {'temperature': 0.2, 'top_p': 0.9})
        generate.assert_not_called()

    def test_generate_reports_each_stage_once(self):
        controller, view = self.controller('generate')
        with provider_patch('generate', side_effect=lambda **kw: iter([{'response': 'one '}, {'response': 'two', 'done': True}])) as generate:
            self.assertTrue(controller.send(self.input('count')))
            self.assertIsNotNone(controller.request)
            self.assertFalse(controller.send(self.input('again')))
            self.settle(controller)
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(view.text, 'one two')
        names = [name for name in view.names() if name != 'chunk']
        self.assertEqual(names, ['request_changed', 'generation_started', 'context_estimated', 'generation_prepared',
                                 'request_finished', 'request_changed', 'tools_changed'])
        state = view.events[view.names().index('request_finished')][1]
        self.assertEqual(state.metadata['status'], 'complete')
        self.assertEqual(state.connection.url, self.host['hostname'])

    def test_chat_saves_history_and_reports_title(self):
        controller, view = self.controller()
        with provider_patch('chat', side_effect=answer('hello')) as chat:
            controller.send(self.input('first question'))
            self.settle(controller)
            pump_until(lambda: 'title_changed' in view.names())
        self.assertEqual(chat.call_args.kwargs['messages'][-1], {'role': 'user', 'content': 'first question'})
        saved = self.storage.get_chat(controller.chat_id)
        self.assertEqual([m['content'] for m in saved['messages']], ['first question', 'hello'])
        self.assertIn('message_saved', view.names())
        self.assertEqual(view.events[view.names().index('title_changed')][1], controller.chat_id)

    def test_validation_errors_start_nothing(self):
        controller, view = self.controller()
        cases = [dict(host=None), dict(model=None), dict(images=('aW1n',), image_support=False),
                 dict(images=('aW1n',), capabilities_loading=True),
                 dict(knowledge=dict(KNOWLEDGE, collection_ids=[]))]
        with provider_patch('chat') as chat:
            for case in cases:
                with self.subTest(case=case), self.assertRaises(ValueError):
                    controller.send(self.input(**case))
        self.assertIsNone(controller.request)
        self.assertEqual(view.events, [])
        self.assertEqual(chat.call_count, 0)

    def test_stop_ends_a_stalled_request(self):
        controller, view = self.controller()
        started = threading.Event()

        def stalled(**kw):
            yield {'message': {'content': 'partial'}}
            started.set()
            while not kw['cancellable'].is_cancelled():
                time.sleep(.005)
            yield {'message': {'content': ' ignored'}}
        with provider_patch('chat', side_effect=stalled):
            controller.send(self.input())
            pump_until(started.is_set)
            controller.stop()
            self.settle(controller)
        state = view.events[view.names().index('request_finished')][1]
        self.assertEqual(state.metadata['status'], 'stopped')
        self.assertEqual(self.storage.get_chat(controller.chat_id)['messages'][-1]['content'], 'partial')

    def test_retrieval_failure_keeps_the_draft_and_offers_each_recovery(self):
        controller, view = self.controller()
        results = [ValueError('index is missing'), SNAPSHOT]

        def retrieve(options, query, cancel):
            result = results.pop(0)
            if isinstance(result, Exception):
                raise result
            return copy.deepcopy(result)
        with patch.object(self.context.knowledge, 'retrieve', side_effect=retrieve) as lookup, \
                provider_patch('chat', side_effect=answer()) as chat:
            controller.send(self.input('what is it', knowledge=KNOWLEDGE, query_override='the answer'))
            pump_until(lambda: 'retrieval_failed' in view.names())
            self.assertIsNone(controller.request)
            self.assertEqual(str(view.events[-1][1]), 'index is missing')
            self.assertEqual(view.names(), ['request_changed', 'retrieval_started', 'retrieval_ended',
                                            'request_changed', 'retrieval_failed'])
            self.assertEqual(chat.call_count, 0)
            self.assertEqual(controller.history, [])
            self.assertEqual(lookup.call_args.args[1], 'the answer')

            # Retry: the same input retrieves again and the sources reach the model.
            controller.send(self.input('what is it', knowledge=KNOWLEDGE))
            self.settle(controller)
            self.assertEqual(lookup.call_count, 2)
            self.assertIn('The answer is 42.', chat.call_args.kwargs['messages'][-1]['content'])
            self.assertEqual(controller.history[0]['response_metadata']['retrieval'], SNAPSHOT)

            # Send without knowledge: nothing is retrieved.
            controller.send(self.input('and now', knowledge=KNOWLEDGE), without_knowledge=True)
            self.settle(controller)
            self.assertEqual(lookup.call_count, 2)
            self.assertEqual(chat.call_args.kwargs['messages'][-1], {'role': 'user', 'content': 'and now'})

    def test_stopping_retrieval_does_not_report_a_failure(self):
        controller, view = self.controller()
        started = threading.Event()

        def retrieve(options, query, cancel):
            started.set()
            while not cancel.is_cancelled():
                time.sleep(.005)
            raise ValueError('cancelled')
        with patch.object(self.context.knowledge, 'retrieve', side_effect=retrieve), provider_patch('chat') as chat:
            controller.send(self.input(knowledge=KNOWLEDGE))
            pump_until(started.is_set)
            controller.stop()
            self.settle(controller)
        self.assertNotIn('retrieval_failed', view.names())
        self.assertEqual(chat.call_count, 0)

    def test_paged_history_is_read_in_full_before_sending(self):
        chat_id = self.storage.create_chat('test')['id']
        messages = [{'role': 'user' if i % 2 == 0 else 'assistant', 'content': 'turn %d' % i} for i in range(120)]
        self.storage.save_chat_delta(chat_id, messages, 0)
        pump_until(lambda: self.storage.writer.idle)
        page = self.storage.conversation_page(chat_id)
        self.assertTrue(page['paged'])
        controller, view = self.controller(chat_id=chat_id, initial_history=page['messages'], chat_data=page)
        self.assertLess(len(controller.history), 120)

        offset = controller.message_offset
        self.assertTrue(controller.load_older())
        pump_until(lambda: 'older_loaded' in view.names())
        older = view.events[view.names().index('older_loaded')][1]
        self.assertEqual(controller.message_offset, max(0, offset - 50))
        self.assertEqual(older[-1]['content'], 'turn %d' % (offset - 1))

        controller.estimate_draft('next', WireOptions(system='', tools='', format=''))
        pump_until(lambda: 'context_estimated' in view.names())
        self.assertIs(view.events[view.names().index('context_estimated')][2], False)

        with provider_patch('chat', side_effect=answer()) as chat:
            controller.send(self.input('next'))
            self.settle(controller)
        self.assertEqual(len(chat.call_args.kwargs['messages']), 121)
        self.assertFalse(controller.paged)
        self.assertEqual([e for e in view.events if e[0] == 'history_loading'],
                         [('history_loading', True), ('history_loading', False)])
        self.assertEqual(len(self.storage.get_messages(chat_id)), 122)

    def tool_round(self):
        controller, view = self.controller()
        tools = [{'type': 'function', 'function': {'name': 'lookup', 'parameters': {'type': 'object', 'properties': {}}}}]
        self.tools = dict(format=None, keep_alive=None, tools=tools)
        with provider_patch('chat', side_effect=lambda **kw: iter(
                [{'message': {'tool_calls': [tool_call(), tool_call()]}, 'done': True}])):
            controller.send(self.input('look it up', request=self.tools))
            self.settle(controller)
        self.assertIsNotNone(controller.pending_round)
        return controller, view

    def test_tool_round_continues_only_when_every_result_is_given(self):
        controller, view = self.tool_round()
        pending = controller.pending_round
        self.assertFalse(controller.send(self.input('ignored', request=self.tools)))
        self.assertFalse(controller.can_send(continuation=True))
        controller.save_tool_result(pending, 0, 'first')
        self.assertTrue(controller.tool_busy)
        controller.save_tool_result(pending, 1, 'ignored while saving')
        self.settle(controller)
        self.assertFalse(controller.can_send(continuation=True))
        controller.save_tool_result(pending, 1, 'second')
        self.settle(controller)
        self.assertEqual(self.storage.get_chat(controller.chat_id)['messages'][-1]['response_metadata']['tool_round']['results'],
                         ['first', 'second'])
        with provider_patch('chat', side_effect=answer('done')) as chat:
            self.assertTrue(controller.send(self.input('not sent', request=self.tools), continuation=True))
            self.settle(controller)
        sent = chat.call_args.kwargs['messages']
        self.assertEqual([m['content'] for m in sent if m['role'] == 'tool'], ['first', 'second'])
        self.assertNotIn('not sent', [m.get('content') for m in sent])
        self.assertIsNone(controller.pending_round)
        self.assertEqual(self.storage.get_chat(controller.chat_id)['messages'][-1]['content'], 'done')

    def test_cancelled_tool_round_frees_the_conversation(self):
        controller, view = self.tool_round()
        controller.cancel_tool_round()
        self.settle(controller)
        self.assertIsNone(controller.pending_round)
        saved = self.storage.get_chat(controller.chat_id)['messages']
        self.assertEqual(next(m for m in saved if m['role'] == 'assistant')['response_metadata']['tool_round']['state'], 'cancelled')
        with provider_patch('chat', side_effect=answer('fine')):
            self.assertTrue(controller.send(self.input('carry on', request=self.tools)))
            self.settle(controller)
        self.assertEqual(controller.history[-1]['content'], 'fine')

    def test_delete_during_a_request_discards_the_conversation(self):
        controller, view = self.controller()
        started, closed = threading.Event(), []

        def stalled(**kw):
            yield {'message': {'content': 'partial'}}
            started.set()
            while not kw['cancellable'].is_cancelled():
                time.sleep(.005)
        with provider_patch('chat', side_effect=stalled):
            controller.send(self.input())
            pump_until(started.is_set)
            chat_id = controller.chat_id
            controller.close(lambda: closed.append(True), delete=True)
            self.storage.delete_chat(chat_id)
            self.assertFalse(closed)
            pump_until(lambda: bool(closed))
        self.assertTrue(controller.disposed)
        self.assertIsNone(controller.request)
        self.assertNotIn('title_changed', view.names())
        pump_until(lambda: self.storage.writer.idle)
        self.assertIsNone(self.storage.get_chat(chat_id))
        self.assertFalse(controller.send(self.input()))

    def test_close_waits_for_pending_saves_and_attachments(self):
        controller, view = self.controller()
        with provider_patch('chat', side_effect=answer()):
            controller.send(self.input())
            self.settle(controller)
        release, closed = threading.Event(), []
        self.storage.submit(release.wait, 5)
        view.pending = True
        controller.close(lambda: closed.append(True))
        release.set()
        pump_until(lambda: self.storage.writer.idle and not self.storage.pending_callbacks)
        self.assertFalse(closed, 'an image import still holds the close')
        view.pending = False
        controller.view_ready()
        self.assertFalse(closed, 'the draft is saved before the close is reported')
        pump_until(lambda: bool(closed))
        self.assertTrue(controller.disposed)

        blocked, closed = self.controller()[0], []
        release.clear()
        self.storage.submit(release.wait, 5)
        blocked.close(lambda: closed.append(True))
        for _ in range(20):
            GLib.MainContext.default().iteration(False)
            time.sleep(.005)
        self.assertFalse(closed, 'an earlier write is still pending')
        release.set()
        pump_until(lambda: bool(closed))


if __name__ == '__main__':
    unittest.main()
