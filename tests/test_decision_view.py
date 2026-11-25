"""The Decision tab: the question form, gating, images, runs and their cards, drafts, and reopening."""
import base64
import copy
import json
import tempfile
import threading
import unittest
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from gi.repository import Gdk, GLib, Gtk

from src.app_context import AppContext
from src.decision import box_rect, parse_questions, questions_object
from src.providers import OllamaProvider, ProviderError, RequestCancelled
from src.providers.registry import PROVIDERS
from src.records import DecisionSettings, has_saved_work
from src.widgets import decision_images
from src.widgets.decision_answers import BoxPicture, RunView
from src.widgets.decision_form import DEFAULT, QuestionForm
from src.widgets.decision_view import DecisionTab
from src.window import TureenWindow
from provider_fixture import LIMITED, limited_host, provider_patch
from test_openai import GIF, JPEG, WEBP
from test_ui import pump_until

FIXTURES = Path(__file__).parent / 'fixtures'
ANSWER = json.loads((FIXTURES / 'systemone_ollama.json').read_text())
BOXED = json.loads((FIXTURES / 'systemone_openrouter.json').read_text())
MODELS = [{'name': 'seeing', 'capabilities': ['decision', 'vision']}, {'name': 'blind', 'capabilities': ['decision']}]
QUESTIONS = {
    'billing': {'type': 'noul', 'instructions': 'Is this about billing?'},
    'tone': {'type': 'choice', 'instructions': 'What is the tone?',
             'criteria': {'angry': 'An upset message', 'calm': None, 'excited': 'An eager message'}},
    'urgency': {'type': 'score', 'criteria': ['Can wait', 'Needs attention this week', 'Needs attention today']}}
# Every type, with values that are not text and so cannot be typed into the form.
EVERYTHING = {
    'plain': {'type': 'noul'},
    'told': {'type': 'noul', 'instructions': {'task': 'Find advertising.'},
             'criteria': {'true': 'Advertising', 'false': ['A conversation', 2]}},
    'bare': {'type': 'noul', 'criteria': {}},
    'tone': {'type': 'choice', 'instructions': ['Pick', 'one'],
             'criteria': {'angry': 'Upset', 'calm': None, 'odd': {'any': [1, None]}, 'empty': ''}},
    'urgency': {'type': 'score', 'instructions': 'How urgent?', 'criteria': ['Can wait', {'level': 'soon'}, ['now']]},
    'where': {'type': 'box', 'instructions': 'Where is the logo?'}}


HINT = 'Some models need images between 256 and 1568 pixels on each side.'
WHERE = {'where': {'type': 'box', 'instructions': 'Where is the logo?'}}
# A host that answers box questions and takes images only as PNG or JPEG, as the gateways do.
GATEWAY = replace(OllamaProvider.capabilities, decision_boxes=True, decision_image_formats=('png', 'jpeg'))


def texture(width, height):
    return Gdk.MemoryTexture.new(width, height, Gdk.MemoryFormat.R8G8B8A8,
                                 GLib.Bytes.new(bytes((200, 60, 20, 255)) * (width * height)), width * 4)


def png(width, height):
    """A real image of this size, as base64."""
    return base64.b64encode(texture(width, height).save_to_png_bytes().get_data()).decode('ascii')


def size_of(image):
    read = Gdk.Texture.new_from_bytes(GLib.Bytes.new(base64.b64decode(image)))
    return read.get_width(), read.get_height()


def settled(tab):
    return tab.picker.input.get_selected_model() is not None


@unittest.skipUnless(Gdk.Display.get_default(), 'GTK tests require a display')
class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.context = AppContext(self.temp.name)
        self.storage = self.context.storage
        self.host = self.storage.get_all_hosts()[0]
        self.stack = ExitStack()
        self.listed = self.stack.enter_context(provider_patch('list_decision_models', return_value=MODELS))
        self.stack.enter_context(provider_patch('list_models', return_value=['test']))
        self.stack.enter_context(provider_patch('show_model', return_value={'capabilities': ['completion']}))
        self.decide = self.stack.enter_context(provider_patch('decide', return_value=ANSWER))
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

    def make_tab(self, questions=QUESTIONS, state='The invoice was charged twice.', **kwargs):
        tab = DecisionTab(self.context, **kwargs)
        self.tabs.append(tab)
        pump_until(lambda: settled(tab))
        if not kwargs:
            tab.form.load(questions)
            tab.chat_input.entry.restore_draft(state)
        return tab

    def run_tab(self, tab):
        tab.run_button.emit('clicked')
        pump_until(lambda: tab.request is None and self.storage.writer.idle and not self.storage.pending_callbacks)
        return self.storage.library.decision_runs(tab.strategy.chat_id)

    def saved(self, tab):
        pump_until(lambda: self.storage.writer.idle and not self.storage.pending_callbacks)
        return self.storage.conversation_page(tab.strategy.chat_id)

    def stalled(self):
        """A `decide` that waits until it is released or cancelled."""
        entered, release = threading.Event(), threading.Event()
        def decide(provider, model, state, questions, images=None, cancellable=None, **kwargs):
            entered.set()
            while not release.wait(0.01):
                if cancellable.is_cancelled():
                    raise RequestCancelled()
            return ANSWER
        self.decide.side_effect = decide
        return entered, release

    # The form.

    def test_the_form_round_trips_every_type_and_keeps_values_that_are_not_text(self):
        form = QuestionForm()
        form.load(EVERYTHING)
        self.assertEqual(form.error, '')
        self.assertEqual(form.value(), EVERYTHING)
        self.assertEqual(list(form.value()), list(EVERYTHING))
        self.assertEqual(questions_object(form.questions()), EVERYTHING)
        self.assertEqual(form.questions(), parse_questions(EVERYTHING))
        told, tone, urgency = form.rows[1], form.rows[3], form.rows[4]
        self.assertEqual(told.instructions.get_text(), '{"task":"Find advertising."}')
        self.assertEqual(told.no.get_text(), '["A conversation",2]')
        self.assertEqual(tone.choice.rows[2].value.get_text(), '{"any":[1,null]}')
        # An edited value is the text typed; one cleared is left out.
        told.instructions.set_text('Is this advertising?')
        told.no.set_text('')
        tone.choice.rows[2].value.set_text('Anything else')
        urgency.score.rows[1].value.set_text('Soon')
        edited = copy.deepcopy(EVERYTHING)
        edited['told'].update(instructions='Is this advertising?', criteria={'true': 'Advertising'})
        edited['tone']['criteria']['odd'] = 'Anything else'
        edited['urgency']['criteria'][1] = 'Soon'
        self.assertEqual(form.value(), edited)
        form.load({})
        self.assertEqual(form.value(), DEFAULT)
        self.assertEqual(form.draft(), {})

    def test_questions_are_added_duplicated_reordered_and_removed_under_unique_names(self):
        changes = []
        form = QuestionForm(lambda: changes.append(1))
        self.assertEqual(list(form.value()), ['question_1'])
        self.assertFalse(form.rows[0].remove_button.get_sensitive())
        form.add_button.emit('clicked')
        form.add_button.emit('clicked')
        self.assertEqual(list(form.value()), ['question_1', 'question_2', 'question_3'])
        form.rows[0].name_entry.set_text('tone')
        form.rows[0].type.set_selected(1)
        row = form.rows[0]
        self.assertEqual(row.kind, 'choice')
        self.assertTrue(row.choice.get_visible() and not row.noul.get_visible() and not row.score.get_visible())
        # The first option is there to fill in, and blocks until it is.
        self.assertEqual(len(row.choice.rows), 1)
        self.assertIn('tone', form.error)
        self.assertTrue(form.error_label.get_visible())
        row.choice.rows[0].key.set_text('calm')
        self.assertEqual(form.error, '')
        row.choice.add('angry', 'An upset message')
        form.duplicate(row)
        self.assertEqual(list(form.value()), ['tone', 'tone_2', 'question_2', 'question_3'])
        self.assertEqual(form.value()['tone_2'], {'type': 'choice', 'criteria': {'calm': None, 'angry': 'An upset message'}})
        form.duplicate(form.rows[1])
        self.assertEqual(list(form.value())[2], 'tone_3')
        form.move(form.rows[0], 1)
        form.move(form.rows[0], -1)
        form.rows[4].down.emit('clicked')
        self.assertEqual(list(form.value()), ['tone_2', 'tone', 'tone_3', 'question_2', 'question_3'])
        self.assertFalse(form.rows[0].up.get_sensitive())
        self.assertFalse(form.rows[4].down.get_sensitive())
        form.rows[3].up.emit('clicked')
        form.rows[2].remove_button.emit('clicked')
        self.assertEqual(list(form.value()), ['tone_2', 'tone', 'tone_3', 'question_3'])
        self.assertTrue(changes)
        self.assertEqual(form.error, '')
        self.assertEqual(questions_object(form.questions()), form.value())

    def test_score_levels_are_ordered_and_both_lists_stop_at_their_limits(self):
        form = QuestionForm()
        form.load({'urgency': {'type': 'score', 'criteria': ['low', 'middle', 'high']},
                   'tone': {'type': 'choice', 'criteria': {str(n): None for n in range(254)}}})
        levels, options = form.rows[0].score, form.rows[1].choice
        self.assertEqual([row.number.get_text() for row in levels.rows], ['0', '1', '2'])
        levels.rows[2].up.emit('clicked')
        levels.rows[0].down.emit('clicked')
        self.assertEqual(form.value()['urgency']['criteria'], ['high', 'low', 'middle'])
        self.assertFalse(levels.rows[0].up.get_sensitive())
        self.assertFalse(levels.rows[2].down.get_sensitive())
        levels.remove_row(levels.rows[1])
        self.assertEqual(form.value()['urgency']['criteria'], ['high', 'middle'])
        for index in range(8):
            levels.add(value=f'level {index}')
        self.assertFalse(levels.add_button.get_sensitive())
        levels.add(value='one too many')
        self.assertEqual(len(form.value()['urgency']['criteria']), 10)
        self.assertTrue(options.add_button.get_sensitive())
        options.add('last')
        self.assertFalse(options.add_button.get_sensitive())
        options.add('over')
        self.assertEqual(len(form.value()['tone']['criteria']), 255)
        self.assertEqual(form.error, '')

    def test_what_cannot_be_asked_is_named_in_the_form(self):
        form = QuestionForm()
        form.load({'first': {'type': 'noul'}, 'second': {'type': 'score', 'criteria': ['low', 'high']}})
        form.rows[1].name_entry.set_text('first')
        self.assertIn('unique', form.error)
        form.rows[1].name_entry.set_text(' ')
        self.assertIn('unique', form.error)
        form.rows[1].name_entry.set_text('second')
        self.assertEqual(form.error, '')
        form.rows[1].score.rows[0].value.set_text('')
        self.assertIn('second', form.error)
        form.rows[1].score.rows[0].value.set_text('low')
        form.rows[1].type.set_selected(1)
        options = form.rows[1].choice
        options.rows[0].key.set_text('same')
        options.add('same')
        self.assertIn('second', form.error)
        options.remove_row(options.rows[1])
        self.assertEqual(form.error, '')
        options.remove_row(options.rows[0])
        self.assertIn('between 1 and 255', form.error)
        # Its levels are still there when the type goes back.
        form.rows[1].type.set_selected(2)
        self.assertEqual(form.value()['second'], {'type': 'score', 'criteria': ['low', 'high']})

    def test_a_box_question_keeps_its_type_which_no_other_question_is_offered(self):
        form = QuestionForm()
        form.load({'where': {'type': 'box'}, 'plain': {'type': 'noul'}})
        box, plain = form.rows
        def labels(row):
            return [row.type.get_model().get_string(i) for i in range(row.type.get_model().get_n_items())]
        self.assertEqual(labels(plain), ['Yes/No', 'Choice', 'Score'])
        self.assertEqual(labels(box), ['Yes/No', 'Choice', 'Score', 'Box'])
        self.assertEqual(box.kind, 'box')
        self.assertFalse(box.noul.get_visible() or box.choice.get_visible() or box.score.get_visible())
        self.assertEqual(form.value(), {'where': {'type': 'box'}, 'plain': {'type': 'noul'}})
        form.add()
        self.assertEqual(labels(form.rows[2]), ['Yes/No', 'Choice', 'Score'])

    def test_a_draft_still_being_written_loads_as_far_as_it_goes(self):
        form = QuestionForm()
        form.load({'half': {'type': 'choice'}, 'odd': {'type': 'unknown', 'criteria': 7}, 'lost': 'text',
                   'levels': {'type': 'score', 'criteria': {'not': 'a list'}}})
        self.assertEqual(list(form.value()), ['half', 'odd', 'levels'])
        self.assertEqual(form.value()['half'], {'type': 'choice', 'criteria': {}})
        self.assertEqual(form.value()['odd'], {'type': 'noul'})
        self.assertTrue(form.error)

    def test_questions_are_edited_imported_and_exported_as_json(self):
        tab = self.make_tab()
        window = Gtk.Window(child=tab)
        self.windows.append(window)
        tab.form.json_button.emit('clicked')
        dialog = tab.form.dialog
        self.assertEqual(json.loads(self.text(dialog.editor)), QUESTIONS)
        self.assertTrue(dialog.import_title)
        dialog.editor.get_buffer().set_text('{"a": {"type": "choice"}}')
        dialog.apply_button.emit('clicked')
        self.assertIn('between 1 and 255', dialog.error_label.get_text())
        self.assertTrue(dialog.error_label.get_visible())
        self.assertEqual(tab.form.value(), QUESTIONS)
        with patch.object(Gtk.FileDialog, 'save') as save:
            dialog.export_text('Export Questions', 'questions.json')
            save.assert_not_called()
        dialog.editor.get_buffer().set_text(json.dumps(EVERYTHING))
        with patch.object(Gtk.FileDialog, 'save') as save:
            dialog.export_text('Export Questions', 'questions.json')
            save.assert_called_once()
        revision = tab.draft.revision
        dialog.apply_button.emit('clicked')
        pump_until(lambda: tab.form.dialog is None)
        self.assertEqual(tab.form.value(), EVERYTHING)
        self.assertGreater(tab.draft.revision, revision)
        # A box question came with the JSON, and this model cannot answer one.
        self.assertEqual(tab.notice.get_text(), 'This model cannot answer box questions.')
        self.assertFalse(tab.run_button.get_sensitive())

    @staticmethod
    def text(view):
        buffer = view.get_buffer()
        return buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)

    # Gating.

    def test_models_are_the_hosts_decision_models_sorted_and_searchable(self):
        tab = self.make_tab()
        dropdown = tab.picker.input.model_dropdown
        names = [dropdown.get_model().get_string(i) for i in range(dropdown.get_model().get_n_items())]
        self.assertEqual(names, ['blind', 'seeing'])
        self.assertTrue(dropdown.get_enable_search())
        self.assertEqual(tab.picker.input.get_selected_model(), 'blind')
        self.assertEqual(tab.picker.input.model_summary.get_text(), 'No images')
        self.assertFalse(tab.picker.decision_capabilities().decision_images)
        tab.picker.input.select_model('seeing')
        self.assertEqual(tab.picker.input.model_summary.get_text(), 'Images')
        self.assertTrue(tab.picker.decision_capabilities().decision_images)
        self.assertTrue(tab.run_button.get_sensitive())
        self.assertFalse(tab.chat_input.send_button.get_visible())
        self.assertEqual(tab.images(), [])

    # Images.

    def attach(self, tab, *images):
        tab.chat_input.restore_images(list(tab.images()) + list(images))

    def test_images_are_attached_where_the_model_takes_them_and_say_why_where_not(self):
        tab = self.make_tab()
        attach = tab.chat_input.attach_button
        self.assertTrue(attach.get_parent().get_visible())
        self.assertEqual(tab.picker.input.get_selected_model(), 'blind')
        self.assertFalse(attach.get_sensitive())
        self.assertEqual(attach.get_tooltip_text(), 'This model does not accept images.')
        # A pasted image is refused too.
        tab.chat_input.attach_pasted([texture(4, 4)])
        self.assertEqual((tab.chat_input.pending_imports, tab.images()), (0, []))
        tab.picker.input.select_model('seeing')
        self.assertTrue(attach.get_sensitive())
        self.assertEqual(attach.get_tooltip_text(), 'Attach Image')
        self.assertFalse(tab.draft.dirty and tab.images())
        tab.chat_input.attach_pasted([texture(4, 4)])
        # Nothing runs while an image is still being read.
        tab.run()
        self.assertIsNone(tab.request)
        pump_until(lambda: not tab.chat_input.pending_imports)
        image, = tab.images()
        self.assertEqual(size_of(image), (4, 4))
        self.assertTrue(tab.draft.dirty)
        self.assertTrue(tab.run_button.get_sensitive())
        self.assertTrue(attach.get_sensitive())
        # The controls belong to the state however it is written.
        tab.json_toggle.set_active(True)
        self.assertFalse(tab.chat_input.get_visible())
        self.assertTrue(tab.attachments.get_visible() and attach.get_parent().get_visible())
        self.assertIs(attach.get_parent().get_parent(), tab.attachments)
        self.assertEqual(tab.images(), [image])
        tab.text_toggle.set_active(True)
        # A model that takes none leaves the image in place and holds the run back.
        tab.picker.input.select_model('blind')
        self.assertEqual(tab.images(), [image])
        self.assertTrue(tab.chat_input.image_preview_scrolled.get_visible())
        self.assertFalse(attach.get_sensitive())
        self.assertEqual(tab.notice.get_text(), 'This model does not accept images.')
        self.assertTrue(tab.notice.get_visible())
        self.assertFalse(tab.run_button.get_sensitive())
        tab.run()
        pump_until(lambda: self.context.services.idle)
        self.decide.assert_not_called()
        tab.chat_input.clear_image_button.emit('clicked')
        self.assertEqual(tab.images(), [])
        self.assertTrue(tab.run_button.get_sensitive())
        self.assertFalse(tab.notice.get_visible())

    def test_images_are_sent_stored_with_the_run_and_shown_when_it_is_reopened(self):
        tab = self.make_tab()
        tab.picker.input.select_model('seeing')
        first, second = png(40, 20), png(10, 30)
        self.attach(tab, first, second)
        runs = self.run_tab(tab)
        self.assertEqual(self.decide.call_args.kwargs['images'], [first, second])
        self.assertEqual(runs[0]['images'], [first, second])
        # They stay for the next run, and are no draft until something changes.
        self.assertEqual(tab.images(), [first, second])
        self.assertFalse(tab.draft.dirty)
        view = tab.runs[runs[0]['id']]
        shown = self.find(view.images, Gtk.Picture)
        pump_until(lambda: all(picture.get_visible() for picture in shown))
        self.assertEqual([(p.get_paintable().get_intrinsic_width(), p.get_paintable().get_intrinsic_height()) for p in shown],
                         [(40, 20), (10, 30)])
        self.assertEqual({picture.get_alternative_text() for picture in shown}, {'Image sent with this run'})
        # Images alone are a state.
        tab.chat_input.entry.restore_draft('')
        self.assertTrue(tab.run_button.get_sensitive())
        tab.chat_input.restore_images([second])
        runs = self.run_tab(tab)
        self.assertEqual([(run['state'], run['images']) for run in runs], [
            ('The invoice was charged twice.', [first, second]), ('', [second])])
        page = self.saved(tab)
        tab.draft.close(discard=True)
        again = self.make_tab(saved=page)
        pump_until(lambda: again.picker.input.get_selected_model() == 'seeing')
        self.assertEqual(again.images(), [second])
        self.assertFalse(again.draft.dirty)
        counts = [len(self.find(again.runs[run['id']].images, Gtk.Picture)) for run in runs]
        self.assertEqual(counts, [2, 1])
        self.assertTrue(all(view.images.get_visible() for view in again.runs.values()))
        # A run without images shows no row of them.
        again.chat_input.clear_image_button.emit('clicked')
        again.chat_input.entry.restore_draft('Text only')
        runs = self.run_tab(again)
        self.assertFalse(again.runs[runs[2]['id']].images.get_visible())
        self.assertIsNone(self.decide.call_args.kwargs['images'])

    def test_a_large_image_is_shown_small(self):
        seen = []
        decision_images.load(png(2400, 30), seen.append)
        self.assertEqual((seen[0].width, seen[0].height), (2400, 30))
        self.assertEqual((seen[0].texture.get_width(), seen[0].texture.get_height()), (960, 12))
        small = decision_images.Bounded(seen[0].texture, 80)
        self.assertEqual((small.get_intrinsic_width(), small.get_intrinsic_height()), (80, 1))
        self.assertAlmostEqual(small.get_intrinsic_aspect_ratio(), 80)
        # One that cannot be read is no picture, and is asked of once by all who wait for it.
        waiting = []
        work = []
        decision_images.load('dW5yZWFkYWJsZQ==', waiting.append, work.append)
        decision_images.load('dW5yZWFkYWJsZQ==', waiting.append, work.append)
        self.assertEqual((len(work), waiting), (1, []))
        work[0]()
        pump_until(lambda: len(waiting) == 2)
        self.assertEqual(waiting, [None, None])
        picture = decision_images.thumbnail('dW5yZWFkYWJsZQ==', 'Image')
        self.assertFalse(picture.get_visible())

    def test_images_are_part_of_a_draft(self):
        tab = self.make_tab({}, '')
        tab.picker.input.select_model('seeing')
        tab.draft.consumed()
        image = png(8, 8)
        self.attach(tab, image)
        self.assertTrue(tab.draft.dirty)
        tab.draft.flush()
        pump_until(lambda: self.storage.writer.idle)
        draft = self.storage.get_draft(tab.draft.id)
        self.assertEqual((draft['text'], draft['images']), ('', [image]))
        self.assertTrue(has_saved_work(draft=draft))
        restored = self.make_tab(draft=draft)
        self.assertEqual(restored.images(), [image])
        self.assertTrue(restored.chat_input.image_preview_scrolled.get_visible())
        self.assertFalse(restored.draft.dirty)
        restored.discard_button.emit('clicked')
        pump_until(lambda: self.storage.writer.idle)
        self.assertEqual(restored.images(), [])
        self.assertIsNone(self.storage.get_draft(tab.draft.id))
        # After a run, discarding returns to the images that run was sent.
        ran = self.make_tab()
        ran.picker.input.select_model('seeing')
        self.attach(ran, image)
        self.run_tab(ran)
        self.attach(ran, png(9, 9))
        self.assertTrue(ran.draft.dirty)
        ran.discard_button.emit('clicked')
        self.assertEqual(ran.images(), [image])

    def test_other_formats_are_made_png_for_a_host_that_takes_only_png_and_jpeg(self):
        real = png(6, 4)
        # Neither of these is an image this build can read, so what reading them gives is supplied.
        read = {WEBP: texture(3, 5), GIF: texture(7, 2)}
        def texture_of(image):
            return read[image] if image in read else Gdk.Texture.new_from_bytes(GLib.Bytes.new(base64.b64decode(image)))
        with patch.object(decision_images, 'texture_of', texture_of):
            made = decision_images.in_formats([real, JPEG, WEBP, GIF], ('png', 'jpeg'))
            self.assertIs(made[0], real)
            self.assertIs(made[1], JPEG)
            self.assertEqual([size_of(image) for image in made[2:]], [(3, 5), (7, 2)])
            self.assertTrue(all(base64.b64decode(image).startswith(b'\x89PNG') for image in made[2:]))
            tab = self.make_tab()
            tab.picker.input.select_model('seeing')
            self.attach(tab, real, JPEG, WEBP, GIF)
            # A host that reads anything is sent them as they are.
            runs = self.run_tab(tab)
            self.assertEqual(self.decide.call_args.kwargs['images'], [real, JPEG, WEBP, GIF])
            self.assertEqual(runs[0]['images'], [real, JPEG, WEBP, GIF])
            with patch.object(OllamaProvider, 'capabilities', GATEWAY):
                runs = self.run_tab(tab)
            self.assertEqual(self.decide.call_args.kwargs['images'], made)
            self.assertEqual((runs[1]['images'], runs[1]['status']), (made, 'done'))
            # The editor keeps what was attached.
            self.assertEqual(tab.images(), [real, JPEG, WEBP, GIF])
        # One that cannot be read is left for the server to refuse.
        self.assertEqual(decision_images.in_formats([GIF, 'not base64!'], ('png', 'jpeg')), [GIF, 'not base64!'])

    # Box questions.

    def test_the_box_type_is_offered_where_the_model_answers_it(self):
        def labels(row):
            return [row.type.get_model().get_string(i) for i in range(row.type.get_model().get_n_items())]
        tab = self.make_tab()
        self.assertFalse(tab.form.boxes)
        self.assertEqual(labels(tab.form.rows[0]), ['Yes/No', 'Choice', 'Score'])
        with patch.object(OllamaProvider, 'capabilities', GATEWAY):
            # A model that sees no images answers none.
            tab.picker.input.select_model('seeing')
            tab.picker.input.select_model('blind')
            self.assertFalse(tab.form.boxes)
            tab.picker.input.select_model('seeing')
            self.assertTrue(tab.form.boxes)
            self.assertEqual([row.kind for row in tab.form.rows], ['noul', 'choice', 'score'])
            self.assertEqual(tab.form.value(), QUESTIONS)
            for row in tab.form.rows:
                self.assertEqual(labels(row), ['Yes/No', 'Choice', 'Score', 'Box'])
            tab.form.add_button.emit('clicked')
            row = tab.form.rows[3]
            self.assertEqual(labels(row), ['Yes/No', 'Choice', 'Score', 'Box'])
            row.name_entry.set_text('where')
            row.type.set_selected(3)
            row.instructions.set_text('Where is the logo?')
            # Instructions, and no criteria.
            self.assertEqual(row.kind, 'box')
            self.assertTrue(row.instructions.get_visible())
            self.assertFalse(row.noul.get_visible() or row.choice.get_visible() or row.score.get_visible())
            self.assertEqual(tab.form.value()['where'], WHERE['where'])
            self.assertEqual(tab.form.error, '')
            # It needs exactly one image.
            self.assertEqual(tab.notice.get_text(), 'A box question needs exactly one image.')
            self.assertFalse(tab.run_button.get_sensitive())
            self.attach(tab, png(320, 320))
            self.assertEqual(tab.notice.get_text(), '')
            self.assertTrue(tab.run_button.get_sensitive())
            self.attach(tab, png(320, 320))
            self.assertEqual(tab.notice.get_text(), 'A box question needs exactly one image.')
            self.assertFalse(tab.run_button.get_sensitive())
            tab.run()
            pump_until(lambda: self.context.services.idle)
            self.decide.assert_not_called()
            # A model that answers none keeps the question that is one, offers no other, and says why it cannot run.
            tab.picker.input.select_model('blind')
            self.assertEqual(row.kind, 'box')
            self.assertEqual(labels(row), ['Yes/No', 'Choice', 'Score', 'Box'])
            self.assertEqual(labels(tab.form.rows[0]), ['Yes/No', 'Choice', 'Score'])
            self.assertEqual(tab.form.value()['where'], WHERE['where'])
            self.assertFalse(tab.run_button.get_sensitive())
            tab.chat_input.clear_image_button.emit('clicked')
            self.assertEqual(tab.notice.get_text(), 'This model cannot answer box questions.')

    def test_a_box_question_about_an_image_of_an_unusual_size_is_advised_on_and_not_held_back(self):
        with patch.object(OllamaProvider, 'capabilities', GATEWAY):
            tab = self.make_tab(dict(QUESTIONS, **WHERE))
            tab.picker.input.select_model('seeing')
            self.assertFalse(tab.hint.get_visible())
            for size, advised in (((200, 300), True), ((320, 320), False), ((300, 1600), True), ((256, 1568), False)):
                with self.subTest(size=size):
                    tab.chat_input.restore_images([png(*size)])
                    pump_until(lambda: all(tab._sizes.values()))
                    self.assertEqual((tab.hint.get_visible(), tab.hint.get_text()), (advised, HINT if advised else ''))
                    self.assertTrue(tab.run_button.get_sensitive())
                    self.assertEqual(tab.notice.get_text(), '')
            small = png(200, 300)
            tab.chat_input.restore_images([small])
            pump_until(lambda: tab.hint.get_visible())
            # Only a box question is advised on.
            tab.form.rows[3].remove_button.emit('clicked')
            self.assertFalse(tab.hint.get_visible())
            tab.form.load(dict(QUESTIONS, **WHERE))
            tab.form.changed()
            pump_until(lambda: tab.hint.get_visible())
            runs = self.run_tab(tab)
            self.assertEqual((runs[0]['status'], runs[0]['images']), ('done', [small]))
            tab.chat_input.clear_image_button.emit('clicked')
            self.assertFalse(tab.hint.get_visible())

    def boxed_run(self, images):
        """A stored run of a box question beside the others, answered as OpenRouter's Jev answers."""
        questions = dict(QUESTIONS, **WHERE)
        settings = DecisionSettings.from_stored(dict(questions=questions, target=dict(
            host_id=self.host['id'], host=self.host['hostname'], model='seeing', host_name='localhost')))
        library = self.storage.library
        library.create_decision_run(dict(settings.to_stored(), id='boxed', session_id='session', new_session=True,
                                         state='See the image.', images=images))
        library.finish_decision_run('boxed', 'done', BOXED)
        return self.storage.conversation_page('session')

    def test_a_box_answer_is_drawn_over_the_image_of_its_run(self):
        box = BOXED['answers']['where']['box']
        tab = self.make_tab(saved=self.boxed_run([png(400, 200)]))
        view = tab.runs['boxed']
        self.assertEqual([answer.type for answer in view.answers()], ['noul', 'choice', 'score', 'box'])
        picture, = self.find(view, BoxPicture)
        # Read on a worker, and shown once it is.
        pump_until(lambda: picture.size)
        self.assertTrue(picture.get_visible())
        self.assertEqual((picture.size, picture.box), ((400, 200), box))
        # It asks for the image's own size, and for a narrower width the height that keeps its shape.
        self.assertEqual(tuple(picture.measure(Gtk.Orientation.HORIZONTAL, -1))[:2], (120, 400))
        self.assertEqual([tuple(picture.measure(Gtk.Orientation.VERTICAL, width))[:2] for width in (-1, 400, 900, 150)],
                         [(200, 200), (200, 200), (200, 200), (75, 75)])
        large = BoxPicture(box, '')
        large.show_image(decision_images.Seen(texture(96, 48), 1920, 960))
        self.assertEqual(tuple(large.measure(Gtk.Orientation.HORIZONTAL, -1))[:2], (120, 480))
        self.assertEqual(tuple(large.measure(Gtk.Orientation.VERTICAL, -1))[:2], (240, 240))
        # Wherever the image is drawn, the box follows it.
        for area in ((400, 200), (200, 100), (300, 450), (137, 61)):
            self.assertEqual(picture.rect(*area), box_rect(box, 400, 200, *area))
        self.assertEqual([round(value, 6) for value in picture.rect(400, 200)], [68.0, 32.0, 212.0, 92.0])
        described = 'Box: top 0.16, bottom 0.62, left 0.17, right 0.70'
        self.assertEqual(picture.picture.get_alternative_text(), described)
        self.assertIn(described, self.labels(view))
        self.assertIn('Confidence: 76%', self.labels(view))
        # A pale halo under a narrower line in the accent colour, so one of them shows on any image.
        (halo, wide), (accent, narrow) = picture.strokes()
        self.assertGreater(wide, narrow)
        self.assertNotEqual(halo[:3], accent[:3])
        import cairo
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 400, 200)
        picture.draw(picture.area, cairo.Context(surface), 400, 200)
        surface.flush()
        def alpha(x, y):
            return surface.get_data()[y * surface.get_stride() + x * 4 + 3]
        self.assertEqual([alpha(68, 78), alpha(280, 78), alpha(174, 32), alpha(174, 124)], [255] * 4)
        self.assertEqual([alpha(174, 78), alpha(20, 20), alpha(380, 180)], [0] * 3)
        # The run's image is its thumbnail too, and what the editor holds.
        self.assertEqual(len(self.find(view.images, Gtk.Picture)), 1)
        self.assertEqual([size_of(image) for image in tab.images()], [(400, 200)])

    def test_a_box_answer_whose_image_cannot_be_read_stands_as_numbers(self):
        tab = self.make_tab(saved=self.boxed_run(['bm90IGFuIGltYWdl']))
        view = tab.runs['boxed']
        picture, = self.find(view, BoxPicture)
        pump_until(lambda: self.context.services.idle)
        pump_until(lambda: True)
        self.assertFalse(picture.get_visible())
        self.assertIsNone(picture.rect(400, 200))
        self.assertEqual(tuple(picture.measure(Gtk.Orientation.VERTICAL, 300))[:2], (0, 0))
        self.assertIn('Box: top 0.16, bottom 0.62, left 0.17, right 0.70', self.labels(view))

    def test_a_host_with_no_decision_models_says_so(self):
        self.listed.return_value = []
        tab = DecisionTab(self.context)
        self.tabs.append(tab)
        notice = tab.picker.input.capability_notice
        pump_until(lambda: notice.get_text().startswith('No decision models'))
        self.assertIsNone(tab.picker.input.get_selected_model())
        tab.chat_input.entry.restore_draft('state')
        self.assertFalse(tab.run_button.get_sensitive())

    def test_a_host_that_cannot_decide_is_listed_and_blocked_with_the_reason(self):
        with patch.dict(PROVIDERS, LIMITED):
            limited = limited_host(self.storage)
            tab = self.make_tab()
            calls = self.listed.call_count
            names = [host['name'] for host in tab.picker.panel.host_list]
            self.assertIn('Limited', names)
            tab.picker.panel.host_dropdown.set_selected(names.index('Limited'))
            notice = tab.picker.input.capability_notice
            self.assertEqual(notice.get_text(), 'This server does not offer decision models.')
            self.assertTrue(notice.get_visible())
            self.assertIsNone(tab.picker.input.get_selected_model())
            self.assertFalse(tab.run_button.get_sensitive())
            tab.run()
            pump_until(lambda: self.context.services.idle)
            self.assertEqual(self.listed.call_count, calls)
            self.decide.assert_not_called()
            self.assertIsNone(tab.strategy.chat_id)
            # The controller refuses it too, whatever the view allows.
            from src.decision_run import prepare
            settings = DecisionSettings.from_stored(dict(questions=QUESTIONS, target=dict(host_id=limited['id'], model='any')))
            with self.assertRaisesRegex(ValueError, 'does not offer decision models'):
                prepare(self.context, limited, settings, 'state')
            tab.picker.panel.host_dropdown.set_selected(names.index(self.host['name']))
            pump_until(lambda: settled(tab))
            self.assertFalse(notice.get_visible())
            self.assertTrue(tab.run_button.get_sensitive())

    def test_more_questions_than_the_host_takes_block_run_with_the_reason(self):
        tab = self.make_tab({f'q{n}': {'type': 'noul'} for n in range(64)})
        self.assertTrue(tab.run_button.get_sensitive())
        self.assertFalse(tab.notice.get_visible())
        tab.form.add_button.emit('clicked')
        self.assertEqual(tab.notice.get_text(), 'This model takes at most 64 questions.')
        self.assertTrue(tab.notice.get_visible())
        self.assertFalse(tab.run_button.get_sensitive())
        tab.run()
        self.decide.assert_not_called()
        tab.form.rows[-1].remove_button.emit('clicked')
        self.assertTrue(tab.run_button.get_sensitive())

    def test_invalid_questions_and_an_empty_state_block_run(self):
        tab = self.make_tab()
        self.assertTrue(tab.run_button.get_sensitive())
        tab.form.rows[1].name_entry.set_text('billing')
        self.assertFalse(tab.run_button.get_sensitive())
        self.assertTrue(tab.form.error_label.get_visible())
        tab.run_button.emit('clicked')
        tab.run()
        tab.form.rows[1].name_entry.set_text('tone')
        self.assertTrue(tab.run_button.get_sensitive())
        tab.chat_input.entry.restore_draft('  \n')
        self.assertFalse(tab.run_button.get_sensitive())
        tab.run()
        pump_until(lambda: self.context.services.idle)
        self.decide.assert_not_called()
        self.assertIsNone(tab.strategy.chat_id)

    # Runs.

    def test_a_run_shows_a_card_for_each_answer_and_is_saved_as_done(self):
        tab = self.make_tab()
        tab.picker.input.select_model('seeing')
        updates = []
        tab.connect('chat-updated', lambda tab, id, title: updates.append((id, title)))
        self.assertEqual(tab.title, 'New Decision')
        runs = self.run_tab(tab)
        provider, model, state, questions = self.decide.call_args.args
        self.assertEqual((model, state, questions), ('seeing', 'The invoice was charged twice.', QUESTIONS))
        self.assertIsNone(self.decide.call_args.kwargs['images'])
        self.assertEqual(provider.url, self.context.connection(self.host).url)
        self.assertEqual([(run['status'], run['result'], run['state'], run['state_mode']) for run in runs],
                         [('done', ANSWER, 'The invoice was charged twice.', 'text')])
        self.assertEqual(runs[0]['questions'], QUESTIONS)
        self.assertEqual((runs[0]['target']['model'], runs[0]['target']['host_id'], runs[0]['target']['host_name']),
                         ('seeing', self.host['id'], self.host['name']))
        self.assertEqual(tab.title, 'The invoice was charged twice.')
        self.assertEqual(updates[-1], (tab.strategy.chat_id, 'The invoice was charged twice.'))
        self.assertEqual(self.storage.get_chat(tab.strategy.chat_id)['title'], tab.title)
        view = tab.runs[runs[0]['id']]
        self.assertEqual([answer.type for answer in view.answers()], ['noul', 'choice', 'score'])
        self.assertIn('Run 1', view.title.get_text())
        self.assertIn('seeing', view.title.get_text())
        self.assertFalse(view.spinner.get_visible())
        self.assertEqual(view.usage.get_text(), 'd3 · Input: 449 tokens · Output: 0 tokens')
        self.assertFalse(view.error.get_visible())
        self.assertTrue(tab.results_title.get_visible())
        shown = self.labels(view)
        for text in ('billing', 'Yes', 'Probability of yes', '99%', 'calm', 'Confidence: 68%', '89%', '1.54 of 2', 'Needs attention today',
                     '2 · Needs attention today', '58%', 'Is this about billing?'):
            self.assertIn(text, shown)
        self.assertEqual(len(self.find(view, Gtk.LevelBar)), 1 + 3 + 3)
        # The editor keeps what was asked, for the next run.
        self.assertEqual(tab.state_text(), 'The invoice was charged twice.')
        self.assertEqual(tab.form.value(), QUESTIONS)
        self.assertTrue(tab.run_button.get_sensitive())
        self.assertEqual(tab.run_button.get_label(), 'Run')
        self.assertIsNone(self.storage.get_draft(tab.draft.id))
        view.copy_button.emit('clicked')
        self.assertEqual(json.loads(view.raw), ANSWER)

    def find(self, widget, kind):
        found = [widget] if isinstance(widget, kind) else []
        child = widget.get_first_child()
        while child:
            found += self.find(child, kind)
            child = child.get_next_sibling()
        return found

    def labels(self, widget):
        return [label.get_text() for label in self.find(widget, Gtk.Label)]

    def test_cost_refusals_and_answers_that_are_missing_or_invalid_have_their_own_cards(self):
        answer = {'model': 'priced', 'usage': {'input_tokens': 12, 'cost': 0.000449},
                  'answers': {'billing': {'type': 'refusal', 'reason': 'unsafe'},
                              'urgency': {'type': 'score', 'score': 99}}}
        self.decide.return_value = answer
        tab = self.make_tab()
        runs = self.run_tab(tab)
        self.assertEqual((runs[0]['status'], runs[0]['result']), ('done', answer))
        view = tab.runs[runs[0]['id']]
        self.assertEqual([answer.type for answer in view.answers()], ['refusal', 'missing', 'invalid'])
        self.assertEqual(view.usage.get_text(), 'priced · Input: 12 tokens · Cost: $0.000449')
        shown = self.labels(view)
        self.assertIn('The model declined to answer this question.', shown)
        self.assertIn('The model returned no answer to this question.', shown)
        self.assertIn('The answer is not valid for this question.', shown)
        raw = self.find(view, Gtk.Expander)
        self.assertEqual(len(raw), 2)
        self.assertFalse(any(expander.get_expanded() for expander in raw))
        self.assertIn('"score": 99', self.text(raw[1].get_child().get_child()))
        self.assertEqual(self.find(view, Gtk.LevelBar), [])

    def test_what_a_host_adds_to_an_answer_is_shown_on_its_card(self):
        answer = copy.deepcopy(ANSWER)
        answer['answers']['billing'].update(confidence=0.91, unknown_probability=0.0312345, abstained=False)
        answer['answers']['tone'].update(abstained=True, note=None, alternatives=['calm', 'angry'])
        self.decide.return_value = answer
        tab = self.make_tab()
        view = tab.runs[self.run_tab(tab)[0]['id']]
        billing, tone, urgency = (self.labels(card) for card in self.find(view.cards, Gtk.Frame)[:3])
        self.assertIn('Confidence: 91%', billing)
        self.assertIn('Unknown probability: 0.03123', billing)
        self.assertIn('Abstained: No', billing)
        self.assertIn('Abstained: Yes', tone)
        self.assertIn('Note: –', tone)
        self.assertFalse(any(label.startswith(('Abstained', 'Unknown')) for label in urgency))
        folded = self.find(view.cards, Gtk.Expander)
        self.assertEqual([expander.get_label() for expander in folded], ['Alternatives'])
        self.assertIn('"angry"', self.text(folded[0].get_child().get_child()))

    def test_a_failed_run_is_saved_with_its_error_and_says_so(self):
        self.decide.side_effect = ProviderError('The model is not loaded.')
        tab = self.make_tab()
        runs = self.run_tab(tab)
        self.assertEqual([(run['status'], run['error'], run['result']) for run in runs],
                         [('failed', 'The model is not loaded.', None)])
        view = tab.runs[runs[0]['id']]
        self.assertIn('Failed', view.title.get_text())
        self.assertEqual(view.error.get_text(), 'The model is not loaded.')
        self.assertTrue(view.error.get_visible())
        self.assertEqual(view.answers(), [])
        self.assertFalse(view.copy_button.get_visible())
        # An answer with no answers in it has failed too.
        self.decide.side_effect = None
        self.decide.return_value = {'model': 'seeing'}
        runs = self.run_tab(tab)
        self.assertEqual((runs[1]['status'], runs[1]['error']), ('failed', 'The response has no answers.'))
        self.assertTrue(tab.run_button.get_sensitive())

    def test_stop_cancels_the_run_and_a_second_cannot_start_meanwhile(self):
        entered, release = self.stalled()
        tab = self.make_tab()
        changes = []
        tab.connect('request-changed', lambda tab: changes.append(tab.request is not None))
        tab.run_button.emit('clicked')
        request = tab.request
        self.assertIsNotNone(request)
        pump_until(entered.is_set)
        # What the window reads to know the model is in use.
        self.assertEqual((request.connection.url, request.settings.target.model),
                         (self.context.connection(self.host).url, 'blind'))
        self.assertTrue(self.context.services.models.busy(self.host, 'blind'))
        window = type('Window', (), dict(context=self.context, tabs=lambda self: [tab]))()
        self.assertTrue(TureenWindow.is_model_busy(window, self.host, 'blind'))
        self.assertEqual(tab.run_button.get_label(), 'Stop')
        self.assertTrue(tab.run_button.get_sensitive())
        self.assertFalse(tab.form.get_sensitive() or tab.picker.get_sensitive() or tab.chat_input.get_sensitive())
        view = next(iter(tab.runs.values()))
        self.assertIn('Running…', view.title.get_text())
        self.assertTrue(view.spinner.get_visible())
        tab.run()
        self.assertIs(tab.request, request)
        tab.run_button.emit('clicked')
        pump_until(lambda: tab.request is None)
        self.assertEqual(self.decide.call_count, 1)
        runs = self.storage.library.decision_runs(tab.strategy.chat_id)
        self.assertEqual([(run['status'], run['error'], run['result']) for run in runs], [('cancelled', None, None)])
        self.assertIn('Cancelled', view.title.get_text())
        self.assertFalse(view.error.get_visible())
        self.assertFalse(self.context.services.models.busy(self.host, 'blind'))
        self.assertFalse(TureenWindow.is_model_busy(window, self.host, 'blind'))
        self.assertEqual((changes[0], changes[-1]), (True, False))
        self.assertTrue(tab.form.get_sensitive())
        self.assertEqual(tab.run_button.get_label(), 'Run')

    def test_an_answer_that_arrives_after_stop_is_not_kept(self):
        entered, release = threading.Event(), threading.Event()
        def decide(*args, **kwargs):
            entered.set()
            release.wait(4)
            return ANSWER
        self.decide.side_effect = decide
        tab = self.make_tab()
        tab.run_button.emit('clicked')
        pump_until(entered.is_set)
        tab.run_button.emit('clicked')
        release.set()
        pump_until(lambda: tab.request is None)
        self.assertEqual([run['status'] for run in self.storage.library.decision_runs(tab.strategy.chat_id)], ['cancelled'])

    def test_each_run_is_added_to_the_same_session(self):
        tab = self.make_tab()
        first = self.run_tab(tab)
        session = tab.strategy.chat_id
        tab.chat_input.entry.restore_draft('A second message, calmer.')
        tab.form.rows[0].remove_button.emit('clicked')
        tab.picker.input.select_model('seeing')
        runs = self.run_tab(tab)
        self.assertEqual(tab.strategy.chat_id, session)
        self.assertEqual([(run['position'], run['state'], run['target']['model'], list(run['questions'])) for run in runs],
                         [(0, 'The invoice was charged twice.', 'blind', ['billing', 'tone', 'urgency']),
                          (1, 'A second message, calmer.', 'seeing', ['tone', 'urgency'])])
        self.assertEqual(runs[0], first[0])
        self.assertEqual(list(tab.runs), [run['id'] for run in runs])
        views = self.find(tab.results, RunView)
        self.assertEqual([view.run['id'] for view in views], [run['id'] for run in runs])
        self.assertIn('Run 2', views[1].title.get_text())
        self.assertEqual([answer.type for answer in views[1].answers()], ['choice', 'score'])
        # The session keeps the name its first run gave it, and the questions of its last.
        self.assertEqual(tab.title, 'The invoice was charged twice.')
        page = self.saved(tab)
        self.assertEqual((page['title'], page['kind'], list(page['options']['questions'])),
                         (tab.title, 'decision', ['tone', 'urgency']))
        self.assertEqual(len([chat for chat in self.storage.get_all_chats() if chat['kind'] == 'decision']), 1)

    def test_json_state_is_sent_parsed_and_invalid_json_blocks_run(self):
        tab = self.make_tab(state='{"message": "hello"}')
        tab.json_toggle.set_active(True)
        # The text goes to the JSON editor with the mode.
        self.assertEqual(tab.state_text(), '{"message": "hello"}')
        self.assertTrue(tab.json_frame.get_visible() and not tab.chat_input.get_visible())
        self.assertEqual(tab.chat_input.entry.read_draft(), '')
        tab.json_view.get_buffer().set_text('{"message": "hello",')
        self.assertFalse(tab.run_button.get_sensitive())
        self.assertIn('The state is not valid JSON', tab.notice.get_text())
        tab.run()
        self.decide.assert_not_called()
        text = '{"message": "hello",\n "parts": [1, 2.5, null]}'
        tab.json_view.get_buffer().set_text(text)
        self.assertFalse(tab.notice.get_visible())
        runs = self.run_tab(tab)
        self.assertEqual(self.decide.call_args.args[2], {'message': 'hello', 'parts': [1, 2.5, None]})
        self.assertEqual([(run['state'], run['state_mode'], run['status']) for run in runs], [(text, 'json', 'done')])
        self.assertEqual(self.saved(tab)['options']['state_mode'], 'json')
        tab.text_toggle.set_active(True)
        self.assertEqual(tab.chat_input.entry.read_draft(), text)
        self.assertEqual(self.text(tab.json_view), '')
        self.run_tab(tab)
        self.assertEqual(self.decide.call_args.args[2], text)

    # Reopening.

    def test_a_reopened_session_shows_its_runs_and_restores_the_editor(self):
        tab = self.make_tab()
        tab.picker.input.select_model('seeing')
        self.run_tab(tab)
        self.decide.side_effect = ProviderError('Gone.')
        tab.chat_input.entry.restore_draft('{"second": true}')
        tab.json_toggle.set_active(True)
        runs = self.run_tab(tab)
        page = self.saved(tab)
        tab.draft.close(discard=True)
        again = self.make_tab(saved=page)
        pump_until(lambda: again.picker.input.get_selected_model() == 'seeing')
        self.assertEqual((again.strategy.chat_id, again.title, again.mode), (page['id'], 'The invoice was charged twice.', 'decision'))
        self.assertEqual(list(again.runs), [run['id'] for run in runs])
        first, second = (again.runs[run['id']] for run in runs)
        self.assertEqual([answer.type for answer in first.answers()], ['noul', 'choice', 'score'])
        self.assertEqual(first.usage.get_text(), 'd3 · Input: 449 tokens · Output: 0 tokens')
        self.assertIn('Failed', second.title.get_text())
        self.assertEqual(second.error.get_text(), 'Gone.')
        self.assertEqual(again.form.value(), QUESTIONS)
        self.assertEqual((again.state_text(), again.json_toggle.get_active()), ('{"second": true}', True))
        self.assertFalse(again.draft.dirty)
        self.assertTrue(again.run_button.get_sensitive())
        self.decide.side_effect = None
        self.assertEqual(len(self.run_tab(again)), 3)
        self.assertIn('Run 3', list(again.runs.values())[2].title.get_text())

    def test_a_run_left_running_reads_as_interrupted_after_a_restart(self):
        tab = self.make_tab()
        self.run_tab(tab)
        with self.storage.db._get_conn() as conn:
            conn.execute("UPDATE decision_runs SET status='running', result=NULL")
            conn.commit()
        self.storage.db.decisions.interrupt_decisions()
        again = self.make_tab(saved=self.saved(tab))
        view = next(iter(again.runs.values()))
        self.assertIn('Interrupted', view.title.get_text())
        self.assertFalse(view.spinner.get_visible())
        self.assertEqual(view.answers(), [])

    def test_a_stored_box_question_and_answer_are_shown_without_an_image(self):
        questions = dict(QUESTIONS, where={'type': 'box', 'instructions': 'Where is the logo?'})
        settings = DecisionSettings.from_stored(dict(questions=questions, target=dict(
            host_id=self.host['id'], host=self.host['hostname'], model='seeing', host_name='localhost')))
        library = self.storage.library
        library.create_decision_run(dict(settings.to_stored(), id='boxed', session_id='session', new_session=True,
                                         state='See the image.', images=[]))
        library.finish_decision_run('boxed', 'done', BOXED)
        tab = self.make_tab(saved=self.storage.conversation_page('session'))
        view = tab.runs['boxed']
        self.assertEqual([answer.type for answer in view.answers()], ['noul', 'choice', 'score', 'box'])
        self.assertEqual(self.find(view, BoxPicture), [])
        self.assertFalse(view.images.get_visible())
        self.assertIn('Box: top 0.16, bottom 0.62, left 0.17, right 0.70', self.labels(view))
        self.assertEqual(view.usage.get_text(), 'typesafe/jev-1.14 · Input: 449 tokens · Output: 0 tokens · Cost: $0.000449')
        self.assertEqual(tab.form.value(), questions)
        self.assertEqual(tab.form.rows[3].kind, 'box')
        pump_until(lambda: tab.picker.input.get_selected_model() == 'seeing')
        # The local server answers no box question, so the run is held back with the reason.
        self.assertEqual(tab.notice.get_text(), 'This model cannot answer box questions.')
        self.assertFalse(tab.run_button.get_sensitive())

    def test_earlier_runs_are_loaded_on_request_and_a_found_run_is_brought_up(self):
        library = self.storage.library
        settings = DecisionSettings.from_stored(dict(questions=QUESTIONS, target=dict(host_id=self.host['id'], model='blind')))
        for index in range(70):
            library.create_decision_run(dict(settings.to_stored(), id=f'run-{index}', session_id='long',
                                             new_session=index == 0, state=f'State {index}', images=[]))
            library.finish_decision_run(f'run-{index}', 'done', ANSWER)
        tab = self.make_tab(saved=self.storage.conversation_page('long'))
        self.assertEqual(list(tab.runs), [f'run-{index}' for index in range(20, 70)])
        self.assertTrue(tab.older.get_visible())
        self.assertEqual(tab.state_text(), 'State 69')
        tab.older.emit('clicked')
        pump_until(lambda: len(tab.runs) == 70)
        self.assertFalse(tab.older.get_visible())
        self.assertEqual([view.run['id'] for view in self.find(tab.results, RunView)], [f'run-{index}' for index in range(70)])
        # A search hit on a page that is not shown.
        other = self.make_tab(saved=self.storage.conversation_page('long'))
        other.show_message('run-3')
        pump_until(lambda: 'run-3' in other.runs)
        self.assertEqual(list(other.runs), [f'run-{index}' for index in range(50)])
        self.assertFalse(other.older.get_visible())
        other.show_message('run-3')
        other.show_message('nothing')
        pump_until(lambda: self.context.services.idle)
        self.assertEqual(len(other.runs), 50)
        # Later runs are offered, and loaded up to the session's last.
        self.assertTrue(other.later.get_visible())
        other.later.emit('clicked')
        pump_until(lambda: len(other.runs) == 70)
        self.assertFalse(other.later.get_visible())
        self.assertEqual([view.run['id'] for view in self.find(other.results, RunView)],
                         [f'run-{index}' for index in range(70)])
        self.assertFalse(tab.later.get_visible())

    def test_a_session_opened_at_a_search_hit_edits_its_last_run_and_new_runs_follow_it(self):
        library = self.storage.library
        settings = DecisionSettings.from_stored(dict(questions=QUESTIONS, target=dict(host_id=self.host['id'], model='blind')))
        for index in range(70):
            library.create_decision_run(dict(settings.to_stored(), id=f'run-{index}', session_id='long',
                                             new_session=index == 0, state=f'State {index}', images=[]))
            library.finish_decision_run(f'run-{index}', 'done', ANSWER)
        tab = self.make_tab(saved=self.storage.conversation_page('long', 'run-3'))
        self.assertEqual(list(tab.runs), [f'run-{index}' for index in range(50)])
        self.assertTrue(tab.later.get_visible())
        # The editor holds the session's last run, not the last one on the page.
        self.assertEqual(tab.state_text(), 'State 69')
        tab.chat_input.entry.restore_draft('State 70')
        pump_until(lambda: tab.run_button.get_sensitive())
        tab.run_button.emit('clicked')
        pump_until(lambda: tab.request is None and self.storage.writer.idle and not self.storage.pending_callbacks
                   and len(tab.runs) == 50 and 'run-0' not in tab.runs)
        new = library.decision_runs('long')[-1]['id']
        # The new run follows the session's last, on the last page.
        self.assertEqual(list(tab.runs), [f'run-{index}' for index in range(21, 70)] + [new])
        self.assertFalse(tab.later.get_visible())

    # Drafts.

    def test_an_untouched_tab_saves_nothing_and_edits_are_saved_restored_and_discarded(self):
        tab = DecisionTab(self.context)
        self.tabs.append(tab)
        pump_until(lambda: settled(tab))
        self.assertFalse(has_saved_work(draft=tab.draft.snapshot()))
        tab.draft.flush()
        pump_until(lambda: self.storage.writer.idle)
        self.assertIsNone(self.storage.get_draft(tab.draft.id))
        self.assertFalse(tab.draft.dirty)
        # Choosing a model is an edit; a model list that arrives is not.
        tab.picker.input.select_model('seeing')
        self.assertTrue(tab.draft.dirty)
        tab.form.load(QUESTIONS)
        tab.form.rows[0].instructions.set_text('Is this about a bill?')
        tab.json_toggle.set_active(True)
        tab.json_view.get_buffer().set_text('{"draft": 1}')
        tab.draft.flush()
        pump_until(lambda: self.storage.writer.idle)
        draft = self.storage.get_draft(tab.draft.id)
        asked = copy.deepcopy(QUESTIONS)
        asked['billing']['instructions'] = 'Is this about a bill?'
        self.assertEqual((draft['mode'], draft['text'], draft['images'], draft['chat_id']), ('decision', '{"draft": 1}', [], None))
        settings = DecisionSettings.from_stored(draft['settings'])
        self.assertEqual((settings.questions, settings.state_mode, settings.target.host_id, settings.target.model),
                         (asked, 'json', self.host['id'], 'seeing'))
        self.assertTrue(has_saved_work(draft=draft))
        restored = self.make_tab(draft=draft)
        pump_until(lambda: restored.picker.input.get_selected_model() == 'seeing')
        self.assertEqual(restored.draft.id, tab.draft.id)
        self.assertEqual((restored.form.value(), restored.state_text(), restored.json_toggle.get_active()),
                         (asked, '{"draft": 1}', True))
        self.assertFalse(restored.draft.dirty)
        self.assertEqual(restored.draft.revision, draft['revision'])
        self.assertEqual(restored.title, 'New Decision')
        # An open editor keeps edits newer than the draft handed to it.
        restored.json_view.get_buffer().set_text('{"draft": 2}')
        restored.restore_draft(draft)
        self.assertEqual(restored.state_text(), '{"draft": 2}')
        updates = []
        restored.connect('chat-updated', lambda *args: updates.append(args[1:]))
        restored.discard_button.emit('clicked')
        pump_until(lambda: self.storage.writer.idle)
        self.assertIsNone(self.storage.get_draft(tab.draft.id))
        self.assertEqual((restored.form.value(), restored.state_text(), restored.json_toggle.get_active()), (DEFAULT, '', False))
        self.assertFalse(restored.draft.dirty)
        self.assertEqual(updates, [('', '')])

    def test_a_draft_with_only_questions_is_named_by_them_in_the_sidebar(self):
        tab = DecisionTab(self.context)
        self.tabs.append(tab)
        pump_until(lambda: settled(tab))
        tab.form.load(QUESTIONS)
        tab.form.rows[0].instructions.set_text('Edited')
        tab.draft.flush()
        pump_until(lambda: self.storage.writer.idle)
        rows = self.storage.sidebar_snapshot()['groups']['drafts']
        self.assertEqual([(row['id'], row['mode'], row['title']) for row in rows],
                         [(tab.draft.id, 'decision', 'billing, tone, urgency')])
        tab.chat_input.entry.restore_draft('Some state\nand more')
        tab.draft.flush()
        pump_until(lambda: self.storage.writer.idle)
        self.assertEqual(self.storage.sidebar_snapshot()['groups']['drafts'][0]['title'], 'Some state\nand more')

    def test_edits_after_a_run_are_the_sessions_draft_and_return_when_it_is_reopened(self):
        tab = self.make_tab()
        first = tab.draft.id
        self.run_tab(tab)
        session = tab.strategy.chat_id
        self.assertIsNone(self.storage.get_draft(first))
        self.assertEqual((tab.draft.id, tab.draft.chat_id), (session, session))
        self.assertFalse(tab.draft.dirty)
        tab.chat_input.entry.restore_draft('Not sent yet')
        tab.form.rows[2].remove_button.emit('clicked')
        tab.draft.flush()
        page = self.saved(tab)
        self.assertEqual(self.storage.get_draft(session)['text'], 'Not sent yet')
        # A session's draft is not listed beside it.
        self.assertEqual(self.storage.sidebar_snapshot()['groups']['drafts'], [])
        tab.draft.close()
        again = self.make_tab(saved=page)
        pump_until(lambda: again.state_text() == 'Not sent yet')
        self.assertEqual(list(again.form.value()), ['billing', 'tone'])
        self.assertFalse(again.draft.dirty)
        # Discarding goes back to what the last run asked.
        again.discard_button.emit('clicked')
        pump_until(lambda: self.storage.writer.idle)
        self.assertIsNone(self.storage.get_draft(session))
        self.assertEqual((again.state_text(), again.form.value()), ('The invoice was charged twice.', QUESTIONS))
        # The next run consumes the draft made before it.
        again.chat_input.entry.restore_draft('Sent now')
        again.draft.flush()
        runs = self.run_tab(again)
        self.assertEqual([run['state'] for run in runs], ['The invoice was charged twice.', 'Sent now'])
        self.assertIsNone(self.storage.get_draft(session))

    # Deleting and closing.

    def test_a_session_deleted_during_a_run_is_not_brought_back(self):
        tab = self.make_tab()
        self.run_tab(tab)
        session = tab.strategy.chat_id
        entered, release = self.stalled()
        tab.run_button.emit('clicked')
        pump_until(entered.is_set)
        self.storage.delete_chat(session)
        release.set()
        pump_until(lambda: tab.request is None and self.storage.writer.idle)
        self.assertIsNone(self.storage.conversation_page(session))
        self.assertEqual(self.storage.library.decision_runs(session), [])
        # Nor by a run started after it was deleted.
        self.decide.reset_mock(side_effect=True)
        self.decide.return_value = ANSWER
        tab.run_button.emit('clicked')
        pump_until(lambda: tab.request is None and self.storage.writer.idle and not self.storage.pending_callbacks)
        self.decide.assert_not_called()
        self.assertIsNone(self.storage.conversation_page(session))
        self.assertEqual(tab.notice.get_text(), 'Conversation no longer exists.')
        self.assertEqual(self.storage.get_all_chats(), [])

    def test_closing_during_a_run_cancels_it_and_keeps_the_session(self):
        entered, release = self.stalled()
        tab = self.make_tab()
        tab.run_button.emit('clicked')
        pump_until(entered.is_set)
        closed = []
        tab.close_session(lambda: closed.append(True))
        self.assertTrue(tab.closing)
        pump_until(lambda: closed)
        self.assertIsNone(tab.request)
        runs = self.storage.library.decision_runs(tab.strategy.chat_id)
        self.assertEqual([run['status'] for run in runs], ['cancelled'])
        self.assertTrue(tab.draft.closed)

    def test_shutdown_stops_a_run_and_leaves_nothing_pending(self):
        entered, release = self.stalled()
        tab = self.make_tab()
        tab.chat_input.entry.restore_draft('Keep me')
        tab.run_button.emit('clicked')
        pump_until(entered.is_set)
        tab.prepare_shutdown()
        pump_until(lambda: tab.request is None and self.context.idle and self.storage.writer.idle)
        self.assertFalse(tab.chat_input.pending_imports)
        self.assertEqual([run['status'] for run in self.storage.library.decision_runs(tab.strategy.chat_id)], ['cancelled'])
        # Had the application stayed open, the tab works again.
        tab.on_host_changed()
        pump_until(lambda: settled(tab))
        release.set()
        self.decide.side_effect = None
        self.assertEqual([run['status'] for run in self.run_tab(tab)], ['cancelled', 'done'])


@unittest.skipUnless(Gdk.Display.get_default(), 'GTK tests require a display')
class DecisionWindowTests(unittest.TestCase):
    setUp = DecisionTests.setUp
    close = DecisionTests.close
    run_tab = DecisionTests.run_tab

    def make_window(self):
        window = TureenWindow(context=self.context)
        self.windows.append(window)
        self.tabs.append(window.tab_view.get_nth_page(0).get_child())
        pump_until(lambda: self.storage.writer.idle and not window.sections['decisions'].history.pending)
        return window

    def decision(self, window):
        window.lookup_action('new_decision').activate(None)
        tab = window.sections['decisions'].tab_view.get_selected_page().get_child()
        self.tabs.append(tab)
        pump_until(lambda: settled(tab))
        return tab

    def test_the_action_opens_a_tab_and_its_runs_appear_under_recent_decisions(self):
        window = self.make_window()
        tab = self.decision(window)
        self.assertIsInstance(tab, DecisionTab)
        page = window.sections['decisions'].tab_view.get_page(tab)
        self.assertEqual((page.get_title(), page.get_icon().get_names()[0]), ('New Decision', 'tureen-decision-symbolic'))
        self.assertEqual(window.recent_decisions_section.get_title(), 'Recent decisions')
        tab.form.load(QUESTIONS)
        tab.chat_input.entry.restore_draft('The invoice was charged twice.')
        entered, release = DecisionTests.stalled(self)
        tab.run_button.emit('clicked')
        pump_until(entered.is_set)
        self.assertTrue(page.get_loading())
        self.assertTrue(window.is_model_busy(self.host, 'blind'))
        release.set()
        self.run_tab(tab)
        self.assertFalse(page.get_loading())
        self.assertFalse(window.is_model_busy(self.host, 'blind'))
        session = tab.strategy.chat_id
        pump_until(lambda: session in window.chat_rows and not window.sections['decisions'].history.pending)
        item = window.chat_rows[session]
        self.assertEqual((item.get_title(), item.get_icon_name(), item.get_section()),
                         ('The invoice was charged twice.', 'tureen-decision-symbolic', window.recent_decisions_section))
        self.assertEqual(page.get_title(), 'The invoice was charged twice.')
        theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
        icon = theme.lookup_icon(item.get_icon_name(), None, 16, 1, Gtk.TextDirection.LTR, Gtk.IconLookupFlags.FORCE_SYMBOLIC)
        self.assertEqual(icon.get_file().get_uri(),
                         'resource:///io/github/jackrabbithanna/Tureen/icons/scalable/actions/tureen-decision-symbolic.svg')
        # The row selects the open tab, and opens the session again once that is closed.
        window._open_history_item(item)
        self.assertIs(window.sections['decisions'].tab_view.get_selected_page().get_child(), tab)
        window.close_tab(tab)
        pump_until(lambda: tab not in window.tabs())
        self.assertIsNotNone(self.storage.get_chat(session))
        window._open_history_item(item)
        pump_until(lambda: any(isinstance(t, DecisionTab) for t in window.tabs()))
        again = next(t for t in window.tabs() if isinstance(t, DecisionTab))
        self.tabs.append(again)
        self.assertEqual((again.strategy.chat_id, len(again.runs), again.form.value()), (session, 1, QUESTIONS))
        self.assertIs(window.open_chat_tab(dict(id=session, kind='decision')), again)

    def test_a_search_hit_opens_the_session_at_its_run(self):
        window = self.make_window()
        tab = self.decision(window)
        tab.form.load(QUESTIONS)
        tab.chat_input.entry.restore_draft('Something ate the invoice.\nIt was an aardvark.')
        runs = self.run_tab(tab)
        window.close_tab(tab)
        pump_until(lambda: tab not in window.tabs())
        window.sections['decisions'].history.search.set_text('aardvark')
        session = tab.strategy.chat_id
        pump_until(lambda: not window.sections['decisions'].history.pending and getattr(window.chat_rows.get(session), 'match_uid', None))
        item = window.chat_rows[session]
        self.assertEqual((item.match_uid, item.get_section()), (runs[0]['id'], window.recent_decisions_section))
        with patch.object(DecisionTab, 'show_message') as show:
            window._open_history_item(item)
            pump_until(lambda: show.called)
        show.assert_called_once_with(runs[0]['id'])
        self.tabs.append(window.sections['decisions'].tab_view.get_selected_page().get_child())

    def test_a_closed_draft_is_listed_reopened_and_discarded_from_the_sidebar(self):
        window = self.make_window()
        tab = self.decision(window)
        tab.form.load(QUESTIONS)
        tab.form.rows[0].instructions.set_text('Edited')
        draft_id = tab.draft.id
        window.close_tab(tab)
        pump_until(lambda: tab not in window.tabs() and draft_id in window.draft_rows and not window.sections['decisions'].history.pending)
        item = window.draft_rows[draft_id]
        self.assertEqual((item.get_title(), item.get_section()), ('billing, tone, urgency', window.sections['decisions'].history.sections['drafts']))
        window.open_draft(draft_id)
        pump_until(lambda: any(t.draft.id == draft_id for t in window.tabs()))
        again = next(t for t in window.tabs() if t.draft.id == draft_id)
        self.tabs.append(again)
        self.assertIsInstance(again, DecisionTab)
        self.assertEqual(again.form.value()['billing']['instructions'], 'Edited')
        window.discard_draft(draft_id)
        pump_until(lambda: self.storage.writer.idle and draft_id not in window.draft_rows)
        self.assertIsNone(self.storage.get_draft(draft_id))
        self.assertEqual(again.form.value(), DEFAULT)

    def test_deleting_a_session_closes_its_tab_and_removes_its_runs(self):
        window = self.make_window()
        tab = self.decision(window)
        tab.form.load(QUESTIONS)
        tab.chat_input.entry.restore_draft('To be deleted')
        self.run_tab(tab)
        session = tab.strategy.chat_id
        entered, release = DecisionTests.stalled(self)
        tab.run_button.emit('clicked')
        pump_until(entered.is_set)
        window._delete_chat_confirmed(session)
        pump_until(lambda: tab not in window.tabs() and self.storage.writer.idle)
        self.assertIsNone(self.storage.get_chat(session))
        self.assertEqual(self.storage.library.decision_runs(session), [])
        self.assertIsNone(self.storage.get_draft(session))

    def test_the_shortcut_is_registered_and_listed(self):
        root = Path(__file__).resolve().parents[1]
        main = (root / 'src/main.py').read_text()
        self.assertIn("set_accels_for_action('win.new_decision', ['<control><shift>e'])", main)
        self.assertEqual(main.lower().count('<control><shift>e'), 1)
        self.assertIn('win.new_decision', (root / 'src/shortcuts-dialog.ui').read_text())
        self.assertIn('win.new_decision', (root / 'src/window.ui').read_text())


if __name__ == '__main__':
    unittest.main()
