"""What one model takes, where a host's models differ: the panel, the stored chat, and every check before sending."""
import json
import unittest
from dataclasses import fields, replace
from unittest.mock import patch

from gi.repository import Gdk

from provider_fixture import provider_patch
from src.chat_controller import ChatController, ComposerInput, ModelLimits
from src.drafts import DraftController
from src.providers import PROVIDERS, Capabilities, OpenRouterProvider
from src.records import WireOptions
from src.tab import GenerationTab
from test_chat_controller import RecordingView
from test_lmstudio import HostFixture, recorded
from test_openrouter import AUDIO, EFFORTS, SWITCH, TEXT, VISION, Catalog
from test_ui import pump_until, wait_for_model

TOOLS = [{'type': 'function', 'function': {'name': 'lookup', 'parameters': {'type': 'object'}}}]
SERVER, MODEL, TARGETS = ('Not available on this server.', 'Not available for this model.',
                          'Not available for every target.')
EVERYTHING = ['include_reasoning', 'logprobs', 'max_tokens', 'min_p', 'reasoning', 'response_format', 'seed',
              'stop', 'temperature', 'tools', 'top_k', 'top_p']
FLAGS = [f.name for f in fields(Capabilities) if f.type is bool]


def catalog(name):
    models = recorded('openrouter_models.json')['data']
    return next(model for model in models if model['id'] == name)['supported_parameters']


def record(parameters, cls=OpenRouterProvider):
    return cls(cls.fixed_url).model_capabilities({'supported_parameters': parameters})


def offered(capabilities):
    return {name for name in FLAGS if getattr(capabilities, name)}


class NarrowingTests(unittest.TestCase):
    def test_a_model_keeps_what_its_parameters_list(self):
        own = offered(OpenRouterProvider.capabilities)
        cases = {
            TEXT: ({'sampling_extras', 'structured_output'}, ()),
            # Top K without Min P is not both, and the model takes no seed.
            VISION: (set(), ('seed',)),
            EFFORTS: (own, ()),
            AUDIO: ({'structured_output'}, ('stop',)),
        }
        for model, (flags, left_out) in cases.items():
            with self.subTest(model=model):
                narrowed = record(catalog(model))
                self.assertEqual((offered(narrowed), narrowed.left_out), (flags, left_out))
        # Either name of a pair is enough.
        for parameters, flag in ((['response_format'], 'structured_output'), (['structured_outputs'], 'structured_output'),
                                 (['reasoning_effort'], 'thinking_levels'), (['include_reasoning'], 'thinking_text')):
            self.assertEqual(offered(record(parameters)), {flag})
        self.assertEqual(offered(record(['reasoning'])), {'thinking_levels', 'thinking_text'})
        self.assertEqual(record(['max_completion_tokens']).left_out, ('temperature', 'top_p', 'seed', 'stop'))
        self.assertEqual(record(['max_tokens', 'temperature', 'top_p', 'stop']).left_out, ('seed',))

    def test_narrowing_never_adds(self):
        class Bare(OpenRouterProvider):
            capabilities = Capabilities(tools=True, options_notice='kept')
        self.assertEqual(record(EVERYTHING, Bare), Bare.capabilities)
        for model in recorded('openrouter_models.json')['data']:
            with self.subTest(model=model['id']):
                narrowed = record(model['supported_parameters'])
                self.assertLessEqual(offered(narrowed), offered(OpenRouterProvider.capabilities))
                self.assertLessEqual(offered(record(model['supported_parameters'], Bare)), {'tools'})

    def test_the_providers_record_stands_where_a_model_says_nothing(self):
        provider = OpenRouterProvider(OpenRouterProvider.fixed_url)
        # Loading or unreadable details, and a catalog entry without the list.
        for details in (None, {}, {'capabilities': ['completion']}, {'supported_parameters': None},
                        {'supported_parameters': []}, 'text'):
            with self.subTest(details=details):
                self.assertIs(provider.model_capabilities(details), OpenRouterProvider.capabilities)

    def test_other_types_give_every_model_the_providers_record(self):
        for kind, cls in PROVIDERS.items():
            if kind != 'openrouter':
                with self.subTest(kind=kind):
                    self.assertIs(record(['temperature'], cls), cls.capabilities)

    def test_a_record_names_and_refuses_what_its_model_lacks(self):
        wire = WireOptions(options=dict(temperature=0.5, seed=1, stop=['x'], num_predict=64, top_k=5), tools=TOOLS)
        vision = record(catalog(VISION))
        self.assertEqual(vision.unsupported(wire), ['Top K', 'Tool Calling', 'Seed'])
        self.assertEqual(replace(vision, left_out=('stop', 'num_predict', 'temperature')).unsupported(wire),
                         ['Top K', 'Tool Calling', 'Temperature', 'Stop sequences', 'Maximum output tokens'])
        with self.assertRaisesRegex(ValueError, r'^Router \(some/model\) does not support: Top K, Tool Calling, Seed.$'):
            vision.require(wire, dict(name='Router'), 'some/model')
        provider = OpenRouterProvider(OpenRouterProvider.fixed_url)
        details = {'supported_parameters': catalog(VISION)}
        # The host's own limits are named for the host, the model's for the model.
        with self.assertRaisesRegex(ValueError, r'^Router does not support: Model Retention.$'):
            provider.check(replace(wire, keep_alive=60), dict(name='Router'), 'some/model', details)
        with self.assertRaisesRegex(ValueError, r'^Router \(some/model\) does not support: Top K'):
            provider.check(wire, dict(name='Router'), 'some/model', details)
        provider.check(wire, dict(name='Router'), 'some/model', None)

    def test_what_two_models_share(self):
        text, vision = record(catalog(TEXT)), record(catalog(VISION))
        both = text.both(replace(vision, left_out=('seed', 'stop')))
        self.assertEqual((offered(both), both.left_out), (set(), ('seed', 'stop')))
        self.assertEqual(text.both(text), text)
        local = PROVIDERS['ollama'].capabilities
        self.assertEqual(offered(local.both(PROVIDERS['ollama_cloud'].capabilities)),
                         offered(PROVIDERS['ollama_cloud'].capabilities))
        self.assertEqual(local.both(local), local)

    def test_the_service_is_told_to_route_only_where_every_option_is_taken(self):
        provider = OpenRouterProvider(OpenRouterProvider.fixed_url)
        body = provider.request_body('test', [{'role': 'user', 'content': 'hello'}], {'top_k': 5})
        self.assertEqual((body['provider'], body['top_k']), ({'require_parameters': True}, 5))
        self.assertNotIn('provider', PROVIDERS['lmstudio']('http://localhost:1234').request_body('test', []))


def disabled(panel):
    """The controls of a panel that cannot be used, and the explanation each one shows."""
    panel.logprobs_check.set_active(True)
    lacking = {name: label.get_text() for name, (label, hint) in panel.field_hints.items()
               if not getattr(panel, name).get_parent().get_sensitive()}
    for name, widget in (('output', panel.output_dropdown), ('memory', panel.memory_group),
                         ('logprobs', panel.logprobs_check), ('tools', panel.tools_check)):
        if not widget.get_sensitive():
            lacking[name] = widget.get_tooltip_text() or ''
    return lacking


@unittest.skipUnless(Gdk.Display.get_default(), 'GTK tests require a display')
class OtherHostTests(unittest.TestCase):
    # What each type's panel disabled before models were asked, with the explanation it gave.
    BEFORE = dict(ollama={}, ollama_cloud=dict(output=SERVER, memory=''),
                  openai=dict(memory='', top_k_entry=SERVER, min_p_entry=SERVER, num_ctx_entry=SERVER),
                  lmstudio=dict(memory='', num_ctx_entry=SERVER))
    DETAILS = {'capabilities': ['completion'], 'supported_parameters': ['temperature']}

    def test_panels_of_other_types_are_as_they_were(self):
        from src.widgets.options_panel import OptionsPanel
        for kind, lacking in self.BEFORE.items():
            with self.subTest(kind=kind):
                cls = PROVIDERS[kind]
                panel = OptionsPanel()
                panel.tools_available = True
                panel.set_capabilities(cls(cls.fixed_url or 'http://localhost:1234').model_capabilities(self.DETAILS),
                                       cls.capabilities)
                self.assertEqual(disabled(panel), lacking)
                self.assertEqual(panel.provider_notice.get_text(), cls.capabilities.options_notice)
                self.assertEqual(panel.get_unsent(), {})

    def test_ollama_tab_ignores_what_a_model_lists(self):
        import tempfile
        from src.app_context import AppContext
        temp = tempfile.TemporaryDirectory()
        context = AppContext(temp.name)
        self.addCleanup(temp.cleanup)
        self.addCleanup(context.shutdown)
        with provider_patch('list_models', return_value=['test']), \
                provider_patch('show_model', return_value=self.DETAILS):
            tab = GenerationTab(mode='chat', chat_id=context.storage.create_chat('test')['id'], context=context)
            self.addCleanup(tab.chat_input.cancel_fetches)
            self.addCleanup(tab.draft.close, discard=True)
            wait_for_model(tab)
            self.assertEqual(disabled(tab.options_panel), {})
            self.assertIs(tab.options_panel.capabilities, PROVIDERS['ollama'].capabilities)


@unittest.skipUnless(Gdk.Display.get_default(), 'GTK tests require a display')
class RouterTests(Catalog, HostFixture, unittest.TestCase):
    def fill(self, panel):
        for name, text in dict(top_k_entry='40', min_p_entry='0.05', seed_entry='7', num_ctx_entry='8192',
                               temperature_entry='0.5', top_logprobs_entry='3').items():
            getattr(panel, name).set_text(text)
        panel.tools_text = json.dumps(TOOLS)
        panel.tools_check.set_active(True)
        panel.logprobs_check.set_active(True)
        panel.output_dropdown.set_selected(1)
        panel.keep_alive_dropdown.set_selected(2)

    def chats(self):
        return [body for path, body, key in self.server.received if path.endswith('/chat/completions')]

    def test_panel_follows_the_selected_model(self):
        tab = self.make_tab(TEXT)
        panel = tab.options_panel
        hints = {name: hint for name, (label, hint) in panel.field_hints.items()}
        self.assertEqual(disabled(panel), dict(memory='', num_ctx_entry=SERVER, tools='', logprobs='',
                                               top_logprobs_entry=MODEL))
        self.assertFalse(panel.tools_button.get_sensitive())
        self.assertEqual((panel.tools_notice.get_text(), panel.tools_notice.get_visible()), (MODEL, True))
        self.select(tab, EFFORTS)
        self.assertEqual(disabled(panel), dict(memory='', num_ctx_entry=SERVER))
        self.assertTrue(panel.tools_button.get_sensitive())
        self.assertEqual(panel.field_hints['top_logprobs_entry'][0].get_text(), hints['top_logprobs_entry'])
        panel.logprobs_check.set_active(False)
        self.assertFalse(panel.tools_notice.get_visible())
        # A sampling option the model does not list is disabled like the rest, and so is the output mode.
        self.select(tab, AUDIO)
        panel.stop_entry.set_text('END')
        panel.temperature_entry.set_text('0.5')
        self.assertEqual(disabled(panel), dict(
            memory='', num_ctx_entry=SERVER, tools='', logprobs='', top_logprobs_entry=MODEL,
            stop_entry=MODEL, top_k_entry=MODEL, min_p_entry=MODEL))
        self.assertEqual(panel.get_options_from_ui(), {'temperature': 0.5})
        self.select(tab, VISION)
        self.assertEqual((panel.output_dropdown.get_sensitive(), panel.output_dropdown.get_tooltip_text()),
                         (False, MODEL))
        self.assertEqual(panel.get_options_from_ui(), {'temperature': 0.5, 'stop': ['END']})
        self.select(tab, EFFORTS)
        self.assertIsNone(panel.output_dropdown.get_tooltip_text())
        self.assertEqual({name: label.get_text() for name, (label, hint) in panel.field_hints.items()
                          if name != 'num_ctx_entry'}, {name: hint for name, hint in hints.items()
                                                        if name != 'num_ctx_entry'})

    def test_held_values_are_stored_not_sent_and_apply_again(self):
        tab = self.make_tab(TEXT)
        chat_id = tab.controller.chat_id
        self.select(tab, EFFORTS)
        self.fill(tab.options_panel)
        self.assertEqual(tab.advanced.get_label(), 'Options: JSON, Tool Calling')
        self.select(tab, VISION)
        self.assertEqual(tab.advanced.get_label(), 'Options')
        held = dict(top_k='40', min_p='0.05', seed='7', num_ctx='8192', logprobs=True, top_logprobs='3',
                    output_mode='json', keep_alive=300, tools_enabled=True)
        self.assertEqual(tab.options_panel.get_unsent(), held)
        body = self.send(tab, 'hello')[1]
        self.assertEqual({name: body[name] for name in body if name not in ('model', 'messages', 'stream')},
                         dict(temperature=0.5, stream_options={'include_usage': True},
                              provider={'require_parameters': True}))
        saved = self.storage.get_chat(chat_id)
        self.assertEqual(saved['options']['unsent'], held)
        self.assertEqual((saved['options']['temperature'], saved['options']['tools_enabled'],
                          saved['options']['output_mode'], saved['options']['logprobs']), (0.5, False, 'text', False))
        self.assertFalse(set(saved['options']) & {'top_k', 'min_p', 'seed', 'num_ctx'})
        details = saved['messages'][-1]['api_details']
        self.assertEqual((details['options'], details['logprobs'], details['top_logprobs'], details['tools'],
                          details['format'], details['keep_alive']), ({'temperature': 0.5}, False, None, None, None, None))
        self.assertNotIn('unsent', details)
        tab.draft.close(discard=True)

        reopened = GenerationTab(mode='chat', chat_id=chat_id, context=self.context)
        self.tabs.append(reopened)
        wait_for_model(reopened, VISION)
        panel = reopened.options_panel
        self.assertEqual({name: getattr(panel, name + '_entry').get_text() for name in
                          ('top_k', 'min_p', 'seed', 'num_ctx', 'temperature', 'top_logprobs')},
                         dict(top_k='40', min_p='0.05', seed='7', num_ctx='8192', temperature='0.5', top_logprobs='3'))
        self.assertEqual((panel.tools_check.get_active(), panel.logprobs_check.get_active(),
                          panel.output_dropdown.get_selected(), panel.keep_alive_dropdown.get_selected()),
                         (True, True, 1, 2))
        self.assertEqual(panel.get_unsent(), held)
        self.assertFalse(panel.tools_check.get_sensitive())
        self.select(reopened, EFFORTS)
        body = self.send(reopened, 'again')[1]
        self.assertEqual({name: body[name] for name in ('top_k', 'min_p', 'seed', 'temperature', 'logprobs',
                                                         'top_logprobs', 'tools', 'response_format')},
                         dict(top_k=40, min_p=0.05, seed=7, temperature=0.5, logprobs=True, top_logprobs=3,
                              tools=TOOLS, response_format={'type': 'json_object'}))
        options = self.storage.get_chat(chat_id)['options']
        # Only what no model of this host takes is still held.
        self.assertEqual(options['unsent'], dict(num_ctx='8192', keep_alive=300))
        self.assertEqual((options['top_k'], options['seed'], options['tools_enabled'], options['output_mode']),
                         (40, 7, True, 'json'))

    def controller(self):
        chat_id = self.storage.create_chat('test')['id']
        controller = ChatController(self.context, RecordingView(), 'chat', chat_id=chat_id)
        controller.draft = DraftController(self.storage, 'chat', lambda: dict(text='', images=[], settings={}),
                                           chat_id=chat_id)
        self.addCleanup(controller.stop)
        return controller

    def test_a_request_built_without_the_view_cannot_carry_what_the_model_lacks(self):
        connection = self.context.connection(self.host)
        for model in (TEXT, VISION):
            self.context.services.catalog.details(connection, model)
        controller = self.controller()
        cases = [(TEXT, dict(request=dict(tools=TOOLS)), 'Tool Calling'),
                 (TEXT, dict(logprobs=True, top_logprobs=2), 'Return token probabilities'),
                 (VISION, dict(options={'top_k': 5}), 'Top K'),
                 (VISION, dict(options={'seed': 1}), 'Seed'),
                 (VISION, dict(request=dict(format='json')), 'Structured output')]
        for model, case, name in cases:
            with self.subTest(case=case), self.assertRaisesRegex(
                    ValueError, r'^Router \({0}\) does not support: {1}'.format(model, name)):
                controller.send(ComposerInput(prompt='hi', host=self.host, model=model, **case))
        self.assertEqual((controller.request, controller.view.events, self.chats()), (None, [], []))
        self.assertTrue(controller.send(ComposerInput(prompt='hi', host=self.host, model=TEXT,
                                                      options={'top_k': 5, 'min_p': 0.1, 'seed': 1})))
        pump_until(lambda: controller.request is None and self.storage.writer.idle and self.context.idle)
        self.assertEqual((self.chats()[-1]['top_k'], self.chats()[-1]['seed']), (5, 1))

    def test_limits_outlast_the_catalogs_memory_of_a_model(self):
        limits = ModelLimits(self.context)
        own = OpenRouterProvider.capabilities
        # Unknown details: the provider's record, as while they load.
        self.assertIs(limits.of(self.host, TEXT), own)
        self.assertIs(limits.of(self.host, None), own)
        self.assertIs(limits.of(None, None), PROVIDERS['ollama'].capabilities)
        self.context.services.catalog.details(self.context.connection(self.host), TEXT)
        self.assertFalse(limits.of(self.host, TEXT).tools)
        with patch.object(self.context.services.catalog, 'cached_details', return_value=None):
            self.assertFalse(limits.of(self.host, TEXT).tools)
            self.assertIs(limits.of(self.host, EFFORTS), own)

    def test_both_target_validators_refuse_what_a_model_lacks(self):
        for check in (self.prepare, self.validate):
            with self.subTest(check=check.__name__):
                with self.assertRaisesRegex(ValueError, r'Router \({0}\) does not support: Return token'.format(TEXT)):
                    check(self.targets(EFFORTS, TEXT), logprobs=True)
                with self.assertRaisesRegex(ValueError, r'Router \({0}\) does not support: Top K, Seed'.format(VISION)):
                    check(self.targets(EFFORTS, VISION), options={'top_k': 5, 'seed': 1})
                with self.assertRaisesRegex(ValueError, r'^Router does not support: Model Retention'):
                    check(self.targets(EFFORTS, SWITCH), keep_alive=60)
                check(self.targets(EFFORTS, SWITCH), logprobs=True, options={'top_k': 5, 'min_p': 0.1, 'seed': 1})
        with self.assertRaisesRegex(ValueError, r'\({0}\) does not support: Structured output, Tool'.format(VISION)):
            self.prepare(self.targets(EFFORTS, VISION), format='json', tools=TOOLS)

    def settle(self, pickers, *models):
        pump_until(lambda: all(p.input.get_selected_model() and not p.input._models_loading for p in pickers))
        for picker, model in zip(pickers, models):
            picker.input.select_model(model)
        pump_until(lambda: all(p.input.get_selected_model() == model and not p.input.capabilities_loading
                               for p, model in zip(pickers, models)) and self.context.services.idle)

    def test_comparison_panel_offers_what_every_target_takes(self):
        from src.widgets.comparison_view import ComparisonTab
        tab = ComparisonTab(self.context)
        self.tabs.append(tab)
        self.addCleanup(tab.prepare_shutdown)
        panel = tab.options_panel
        self.settle(tab.targets, EFFORTS, SWITCH)
        self.assertEqual(disabled(panel), dict(memory='', num_ctx_entry=TARGETS))
        self.settle(tab.targets, EFFORTS, VISION)
        self.assertEqual(disabled(panel), dict(
            memory='', num_ctx_entry=TARGETS, output=TARGETS, logprobs='', top_logprobs_entry=TARGETS,
            top_k_entry=TARGETS, min_p_entry=TARGETS, seed_entry=TARGETS, tools=''))
        # What is held for the second target's sake reaches neither, and is not lost.
        panel.top_k_entry.set_text('40')
        panel.temperature_entry.set_text('0.5')
        tab.chat_input.entry.restore_draft('compare')
        tab.send_or_stop()
        pump_until(lambda: tab.request is None and len(tab.results) == 2 and self.storage.writer.idle)
        self.assertFalse(tab.notice.get_visible(), tab.notice.get_text())
        bodies = self.chats()
        self.assertEqual(sorted(body['model'] for body in bodies), sorted([EFFORTS, VISION]))
        for body in bodies:
            self.assertEqual((body['temperature'], 'top_k' in body, 'logprobs' in body), (0.5, False, False))
        saved = self.storage.export_snapshot(tab.strategy.chat_id)
        for target in saved['targets']:
            self.assertEqual(target['settings']['unsent'], dict(top_k='40', logprobs=True))
            self.assertEqual(target['settings']['options'], {'temperature': 0.5})

    def test_each_participant_panel_follows_its_own_model(self):
        from src.widgets.model_target import TargetPicker
        pickers = [TargetPicker(self.context, lambda *args: None, dict(host_id=self.host['id'], model=model),
                                participant=True) for model in (VISION, EFFORTS)]
        for picker in pickers:
            self.addCleanup(picker.close)
        self.settle(pickers, VISION, EFFORTS)
        first, second = (picker.panel for picker in pickers)
        self.assertEqual(disabled(first), dict(
            memory='', num_ctx_entry=SERVER, output=MODEL, logprobs='', top_logprobs_entry=MODEL,
            top_k_entry=MODEL, min_p_entry=MODEL, seed_entry=MODEL, tools=''))
        self.assertEqual(disabled(second), dict(memory='', num_ctx_entry=SERVER))
        for panel in (first, second):
            panel.seed_entry.set_text('7')
            panel.temperature_entry.set_text('0.5')
        settings = [picker.request_settings() for picker in pickers]
        self.assertEqual([s.wire.options for s in settings], [{'temperature': 0.5}, {'temperature': 0.5, 'seed': 7}])
        self.assertEqual([(s.wire.logprobs, s.view.unsent) for s in settings],
                         [(False, dict(seed='7', logprobs=True)), (True, {})])
        self.settle(pickers, EFFORTS, VISION)
        self.assertEqual(pickers[0].request_settings().wire.options, {'temperature': 0.5, 'seed': 7})
        self.assertEqual(disabled(first), dict(memory='', num_ctx_entry=SERVER))


if __name__ == '__main__':
    unittest.main()
