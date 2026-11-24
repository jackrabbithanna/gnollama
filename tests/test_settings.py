"""Typed request settings: what is sent, what is only kept, and what is stored."""
import copy
import json
import tempfile
import time
import unittest

from gi.repository import Gdk

from legacy_schema import downgrade_to_version_three
from provider_fixture import provider_patch
from src.app_context import AppContext
from src.chat_controller import ComposerInput
from src.database import DatabaseManager
from src.knowledge import DEFAULT_RAG
from src.providers.registry import PROVIDERS
from src.records import ModelTarget, RequestSettings, ViewState, WireOptions
from src.session import ChatStrategy, GenerationStrategy, RequestState
import test_chat_controller
from test_chat_controller import answer

SCHEMA = {'type': 'object'}
TOOLS = [{'type': 'function', 'function': {'name': 'lookup', 'parameters': {'type': 'object'}}}]

# A chat's options exactly as ChatStrategy.save wrote them before the settings were typed.
CHAT_OPTIONS = dict(
    temperature=0.2, num_ctx=4096, stop=['END'], thinking_val='high', logprobs=True, top_logprobs=3,
    show_stats=False, output_mode='schema', schema_text='{"type": "object"}', keep_alive=300,
    tools_enabled=True, tools_text='tools source', knowledge=dict(DEFAULT_RAG, enabled=False))

# A comparison target's settings as prepare_run stored them.
COMPARISON_TARGET = dict(
    output_mode='schema', schema_text='{"type": "object"}', format=SCHEMA, keep_alive=300, tools=TOOLS,
    tools_enabled=True, tools_text='tools source', options={'num_ctx': 2048}, system='Be brief.',
    thinking=True, logprobs=True, top_logprobs=2, show_stats=False,
    knowledge=dict(DEFAULT_RAG, enabled=False), query_override='rewritten',
    draft_settings={'panel': {'fields': {}}, 'model': None}, host='http://localhost:11434',
    host_id='host-id', model='one', endpoint='chat')

# A model-conversation participant as validate_participants stored it.
PARTICIPANT = dict(
    host_id='host-id', model='one', system='You are A.', options={'temperature': 0.5}, thinking=None,
    keep_alive=None, logprobs=False, top_logprobs=None, show_stats=True, host='http://localhost:11434',
    endpoint='chat', format=None, tools=None)

FUTURE = {'future_option': {'nested': [1, 2]}}

# Anything a provider is sent besides the wire options.
ENVELOPE = {'model', 'messages', 'prompt', 'stream', 'images'}
WIRE_NAMES = {'options', 'system', 'think', 'format', 'keep_alive', 'tools', 'logprobs', 'top_logprobs'}


class RecordingTransport:
    """Stands in for Transport and keeps each request body instead of sending it."""
    def __init__(self):
        self.bodies = []

    def stream_lines(self, url, data, *args, **kwargs):
        self.bodies.append(data())
        return iter([{'message': {'content': 'ok'}, 'response': 'ok', 'done': True}])

    stream_json = stream_lines


class StoredSettingsTests(unittest.TestCase):
    def test_stored_forms_round_trip(self):
        for stored, sparse in ((COMPARISON_TARGET, False), (PARTICIPANT, True)):
            with self.subTest(sparse=sparse):
                settings = RequestSettings.from_stored(stored)
                self.assertEqual(settings.to_stored(sparse=sparse), stored)
                self.assertEqual(RequestSettings.from_stored(settings.to_stored()), settings)
        settings = RequestSettings.from_chat_options(CHAT_OPTIONS)
        self.assertEqual(settings.to_chat_options(), CHAT_OPTIONS)
        self.assertEqual(settings.wire.options, {'temperature': 0.2, 'num_ctx': 4096, 'stop': ['END']})
        self.assertEqual(settings.wire.thinking, 'high')
        self.assertFalse(settings.view.show_stats)

    def test_unknown_stored_keys_survive_and_are_never_sent(self):
        chat = RequestSettings.from_chat_options(dict(CHAT_OPTIONS, **FUTURE))
        self.assertEqual(chat.view.extra, FUTURE)
        self.assertEqual(chat.to_chat_options(), dict(CHAT_OPTIONS, **FUTURE))
        self.assertNotIn('future_option', chat.wire.options)
        flat = RequestSettings.from_stored(dict(COMPARISON_TARGET, **FUTURE))
        self.assertEqual(flat.to_stored(), dict(COMPARISON_TARGET, **FUTURE))
        self.assertNotIn('future_option', json.dumps(flat.api_details()))

    def test_stored_records_with_missing_or_null_keys_load_with_defaults(self):
        settings = RequestSettings.from_stored(dict(model='test', host='http://localhost:11434', options=None,
                                                    knowledge=None, tools_text=None))
        self.assertEqual(settings.wire, WireOptions())
        self.assertEqual(settings.view, ViewState())
        self.assertEqual(settings.target, ModelTarget(None, 'http://localhost:11434', 'test'))
        self.assertEqual(RequestSettings.from_chat_options(None), RequestSettings())

    def test_details_are_the_destination_and_wire_options_only(self):
        details = RequestSettings.from_stored(COMPARISON_TARGET).api_details()
        self.assertEqual(set(details), {'host', 'model', 'endpoint'} | {f for f in WireOptions.__dataclass_fields__})
        self.assertEqual(details['format'], SCHEMA)
        self.assertEqual(details['host'], 'http://localhost:11434')

    def test_composer_values_are_sorted_once_and_copied(self):
        knowledge, options = dict(DEFAULT_RAG, enabled=True), {'seed': 1}
        input = ComposerInput(host={'id': 'host-id'}, model='one', options=options, knowledge=knowledge,
                              request=dict(format=SCHEMA, tools=TOOLS, keep_alive=5, output_mode='schema',
                                           schema_text='source', tools_enabled=True, tools_text='tools'),
                              show_stats=False, query_override='query')
        settings = input.settings('http://localhost:11434', 'chat', extra=FUTURE)
        knowledge['enabled'] = False
        options['seed'] = 2
        self.assertEqual(settings.wire, WireOptions(options={'seed': 1}, format=SCHEMA, tools=TOOLS, keep_alive=5))
        self.assertEqual(settings.view, ViewState(
            show_stats=False, output_mode='schema', schema_text='source', tools_enabled=True, tools_text='tools',
            knowledge=dict(DEFAULT_RAG, enabled=True), query_override='query', extra=FUTURE))
        self.assertEqual(settings.target, ModelTarget('host-id', 'http://localhost:11434', 'one'))

    def test_request_bodies_hold_only_wire_options(self):
        view = ViewState(output_mode='schema', schema_text='VIEW-schema', tools_enabled=True,
                         tools_text='VIEW-tools', knowledge={'note': 'VIEW-knowledge'},
                         query_override='VIEW-query', draft_settings={'note': 'VIEW-draft'},
                         extra={'VIEW-key': 'VIEW-extra'})
        wire = WireOptions(options={'temperature': 0.5}, system='Be brief.', thinking=True, format=SCHEMA,
                           keep_alive=60, tools=TOOLS, logprobs=True, top_logprobs=2)
        for kind, cls in PROVIDERS.items():
            for strategy in (ChatStrategy(None), GenerationStrategy()):
                with self.subTest(provider=kind, strategy=type(strategy).__name__):
                    transport = RecordingTransport()
                    provider = cls(cls.fixed_url or 'http://localhost:11434', 'host', transport=transport)
                    settings = RequestSettings(ModelTarget('VIEW-host-id', provider.url, 'test'), wire, view,
                                               history_images_omitted=True)
                    state = RequestState(settings, 'hello', connection=provider,
                                         messages=[{'role': 'user', 'content': 'hello'}])
                    list(strategy.process(state))
                    body, = transport.bodies
                    self.assertLessEqual(set(body), ENVELOPE | WIRE_NAMES)
                    self.assertEqual(body['options'], wire.options)
                    self.assertEqual(body['think'], True)
                    self.assertNotIn('VIEW-', json.dumps(body))


class StoredChatTests(unittest.TestCase):
    # The controller fixture, without running its tests a second time.
    fixture = test_chat_controller.ChatControllerTests
    setUp, tearDown = fixture.setUp, fixture.tearDown
    controller, input, settle = fixture.controller, fixture.input, fixture.settle
    del fixture

    def test_chat_written_by_this_release_round_trips_through_a_request(self):
        written = dict(CHAT_OPTIONS, **FUTURE)
        chat_id = self.storage.create_chat('test')['id']
        self.storage.save_chat(chat_id, [], model='test', options=written, system='Be brief.', host=self.host['id'])
        self.storage.writer.flush().result(2)
        stored = RequestSettings.from_chat_options(self.storage.get_chat(chat_id)['options'])
        controller, view = self.controller(chat_id=chat_id)
        controller.extra = stored.view.extra
        view.options = dict(tools_enabled=stored.view.tools_enabled, tools_text=stored.view.tools_text,
                            knowledge=copy.deepcopy(stored.view.knowledge))
        composed = self.input(
            options=stored.wire.options, thinking=stored.wire.thinking, logprobs=stored.wire.logprobs,
            top_logprobs=stored.wire.top_logprobs, show_stats=stored.view.show_stats, system='Be brief.',
            knowledge=stored.view.knowledge, request=dict(
                format=SCHEMA, tools=TOOLS, keep_alive=stored.wire.keep_alive, output_mode=stored.view.output_mode,
                schema_text=stored.view.schema_text, tools_enabled=stored.view.tools_enabled,
                tools_text=stored.view.tools_text))
        with provider_patch('chat', side_effect=answer('{}')) as chat:
            self.assertTrue(controller.send(composed))
            self.settle(controller)
        sent = chat.call_args.kwargs
        self.assertNotIn('future_option', json.dumps({k: v for k, v in sent.items() if k not in ('host', 'cancellable')}))
        saved = self.storage.get_chat(chat_id)
        self.assertEqual(saved['options'], written)
        self.assertEqual(saved['messages'][-1]['api_details'], controller.history[-1]['api_details'])
        self.assertNotIn('schema_text', saved['messages'][-1]['api_details'])

    def test_legacy_database_options_survive_load_save_reload(self):
        legacy = dict(temperature=0.7, seed=42, thinking_val=True, logprobs=False, top_logprobs=None,
                      show_stats=True, repeat_penalty=1.1)
        path = self.temp.name + '/v3.db'
        db = DatabaseManager(path)
        db.chats.create_chat('chat', 'old chat', 1, 1, 'model')
        db.chats.update_chat('chat', 'model', legacy, 'system', None, time.time())
        with db._get_conn() as conn:
            downgrade_to_version_three(conn)
        db = DatabaseManager(path)
        loaded = RequestSettings.from_chat_options(db.chats.get_chat('chat')['options'])
        self.assertEqual(loaded.wire.options, {'temperature': 0.7, 'seed': 42})
        self.assertEqual(loaded.view.extra, {'repeat_penalty': 1.1})
        db.chats.update_chat('chat', 'model', loaded.to_chat_options(), 'system', None, time.time())
        saved = db.chats.get_chat('chat')['options']
        self.assertEqual({key: saved[key] for key in legacy}, legacy)
        self.assertEqual(RequestSettings.from_chat_options(saved), loaded)


@unittest.skipUnless(Gdk.Display.get_default(), 'GTK tests require a display')
class StoredChatViewTests(unittest.TestCase):
    def test_tab_keeps_unknown_options_for_the_next_save(self):
        from src.tab import GenerationTab
        temp = tempfile.TemporaryDirectory()
        context = AppContext(temp.name)
        self.addCleanup(temp.cleanup)
        self.addCleanup(context.shutdown)
        chat_id = context.storage.create_chat('test')['id']
        context.storage.save_chat(chat_id, [], model='test', options=dict(CHAT_OPTIONS, **FUTURE))
        context.storage.writer.flush().result(2)
        with provider_patch('list_models', return_value=['test']), provider_patch('show_model', return_value={}):
            tab = GenerationTab(mode='chat', chat_id=chat_id, context=context)
            self.addCleanup(tab.chat_input.cancel_fetches)
            self.addCleanup(tab.draft.close, discard=True)
            self.assertEqual(tab.controller.extra, FUTURE)
            panel = tab.options_panel
            self.assertEqual(panel.temperature_entry.get_text(), '0.2')
            self.assertEqual(panel.schema_text, CHAT_OPTIONS['schema_text'])
            self.assertFalse(panel.stats_check.get_active())
            self.assertTrue(panel.logprobs_check.get_active())


if __name__ == '__main__':
    unittest.main()
