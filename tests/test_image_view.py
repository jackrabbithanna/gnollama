"""The image tab: its models and settings, references, runs and their images, drafts, and its place in the window."""
import base64
import json
import os
import tempfile
import threading
import unittest
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from gi.repository import Adw, Gdk, Gio, Gtk

from src.app_context import AppContext
from src.providers import OllamaProvider, ProviderError, RequestCancelled
from src.providers.registry import PROVIDERS
from src.records import ImageSettings, has_saved_work
from src.widgets.image_results import ImageCard, ImageRunView
from src.widgets.image_view import ImageTab
from src.window import GnollamaWindow
from provider_fixture import LIMITED, limited_host, provider_patch
from test_ui import pump_until

ANSWER = json.loads((Path(__file__).parent / 'fixtures' / 'images_openrouter.json').read_text())['answer']
PNG = ANSWER['data'][0]['b64_json']
PAINTER = {'name': 'painter', 'capabilities': ['image', 'vision'], 'image_parameters': {
    'aspect_ratio': {'type': 'enum', 'values': ['1:1', '16:9']},
    'output_format': {'type': 'enum', 'values': ['png', 'webp']},
    'n': {'type': 'range', 'min': 1, 'max': 4}, 'seed': {'type': 'boolean'},
    'input_references': {'type': 'range', 'min': 0, 'max': 2}}}
PLAIN = {'name': 'plain', 'capabilities': ['image'], 'image_parameters': {
    'aspect_ratio': {'type': 'enum', 'values': ['16:9', '4:3']}}}
MODELS = [PLAIN, PAINTER]
IMAGING = replace(OllamaProvider.capabilities, image_generation=True, image_references=True)


def settled(tab):
    return tab.picker.input.get_selected_model() is not None


def answer(*images, text=None):
    """An images API answer with these images, as base64."""
    raw = {'data': [{'b64_json': image, 'media_type': 'image/png'} for image in images]}
    if text is not None:
        raw = {'choices': [{'message': {'content': text, 'images': []}}]}
    return raw


@unittest.skipUnless(Gdk.Display.get_default(), 'GTK tests require a display')
class ImageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.context = AppContext(self.temp.name)
        self.storage = self.context.storage
        self.host = self.storage.get_all_hosts()[0]
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(OllamaProvider, 'capabilities', IMAGING))
        self.listed = self.stack.enter_context(provider_patch('list_image_models', return_value=MODELS))
        self.stack.enter_context(provider_patch('list_models', return_value=['test']))
        self.stack.enter_context(provider_patch('show_model', return_value={'capabilities': ['completion']}))
        self.generate = self.stack.enter_context(provider_patch('generate_image', return_value=ANSWER))
        self.tabs, self.windows = [], []
        self.addCleanup(self.close)

    def close(self):
        for tab in self.tabs:
            tab.draft.close(discard=True)
            tab.chat_input.cancel_fetches()
            if hasattr(tab, 'picker'):
                tab.picker.input.cancel_fetches()
            if tab.request:
                tab.request.cancellable.cancel()
        pump_until(lambda: self.context.services.idle and all(t.request is None for t in self.tabs))
        pump_until(lambda: self.storage.writer.idle and not self.storage.pending_callbacks)
        self.context.shutdown()
        for window in self.windows:
            window.destroy()
        self.stack.close()
        self.temp.cleanup()

    def make_tab(self, prompt='A lighthouse at dusk', **kwargs):
        tab = ImageTab(self.context, **kwargs)
        self.tabs.append(tab)
        pump_until(lambda: settled(tab))
        if not kwargs:
            tab.chat_input.entry.restore_draft(prompt)
        return tab

    def run_tab(self, tab):
        tab.run_button.emit('clicked')
        pump_until(lambda: tab.request is None and self.storage.writer.idle and not self.storage.pending_callbacks)
        return self.storage.library.image_runs(tab.strategy.chat_id)

    def saved(self, tab):
        pump_until(lambda: self.storage.writer.idle and not self.storage.pending_callbacks)
        return self.storage.conversation_page(tab.strategy.chat_id)

    def stalled(self):
        """A `generate_image` that waits until it is released or cancelled."""
        entered, release = threading.Event(), threading.Event()
        def generate(provider, model, prompt, options, references=None, cancellable=None, **kwargs):
            entered.set()
            while not release.wait(0.01):
                if cancellable.is_cancelled():
                    raise RequestCancelled()
            return ANSWER
        self.generate.side_effect = generate
        return entered, release

    def find(self, widget, kind):
        found = [widget] if isinstance(widget, kind) else []
        child = widget.get_first_child()
        while child:
            found += self.find(child, kind)
            child = child.get_next_sibling()
        return found

    def card(self, view):
        card, = view.cards
        pump_until(lambda: card.seen is not None or card.placeholder.get_visible())
        return card

    # Models and settings.

    def test_models_are_the_hosts_image_models_and_the_form_offers_what_each_takes(self):
        tab = self.make_tab()
        self.assertEqual(tab.picker.input.kind, 'image')
        dropdown = tab.picker.input.model_dropdown
        names = [dropdown.get_model().get_string(i) for i in range(dropdown.get_model().get_n_items())]
        self.assertEqual(names, ['painter', 'plain'])
        self.assertEqual(tab.picker.input.get_selected_model(), 'painter')
        # Nothing is said of tools, thinking, or seeing images.
        self.assertFalse(tab.picker.input.model_summary_cell.get_visible())
        self.assertEqual(tab.picker.image_parameters().references, 2)
        form = tab.form
        self.assertEqual(list(form.rows), ['aspect_ratio', 'output_format', 'n', 'seed'])
        self.assertEqual([form.rows[name].get_title() for name in form.rows],
                         ['Aspect ratio', 'Output format', 'Number of images', 'Seed'])
        aspect = form.rows['aspect_ratio']
        self.assertIsInstance(aspect, Adw.ComboRow)
        self.assertEqual([aspect.get_model().get_string(i) for i in range(3)], ['Model default', '1:1', '16:9'])
        # PNG stands for the model's own output format.
        output = form.rows['output_format']
        self.assertEqual([output.get_model().get_string(i) for i in range(output.get_model().get_n_items())],
                         ['png', 'webp'])
        self.assertEqual((output.get_selected(), form.rows['n'].get_value()), (0, 1))
        self.assertEqual(form.options(), {'output_format': 'png'})
        self.assertTrue(tab.run_button.get_sensitive())
        self.assertEqual(tab.run_button.get_label(), 'Generate')
        self.assertFalse(tab.chat_input.send_button.get_visible())
        tab.picker.input.select_model('plain')
        self.assertEqual(list(form.rows), ['aspect_ratio'])
        self.assertEqual(form.options(), {})

    def test_chosen_settings_are_sent_and_kept_for_the_next_model_that_takes_them(self):
        tab = self.make_tab()
        form = tab.form
        form.rows['aspect_ratio'].set_selected(2)
        form.rows['n'].set_value(3)
        form.rows['seed'].set_text('42')
        self.assertEqual(form.options(), {'aspect_ratio': '16:9', 'output_format': 'png', 'n': 3, 'seed': 42})
        self.assertTrue(tab.draft.dirty)
        tab.picker.input.select_model('plain')
        self.assertEqual(form.options(), {'aspect_ratio': '16:9'})
        tab.picker.input.select_model('painter')
        self.assertEqual(form.options(), {'aspect_ratio': '16:9', 'output_format': 'png', 'n': 3, 'seed': 42})
        # Back at its default, a setting is not sent.
        form.rows['n'].set_value(1)
        form.rows['aspect_ratio'].set_selected(0)
        self.assertEqual(form.options(), {'output_format': 'png', 'seed': 42})
        runs = self.run_tab(tab)
        self.assertEqual(self.generate.call_args.args[1:], ('painter', 'A lighthouse at dusk',
                                                            {'output_format': 'png', 'seed': 42}))
        self.assertEqual(runs[0]['options'], {'output_format': 'png', 'seed': 42})
        # A seed that is no number is named and holds the run back.
        form.rows['seed'].set_text('soon')
        self.assertIn('error', form.rows['seed'].get_css_classes())
        self.assertEqual(tab.notice.get_text(), 'This model does not take that value for Seed.')
        self.assertFalse(tab.run_button.get_sensitive())
        form.rows['seed'].set_text('')
        self.assertNotIn('error', form.rows['seed'].get_css_classes())
        self.assertEqual(form.options(), {'output_format': 'png'})
        self.assertTrue(tab.run_button.get_sensitive())

    def test_an_empty_prompt_holds_generate_back_without_a_notice(self):
        tab = self.make_tab('')
        self.assertFalse(tab.run_button.get_sensitive())
        self.assertFalse(tab.notice.get_visible())
        tab.run()
        pump_until(lambda: self.context.services.idle)
        self.generate.assert_not_called()

    def test_a_host_without_image_generation_is_listed_and_blocked_with_the_reason(self):
        with patch.dict(PROVIDERS, LIMITED):
            limited_host(self.storage)
            tab = self.make_tab()
            calls = self.listed.call_count
            names = [host['name'] for host in tab.picker.panel.host_list]
            tab.picker.panel.host_dropdown.set_selected(names.index('Limited'))
            notice = tab.picker.input.capability_notice
            self.assertEqual(notice.get_text(), 'This server does not offer image generation.')
            self.assertTrue(notice.get_visible())
            self.assertIsNone(tab.picker.input.get_selected_model())
            self.assertFalse(tab.form.get_visible())
            self.assertFalse(tab.run_button.get_sensitive())
            tab.run()
            pump_until(lambda: self.context.services.idle)
            self.assertEqual(self.listed.call_count, calls)
            self.generate.assert_not_called()
            tab.picker.panel.host_dropdown.set_selected(names.index(self.host['name']))
            pump_until(lambda: settled(tab))
            self.assertFalse(notice.get_visible())
            self.assertTrue(tab.run_button.get_sensitive())

    def test_ollama_lists_no_image_models(self):
        self.stack.enter_context(patch.object(OllamaProvider, 'capabilities', replace(IMAGING, image_generation=False)))
        tab = ImageTab(self.context)
        self.tabs.append(tab)
        notice = tab.picker.input.capability_notice
        self.assertEqual(notice.get_text(), 'This server does not offer image generation.')
        self.listed.assert_not_called()

    def test_a_host_with_no_image_models_says_so(self):
        self.listed.return_value = []
        tab = ImageTab(self.context)
        self.tabs.append(tab)
        notice = tab.picker.input.capability_notice
        pump_until(lambda: notice.get_text() == 'No image models found on this server.')
        self.assertIsNone(tab.picker.input.get_selected_model())

    # References.

    def test_references_are_attached_where_the_model_takes_them_and_say_why_where_not(self):
        tab = self.make_tab()
        attach = tab.chat_input.attach_button
        self.assertIs(attach.get_parent().get_parent(), tab.attachments)
        self.assertTrue(attach.get_sensitive())
        self.assertEqual(attach.get_tooltip_text(), 'Attach Image')
        tab.chat_input.restore_images([PNG])
        self.assertEqual(tab.references(), [PNG])
        tab.picker.input.select_model('plain')
        self.assertFalse(attach.get_sensitive())
        self.assertEqual(attach.get_tooltip_text(), 'This model does not take reference images.')
        self.assertEqual(tab.notice.get_text(), 'This model does not take reference images.')
        self.assertFalse(tab.run_button.get_sensitive())
        tab.picker.input.select_model('painter')
        tab.chat_input.restore_images([PNG] * 3)
        self.assertEqual(tab.notice.get_text(), 'This model takes at most 2 reference images.')
        tab.chat_input.restore_images([PNG])
        runs = self.run_tab(tab)
        self.assertEqual(self.generate.call_args.kwargs['references'], [PNG])
        self.assertEqual(runs[0]['references'], [PNG])
        view = tab.runs[runs[0]['id']]
        self.assertTrue(view.references.get_visible())

    # Runs.

    def test_a_run_shows_its_images_settings_and_cost_and_is_saved_as_done(self):
        tab = self.make_tab()
        tab.form.rows['aspect_ratio'].set_selected(1)
        updates = []
        tab.connect('chat-updated', lambda tab, id, title: updates.append((id, title)))
        self.assertEqual(tab.title, 'New Image')
        runs = self.run_tab(tab)
        self.assertEqual([(run['status'], run['prompt']) for run in runs], [('done', 'A lighthouse at dusk')])
        self.assertEqual(tab.title, 'A lighthouse at dusk')
        self.assertEqual(updates[-1], (tab.strategy.chat_id, 'A lighthouse at dusk'))
        view = tab.runs[runs[0]['id']]
        self.assertIn('Run 1', view.title.get_text())
        self.assertIn('painter', view.title.get_text())
        self.assertFalse(view.spinner.get_visible())
        self.assertEqual(view.prompt.get_text(), 'A lighthouse at dusk')
        self.assertEqual(view.settings.get_text(), 'Aspect ratio: 1:1 · Output format: png')
        self.assertEqual(view.usage.get_text(), 'Cost: $0')
        self.assertFalse(view.error.get_visible() or view.text.get_visible() or view.references.get_visible())
        card = self.card(view)
        self.assertTrue(card.picture.get_visible())
        self.assertFalse(card.placeholder.get_visible())
        self.assertEqual(card.picture.get_alternative_text(), 'A lighthouse at dusk')
        self.assertTrue(card.save_button.get_sensitive() and card.copy_button.get_sensitive())
        # The editor keeps what was asked, for the next run.
        self.assertEqual(tab.prompt(), 'A lighthouse at dusk')
        self.assertEqual(tab.form.options(), {'aspect_ratio': '1:1', 'output_format': 'png'})
        self.assertTrue(tab.run_button.get_sensitive())
        self.assertIsNone(self.storage.get_draft(tab.draft.id))

    def test_an_image_is_saved_as_it_came_and_copied_at_full_size(self):
        tab = self.make_tab()
        runs = self.run_tab(tab)
        card = self.card(tab.runs[runs[0]['id']])
        self.assertEqual(card.file_name(), 'image-1.png')
        path = os.path.join(self.temp.name, 'saved.png')
        errors = []
        card.write(Gio.File.new_for_path(path), errors.append)
        pump_until(lambda: errors)
        self.assertEqual(errors, [None])
        self.assertEqual(Path(path).read_bytes(), base64.b64decode(PNG))
        copied = []
        card.copy(lambda: copied.append(True))
        pump_until(lambda: copied)
        self.assertTrue(card.get_clipboard().get_formats().contain_gtype(Gdk.Texture))
        self.assertEqual(ImageCard(dict(data=PNG, media_type='image/webp'), 2, '').file_name(), 'image-3.webp')

    def test_an_image_is_used_as_a_reference_where_the_model_takes_references(self):
        tab = self.make_tab()
        runs = self.run_tab(tab)
        view = tab.runs[runs[0]['id']]
        card = self.card(view)
        self.assertTrue(card.use_button.get_sensitive())
        card.use_button.emit('clicked')
        self.assertEqual(tab.references(), [PNG])
        self.assertTrue(tab.draft.dirty)
        # Added beside what is attached, without discarding files still being read.
        generation = tab.chat_input._image_generation
        tab.use_reference(PNG)
        self.assertEqual((tab.references(), tab.chat_input._image_generation), ([PNG, PNG], generation))
        tab.chat_input.restore_images([PNG])
        tab.picker.input.select_model('plain')
        self.assertFalse(card.use_button.get_sensitive())
        card.use_button.emit('clicked')
        self.assertEqual(tab.references(), [PNG])
        tab.picker.input.select_model('painter')
        tab.chat_input.restore_images([])
        # Not while a run is on its way.
        entered, release = self.stalled()
        tab.run_button.emit('clicked')
        pump_until(entered.is_set)
        self.assertFalse(card.use_button.get_sensitive())
        tab.use_reference(PNG)
        self.assertEqual(tab.references(), [])
        release.set()
        pump_until(lambda: tab.request is None and self.storage.writer.idle)
        self.assertTrue(card.use_button.get_sensitive())

    def test_an_image_that_cannot_be_shown_can_still_be_saved(self):
        self.generate.return_value = answer('aGVsbG8=')
        tab = self.make_tab()
        runs = self.run_tab(tab)
        self.assertEqual(runs[0]['status'], 'done')
        card = self.card(tab.runs[runs[0]['id']])
        self.assertTrue(card.placeholder.get_visible())
        self.assertFalse(card.picture.get_visible())
        self.assertIn('This image cannot be shown here. Use Save Image… to open it in another app.', [label.get_text() for label in self.find(card, Gtk.Label)])
        self.assertTrue(card.save_button.get_sensitive())
        self.assertFalse(card.copy_button.get_sensitive() or card.use_button.get_sensitive())

    def test_an_answer_without_an_image_and_a_failure_say_so(self):
        self.generate.return_value = answer(text='I cannot draw that.')
        tab = self.make_tab()
        runs = self.run_tab(tab)
        self.generate.side_effect = ProviderError('No such model.')
        runs = self.run_tab(tab)
        first, second = (tab.runs[run['id']] for run in runs)
        self.assertIn('Failed', first.title.get_text())
        self.assertEqual((first.text.get_text(), first.error.get_text()),
                         ('I cannot draw that.', 'The model answered without an image.'))
        self.assertFalse(first.gallery.get_visible())
        self.assertEqual((second.error.get_text(), second.text.get_visible()), ('No such model.', False))

    def test_stop_cancels_the_run_and_a_second_cannot_start_meanwhile(self):
        tab = self.make_tab()
        entered, release = self.stalled()
        tab.run_button.emit('clicked')
        pump_until(entered.is_set)
        view, = tab.runs.values()
        self.assertTrue(view.spinner.get_visible())
        self.assertIn('Running…', view.title.get_text())
        self.assertEqual(tab.run_button.get_label(), 'Stop')
        self.assertFalse(tab.chat_input.get_sensitive() or tab.form.get_sensitive() or tab.picker.get_sensitive())
        tab.run()
        tab.run_button.emit('clicked')
        pump_until(lambda: tab.request is None and self.storage.writer.idle and not self.storage.pending_callbacks)
        release.set()
        run, = self.storage.library.image_runs(tab.strategy.chat_id)
        self.assertEqual(run['status'], 'cancelled')
        self.assertIn('Cancelled', view.title.get_text())
        self.assertEqual(self.generate.call_count, 1)

    def test_a_reopened_session_shows_its_runs_and_restores_the_editor(self):
        tab = self.make_tab()
        tab.form.rows['aspect_ratio'].set_selected(2)
        self.run_tab(tab)
        self.generate.side_effect = ProviderError('Gone.')
        tab.chat_input.entry.restore_draft('A second lighthouse')
        tab.chat_input.restore_images([PNG])
        runs = self.run_tab(tab)
        page = self.saved(tab)
        tab.draft.close(discard=True)
        again = self.make_tab(saved=page)
        pump_until(lambda: again.picker.input.get_selected_model() == 'painter')
        self.assertEqual((again.strategy.chat_id, again.title, again.mode), (page['id'], 'A lighthouse at dusk', 'image'))
        self.assertEqual(list(again.runs), [run['id'] for run in runs])
        first, second = (again.runs[run['id']] for run in runs)
        self.assertEqual(len(first.cards), 1)
        self.assertIn('Failed', second.title.get_text())
        self.assertEqual(second.error.get_text(), 'Gone.')
        self.assertEqual((again.prompt(), again.references()), ('A second lighthouse', [PNG]))
        self.assertEqual(again.form.options(), {'aspect_ratio': '16:9', 'output_format': 'png'})
        self.assertFalse(again.draft.dirty)
        self.assertTrue(again.run_button.get_sensitive())
        self.generate.side_effect = None
        self.assertEqual(len(self.run_tab(again)), 3)

    def test_a_run_left_running_reads_as_interrupted_after_a_restart(self):
        tab = self.make_tab()
        self.run_tab(tab)
        with self.storage.db._get_conn() as conn:
            conn.execute("UPDATE image_runs SET status='running'")
            conn.commit()
        self.storage.db.images.interrupt_image_runs()
        again = self.make_tab(saved=self.saved(tab))
        view = next(iter(again.runs.values()))
        self.assertIn('Interrupted', view.title.get_text())
        self.assertFalse(view.spinner.get_visible())

    def test_earlier_runs_are_loaded_on_request_and_a_found_run_is_brought_up(self):
        library = self.storage.library
        settings = ImageSettings.from_stored(dict(target=dict(host_id=self.host['id'], model='painter')))
        for index in range(55):
            library.create_image_run(dict(settings.to_stored(), id=f'run-{index}', session_id='long',
                                          new_session=index == 0, prompt=f'Prompt {index}', references=[]))
            library.finish_image_run(f'run-{index}', 'failed', None, 'Gone.')
        tab = self.make_tab(saved=self.storage.conversation_page('long'))
        self.assertEqual(list(tab.runs), [f'run-{index}' for index in range(5, 55)])
        self.assertTrue(tab.older.get_visible())
        self.assertEqual(tab.prompt(), 'Prompt 54')
        tab.older.emit('clicked')
        pump_until(lambda: len(tab.runs) == 55)
        self.assertFalse(tab.older.get_visible())
        self.assertEqual([view.run['id'] for view in self.find(tab.results, ImageRunView)],
                         [f'run-{index}' for index in range(55)])
        other = self.make_tab(saved=self.storage.conversation_page('long'))
        other.show_message('run-1')
        pump_until(lambda: 'run-1' in other.runs)
        self.assertEqual(list(other.runs), [f'run-{index}' for index in range(50)])
        other.show_message('nothing')
        pump_until(lambda: self.context.services.idle)
        self.assertEqual(len(other.runs), 50)
        # Later runs are offered, and loaded up to the session's last.
        self.assertTrue(other.later.get_visible())
        other.later.emit('clicked')
        pump_until(lambda: len(other.runs) == 55)
        self.assertFalse(other.later.get_visible())
        self.assertEqual([view.run['id'] for view in self.find(other.results, ImageRunView)],
                         [f'run-{index}' for index in range(55)])

    def test_a_session_opened_at_a_search_hit_edits_its_last_run_and_new_runs_follow_it(self):
        library = self.storage.library
        settings = ImageSettings.from_stored(dict(target=dict(host_id=self.host['id'], model='painter')))
        for index in range(55):
            library.create_image_run(dict(settings.to_stored(), id=f'run-{index}', session_id='long',
                                          new_session=index == 0, prompt=f'Prompt {index}', references=[]))
            library.finish_image_run(f'run-{index}', 'failed', None, 'Gone.')
        tab = self.make_tab(saved=self.storage.conversation_page('long', 'run-1'))
        self.assertEqual(list(tab.runs), [f'run-{index}' for index in range(50)])
        self.assertTrue(tab.later.get_visible())
        # The editor holds the session's last run, not the last one on the page.
        self.assertEqual(tab.prompt(), 'Prompt 54')
        pump_until(lambda: tab.run_button.get_sensitive())
        tab.run_button.emit('clicked')
        pump_until(lambda: tab.request is None and self.storage.writer.idle and not self.storage.pending_callbacks
                   and 'run-0' not in tab.runs)
        new = library.image_runs('long')[-1]['id']
        # The new run follows the session's last, on the last page.
        self.assertEqual(list(tab.runs), [f'run-{index}' for index in range(6, 55)] + [new])
        self.assertFalse(tab.later.get_visible())

    # Drafts.

    def test_an_untouched_tab_saves_nothing_and_edits_are_saved_restored_and_discarded(self):
        tab = ImageTab(self.context)
        self.tabs.append(tab)
        pump_until(lambda: settled(tab))
        self.assertFalse(has_saved_work(draft=tab.draft.snapshot()))
        self.assertFalse(tab.draft.dirty)
        tab.picker.input.select_model('plain')
        self.assertTrue(tab.draft.dirty)
        tab.picker.input.select_model('painter')
        tab.chat_input.entry.restore_draft('A draft lighthouse')
        tab.chat_input.restore_images([PNG])
        tab.form.rows['seed'].set_text('7')
        tab.draft.flush()
        pump_until(lambda: self.storage.writer.idle)
        draft = self.storage.get_draft(tab.draft.id)
        self.assertEqual((draft['mode'], draft['text'], draft['images'], draft['chat_id']),
                         ('image', 'A draft lighthouse', [PNG], None))
        settings = ImageSettings.from_stored(draft['settings'])
        self.assertEqual((settings.options, settings.target.host_id, settings.target.model),
                         ({'output_format': 'png', 'seed': 7}, self.host['id'], 'painter'))
        restored = self.make_tab(draft=draft)
        pump_until(lambda: restored.picker.input.get_selected_model() == 'painter')
        self.assertEqual(restored.draft.id, tab.draft.id)
        self.assertEqual((restored.prompt(), restored.references(), restored.form.options()),
                         ('A draft lighthouse', [PNG], {'output_format': 'png', 'seed': 7}))
        self.assertFalse(restored.draft.dirty)
        self.assertEqual(restored.title, 'New Image')
        # An open editor keeps edits newer than the draft handed to it.
        restored.chat_input.entry.restore_draft('Newer')
        restored.restore_draft(draft)
        self.assertEqual(restored.prompt(), 'Newer')
        restored.discard_button.emit('clicked')
        pump_until(lambda: self.storage.writer.idle)
        self.assertIsNone(self.storage.get_draft(tab.draft.id))
        self.assertEqual((restored.prompt(), restored.references(), restored.form.options()), ('', [], {'output_format': 'png'}))
        self.assertFalse(restored.draft.dirty)

    def test_shutdown_stops_a_run_and_leaves_nothing_pending(self):
        tab = self.make_tab()
        entered, release = self.stalled()
        tab.run_button.emit('clicked')
        pump_until(entered.is_set)
        closed = []
        tab.close_session(lambda: closed.append(True))
        pump_until(lambda: closed)
        release.set()
        run, = self.storage.library.image_runs(tab.strategy.chat_id)
        self.assertEqual(run['status'], 'cancelled')
        self.assertIsNotNone(self.storage.get_chat(tab.strategy.chat_id))


@unittest.skipUnless(Gdk.Display.get_default(), 'GTK tests require a display')
class ImageWindowTests(unittest.TestCase):
    setUp = ImageTests.setUp
    close = ImageTests.close
    run_tab = ImageTests.run_tab
    stalled = ImageTests.stalled

    def make_window(self):
        window = GnollamaWindow(context=self.context)
        self.windows.append(window)
        self.tabs.append(window.tab_view.get_nth_page(0).get_child())
        pump_until(lambda: self.storage.writer.idle and not window.sections['images'].history.pending)
        return window

    def image(self, window):
        window.lookup_action('new_image').activate(None)
        tab = window.sections['images'].tab_view.get_selected_page().get_child()
        self.tabs.append(tab)
        pump_until(lambda: settled(tab))
        return tab

    def test_the_action_opens_a_tab_and_its_runs_appear_under_recent_images(self):
        window = self.make_window()
        tab = self.image(window)
        self.assertIsInstance(tab, ImageTab)
        page = window.sections['images'].tab_view.get_page(tab)
        self.assertEqual((page.get_title(), page.get_icon().get_names()[0]), ('New Image', 'gnollama-image-symbolic'))
        self.assertEqual(window.recent_images_section.get_title(), 'Recent images')
        tab.chat_input.entry.restore_draft('A lighthouse at dusk')
        entered, release = self.stalled()
        tab.run_button.emit('clicked')
        pump_until(entered.is_set)
        self.assertTrue(page.get_loading())
        self.assertTrue(window.is_model_busy(self.host, 'painter'))
        release.set()
        self.run_tab(tab)
        self.assertFalse(page.get_loading())
        session = tab.strategy.chat_id
        pump_until(lambda: session in window.chat_rows and not window.sections['images'].history.pending)
        item = window.chat_rows[session]
        self.assertEqual((item.get_title(), item.get_icon_name(), item.get_section()),
                         ('A lighthouse at dusk', 'gnollama-image-symbolic', window.recent_images_section))
        theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
        icon = theme.lookup_icon(item.get_icon_name(), None, 16, 1, Gtk.TextDirection.LTR, Gtk.IconLookupFlags.FORCE_SYMBOLIC)
        self.assertEqual(icon.get_file().get_uri(),
                         'resource:///io/github/jackrabbithanna/Gnollama/icons/scalable/actions/gnollama-image-symbolic.svg')
        window.close_tab(tab)
        pump_until(lambda: tab not in window.tabs())
        window._open_history_item(item)
        pump_until(lambda: any(isinstance(t, ImageTab) for t in window.tabs()))
        again = next(t for t in window.tabs() if isinstance(t, ImageTab))
        self.tabs.append(again)
        self.assertEqual((again.strategy.chat_id, len(again.runs)), (session, 1))
        self.assertIs(window.open_chat_tab(dict(id=session, kind='image')), again)

    def test_clearing_history_closes_every_tab_with_a_session(self):
        window = self.make_window()
        saved = self.image(window)
        saved.chat_input.entry.restore_draft('A lighthouse at dusk')
        self.assertTrue(self.run_tab(saved))
        fresh = self.image(window)
        window.clear_history()
        pump_until(lambda: saved not in window.tabs() and self.storage.writer.idle
                   and not self.storage.pending_callbacks)
        # A tab with nothing saved stays, and a new chat tab is opened.
        self.assertIn(fresh, window.tabs())
        self.tabs.extend(tab for tab in window.tabs() if tab not in self.tabs)
        # Only the empty chat the new tab starts remains.
        self.assertEqual([chat['kind'] for chat in self.storage.get_all_chats()], ['chat'])

    def test_a_search_hit_opens_the_session_at_its_run(self):
        window = self.make_window()
        tab = self.image(window)
        tab.chat_input.entry.restore_draft('A lighthouse\nguarded by an aardvark')
        runs = self.run_tab(tab)
        window.close_tab(tab)
        pump_until(lambda: tab not in window.tabs())
        window.sections['images'].history.search.set_text('aardvark')
        session = tab.strategy.chat_id
        pump_until(lambda: not window.sections['images'].history.pending and getattr(window.chat_rows.get(session), 'match_uid', None))
        item = window.chat_rows[session]
        self.assertEqual((item.match_uid, item.get_section()), (runs[0]['id'], window.recent_images_section))
        with patch.object(ImageTab, 'show_message') as show:
            window._open_history_item(item)
            pump_until(lambda: show.called)
        show.assert_called_once_with(runs[0]['id'])
        self.tabs.append(window.sections['images'].tab_view.get_selected_page().get_child())

    def test_a_closed_draft_is_listed_reopened_and_discarded_from_the_sidebar(self):
        window = self.make_window()
        tab = self.image(window)
        tab.chat_input.entry.restore_draft('An unsent lighthouse')
        draft_id = tab.draft.id
        window.close_tab(tab)
        pump_until(lambda: tab not in window.tabs() and draft_id in window.draft_rows and not window.sections['images'].history.pending)
        item = window.draft_rows[draft_id]
        self.assertEqual((item.get_title(), item.get_section()), ('An unsent lighthouse', window.sections['images'].history.sections['drafts']))
        window.open_draft(draft_id)
        pump_until(lambda: any(t.draft.id == draft_id for t in window.tabs()))
        again = next(t for t in window.tabs() if t.draft.id == draft_id)
        self.tabs.append(again)
        self.assertIsInstance(again, ImageTab)
        self.assertEqual(again.prompt(), 'An unsent lighthouse')
        window.discard_draft(draft_id)
        pump_until(lambda: self.storage.writer.idle and draft_id not in window.draft_rows)
        self.assertIsNone(self.storage.get_draft(draft_id))
        self.assertEqual(again.prompt(), '')

    def test_the_shortcut_is_registered_and_listed(self):
        root = Path(__file__).resolve().parents[1]
        main = (root / 'src/main.py').read_text()
        self.assertIn("set_accels_for_action('win.new_image', ['<control><shift>g'])", main)
        self.assertEqual(main.lower().count('<control><shift>g'), 1)
        self.assertIn('win.new_image', (root / 'src/shortcuts-dialog.ui').read_text())
        self.assertIn('win.new_image', (root / 'src/window.ui').read_text())


if __name__ == '__main__':
    unittest.main()
