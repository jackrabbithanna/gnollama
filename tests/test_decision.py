import copy
import json
import unittest
from types import SimpleNamespace

from src.decision import (Answer, InvalidQuestions, Question, Result, box_rect, box_size_hint, build_request,
                          check_request, parse_questions, parse_result, questions_object)


# The request and response examples of docs/typesafe-ai-system-one.md, combined.
QUESTIONS = {
    'billing': {'type': 'noul', 'instructions': 'Is this message about billing?'},
    'tone': {'type': 'choice', 'instructions': 'What is the tone of this message?',
             'criteria': {'angry': 'An upset or hostile message', 'calm': 'A neutral or polite message',
                          'excited': 'An enthusiastic or eager message'}},
    'urgency': {'type': 'score', 'instructions': 'How urgent is this message?',
                'criteria': ['Can wait', 'Needs attention this week', 'Needs attention today']},
    'red_rectangle': {'type': 'box', 'instructions': 'Locate the red rectangle.'},
}
RESPONSE = {
    'model': 'jev-latest',
    'answers': {
        'billing': {'type': 'noul', 'noul': 0.98},
        'tone': {'type': 'choice', 'choice': 'calm', 'confidence': 0.7,
                 'probabilities': {'angry': 0.15, 'calm': 0.8, 'excited': 0.05}},
        'urgency': {'type': 'score', 'score': 1.7, 'confidence': 0.55,
                    'legend': {'0': 'Can wait', '1': 'Needs attention this week', '2': 'Needs attention today'},
                    'probabilities': {'0': 0.1, '1': 0.1, '2': 0.8}},
        'red_rectangle': {'type': 'box', 'box': {'top': 0.2, 'bottom': 0.66, 'left': 0.19, 'right': 0.68},
                          'confidence': None},
    },
    'usage': {'input_tokens': 285, 'output_tokens': 20},
}
ALL = SimpleNamespace(decisions=True, decision_images=True, decision_boxes=True, decision_question_limit=0)


def result(answers, **top):
    return parse_result({'model': 'm', 'answers': answers, 'usage': {'input_tokens': 1, 'output_tokens': 1}, **top},
                        parse_questions(QUESTIONS))


def answer(name, value):
    answers = copy.deepcopy(RESPONSE['answers'])
    answers[name] = value
    return {item.name: item for item in result(answers).answers}[name]


class QuestionTests(unittest.TestCase):
    def test_spec_examples_round_trip_from_an_object_text_and_records(self):
        questions = parse_questions(QUESTIONS)
        self.assertEqual([q.name for q in questions], list(QUESTIONS))
        self.assertEqual(questions[1], Question('tone', 'choice', 'What is the tone of this message?',
                                                QUESTIONS['tone']['criteria']))
        self.assertEqual(questions_object(questions), QUESTIONS)
        self.assertEqual(list(questions_object(questions)), list(QUESTIONS))
        self.assertEqual(questions_object(parse_questions(json.dumps(QUESTIONS))), QUESTIONS)
        self.assertEqual(parse_questions(questions), questions)
        self.assertEqual(parse_questions(tuple(questions)), questions)

    def test_parsed_and_exported_values_are_copies(self):
        source = copy.deepcopy(QUESTIONS)
        questions = parse_questions(source)
        source['tone']['criteria']['calm'] = 'changed'
        exported = questions_object(questions)
        exported['urgency']['criteria'].append('changed')
        self.assertEqual(questions_object(questions), QUESTIONS)

    def test_instructions_and_criteria_take_every_form_the_api_does(self):
        value = {
            'a': {'type': 'noul'},
            'b': {'type': 'noul', 'instructions': None, 'criteria': None},
            'c': {'type': 'noul', 'instructions': {'task': 'x'}, 'criteria': {}},
            'd': {'type': 'noul', 'instructions': ['x', 1], 'criteria': {'true': 'yes', 'false': None}},
            'e': {'type': 'choice', 'criteria': {'one': None, 'two': {'k': 1}, 'three': ['x'], '': 'blank'}},
            'f': {'type': 'score', 'criteria': ['low', {'k': 1}, ['x']]},
            'g': {'type': 'box', 'criteria': None},
        }
        exported = questions_object(parse_questions(value))
        self.assertEqual(exported['a'], {'type': 'noul'})
        self.assertEqual(exported['b'], {'type': 'noul'})
        self.assertEqual(exported['g'], {'type': 'box'})
        for name in 'cdef':
            self.assertEqual(exported[name], value[name])

    def test_question_sets_that_are_not_named_questions(self):
        for value in ('', '  \n', '{}', '[]', '"text"', 'null', '3', {}, [], (), None, 3,
                      [{'type': 'noul'}], [Question('a', 'noul'), 'b']):
            with self.subTest(value=value), self.assertRaisesRegex(InvalidQuestions, 'at least one named question'):
                parse_questions(value)
        for text in ('{', '{"a": {"type": "noul"}, "a": {"type": "noul"}}', '{"a": {"type": "noul", "x": NaN}}'):
            with self.subTest(text=text), self.assertRaises(InvalidQuestions):
                parse_questions(text)
        self.assertTrue(issubclass(InvalidQuestions, ValueError))

    def test_names_are_unique_and_not_blank(self):
        for value in ({'': {'type': 'noul'}}, {' \t': {'type': 'noul'}}, {3: {'type': 'noul'}},
                      [Question('a', 'noul'), Question('a', 'box')], [Question('', 'noul')]):
            with self.subTest(value=value), self.assertRaisesRegex(InvalidQuestions, 'unique and not blank'):
                parse_questions(value)
        with self.assertRaisesRegex(InvalidQuestions, '"twice"'):
            parse_questions([Question('twice', 'noul'), Question('twice', 'noul')])

    def test_type_is_required_and_known(self):
        for question in ({}, {'type': 'text'}, {'type': None}, {'type': ['noul']}, {'instructions': 'x'},
                         'noul', None, ['noul']):
            with self.subTest(question=question), self.assertRaisesRegex(InvalidQuestions, '"mine" needs a type'):
                parse_questions({'mine': question})

    def test_fields_that_are_refused_are_named_with_the_question(self):
        cases = [
            ({'type': 'noul', 'model': 'x'}, 'model'),
            ({'type': 'noul', 'instructions': 3}, 'instructions'),
            ({'type': 'box', 'instructions': True}, 'instructions'),
            ({'type': 'noul', 'criteria': ['yes']}, 'criteria'),
            ({'type': 'noul', 'criteria': 'yes'}, 'criteria'),
            ({'type': 'noul', 'criteria': {'true': 'yes', 'maybe': 'x'}}, r'criteria\.maybe'),
            ({'type': 'noul', 'criteria': {'true': 1}}, r'criteria\.true'),
            ({'type': 'choice', 'criteria': ['a', 'b']}, 'criteria'),
            ({'type': 'choice', 'criteria': {'a': 'x', 'b': 2}}, r'criteria\.b'),
            ({'type': 'score', 'criteria': {'0': 'low'}}, 'criteria'),
            ({'type': 'score', 'criteria': ['low', None]}, r'criteria\[1\]'),
            ({'type': 'score', 'criteria': ['low', 'mid', 3]}, r'criteria\[2\]'),
            ({'type': 'box', 'criteria': {}}, 'criteria'),
            ({'type': 'box', 'criteria': ['x']}, 'criteria'),
        ]
        for question, field in cases:
            with self.subTest(question=question), self.assertRaisesRegex(
                    InvalidQuestions, f'"mine" has an invalid field: {field}$'):
                parse_questions({'mine': question})

    def test_choice_and_score_criteria_counts(self):
        for kind, most, make in (('choice', 255, lambda n: {str(i): None for i in range(n)}),
                                 ('score', 10, lambda n: ['level'] * n)):
            for count in (1, most):
                parsed = parse_questions({'mine': {'type': kind, 'criteria': make(count)}})
                self.assertEqual(len(parsed[0].criteria), count)
            for question in ({'type': kind}, {'type': kind, 'criteria': None},
                             {'type': kind, 'criteria': make(0)}, {'type': kind, 'criteria': make(most + 1)}):
                with self.subTest(kind=kind, count=len(question.get('criteria') or ())), self.assertRaisesRegex(
                        InvalidQuestions, f'"mine" needs between 1 and {most} criteria'):
                    parse_questions({'mine': question})


class RequestTests(unittest.TestCase):
    def setUp(self):
        self.questions = parse_questions(QUESTIONS)
        self.text = [q for q in self.questions if q.type != 'box']

    def test_a_request_the_model_takes_has_no_reason(self):
        self.assertEqual(check_request(self.questions, ['image'], ALL), '')
        self.assertEqual(check_request(self.text, [], ALL), '')
        self.assertEqual(check_request(self.text, None, ALL), '')
        self.assertEqual(check_request(self.text, ['a', 'b'], ALL), '')
        self.assertEqual(check_request(self.text, (), SimpleNamespace(decisions=True)), '')

    def test_capabilities_without_decision_fields_refuse(self):
        for capabilities in (object(), SimpleNamespace(decisions=False, decision_images=True)):
            self.assertIn('does not offer decision models', check_request(self.text, [], capabilities))

    def test_question_limit(self):
        limited = SimpleNamespace(decisions=True, decision_question_limit=3)
        self.assertEqual(check_request(self.text, [], limited), '')
        four = self.text + [Question('more', 'noul')]
        self.assertIn('at most 3 questions', check_request(four, [], limited))
        self.assertEqual(check_request(four, [], SimpleNamespace(decisions=True, decision_question_limit=0)), '')

    def test_images_and_boxes_need_their_capabilities(self):
        no_images = SimpleNamespace(decisions=True, decision_boxes=True)
        self.assertIn('does not accept images', check_request(self.text, ['image'], no_images))
        no_boxes = SimpleNamespace(decisions=True, decision_images=True)
        self.assertIn('cannot answer box questions', check_request(self.questions, ['image'], no_boxes))
        self.assertIn('cannot answer box questions', check_request(self.questions, [], no_boxes))

    def test_a_box_question_needs_exactly_one_image(self):
        for images in (None, [], ['a', 'b']):
            with self.subTest(images=images):
                self.assertIn('exactly one image', check_request(self.questions, images, ALL))

    def test_build_request(self):
        state = {'subject': 'Duplicate charge', 'lines': ['Please help.']}
        request = build_request('jev-latest', state, self.questions)
        self.assertEqual(request, {'model': 'jev-latest', 'state': state, 'questions': QUESTIONS})
        self.assertEqual(list(request), ['model', 'state', 'questions'])
        request['state']['lines'].append('changed')
        self.assertEqual(state['lines'], ['Please help.'])
        self.assertEqual(build_request('m', 'text', self.text[:1])['state'], 'text')
        json.dumps(request, allow_nan=False)


class ResultTests(unittest.TestCase):
    def test_spec_examples(self):
        parsed = parse_result(RESPONSE, parse_questions(QUESTIONS))
        self.assertIsInstance(parsed, Result)
        self.assertEqual((parsed.model, parsed.input_tokens, parsed.output_tokens, parsed.cost, parsed.extra),
                         ('jev-latest', 285, 20, None, {}))
        billing, tone, urgency, box = parsed.answers
        self.assertEqual(billing, Answer('billing', 'noul', 0.98))
        self.assertEqual(tone, Answer('tone', 'choice', 'calm', 0.7, {'angry': 0.15, 'calm': 0.8, 'excited': 0.05}))
        self.assertEqual(urgency, Answer(
            'urgency', 'score', 1.7, 0.55, {'0': 0.1, '1': 0.1, '2': 0.8},
            {'0': 'Can wait', '1': 'Needs attention this week', '2': 'Needs attention today'}))
        self.assertEqual(box, Answer('red_rectangle', 'box',
                                     box={'top': 0.2, 'bottom': 0.66, 'left': 0.19, 'right': 0.68}))

    def test_json_text_and_question_order(self):
        text = json.dumps(dict(RESPONSE, answers=dict(reversed(RESPONSE['answers'].items()))))
        parsed = parse_result(text, parse_questions(QUESTIONS))
        self.assertEqual([a.name for a in parsed.answers], list(QUESTIONS))
        self.assertEqual(parsed, parse_result(RESPONSE, parse_questions(QUESTIONS)))

    def test_responses_without_an_answers_object_raise(self):
        for raw in (None, [], 'null', '[]', '"text"', {}, {'model': 'm'}, {'answers': []}, {'answers': None},
                    '{"answers": "none"}', {'error': {'message': 'no'}}):
            with self.subTest(raw=raw), self.assertRaisesRegex(ValueError, 'no answers'):
                parse_result(raw, parse_questions(QUESTIONS))
        for text in ('', '{', '{"answers": {"a": NaN}}'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_result(text, parse_questions(QUESTIONS))

    def test_optional_and_null_confidence(self):
        self.assertIsNone(answer('billing', {'type': 'noul', 'noul': 1}).confidence)
        self.assertEqual(answer('billing', {'type': 'noul', 'noul': 0, 'confidence': 1}),
                         Answer('billing', 'noul', 0.0, 1.0))
        self.assertIsNone(answer('billing', {'type': 'noul', 'noul': 0.5, 'confidence': None}).confidence)
        box = {'top': 0, 'bottom': 1, 'left': 0, 'right': 1}
        self.assertEqual(answer('red_rectangle', {'type': 'box', 'box': box, 'confidence': 0.9}).confidence, 0.9)
        self.assertIsNone(answer('red_rectangle', {'type': 'box', 'box': box}).confidence)
        tone = answer('tone', {'type': 'choice', 'choice': 'angry'})
        self.assertEqual((tone.type, tone.value, tone.confidence, tone.probabilities), ('choice', 'angry', None, {}))
        urgency = answer('urgency', {'type': 'score', 'score': 2})
        self.assertEqual((urgency.type, urgency.value, urgency.legend, urgency.probabilities), ('score', 2.0, {}, {}))

    def test_refusal_and_missing(self):
        parsed = result({'billing': {'type': 'refusal', 'reason': 'policy'}, 'tone': {'type': 'refusal'}})
        self.assertEqual(parsed.answers, (
            Answer('billing', 'refusal', extra={'reason': 'policy'}), Answer('tone', 'refusal'),
            Answer('urgency', 'missing'), Answer('red_rectangle', 'missing')))
        self.assertEqual([a.type for a in result({}).answers], ['missing'] * 4)

    def test_an_answer_of_another_type_is_invalid(self):
        for name, value in (('billing', RESPONSE['answers']['tone']), ('tone', {'type': 'noul', 'noul': 0.5}),
                            ('urgency', {'score': 1}), ('billing', {'type': 'text', 'text': 'yes'}),
                            ('red_rectangle', {'type': None})):
            with self.subTest(name=name, value=value):
                parsed = answer(name, value)
                self.assertEqual((parsed.type, parsed.extra), ('invalid', value))
                self.assertIn('not valid', parsed.note)
                self.assertIsNone(parsed.value)
        for value in (0.98, 'yes', None, ['noul']):
            with self.subTest(value=value):
                parsed = answer('billing', value)
                self.assertEqual((parsed.type, parsed.extra), ('invalid', {'value': value}))

    def test_numbers_out_of_range_or_not_finite_are_invalid(self):
        nan, inf = float('nan'), float('inf')
        box = RESPONSE['answers']['red_rectangle']['box']
        cases = [('billing', {'type': 'noul', 'noul': value})
                 for value in (nan, inf, -inf, -0.01, 1.01, True, '0.5', None, 10 ** 400)]
        cases += [('billing', {'type': 'noul'}),
                  ('billing', {'type': 'noul', 'noul': 0.5, 'confidence': 1.5}),
                  ('billing', {'type': 'noul', 'noul': 0.5, 'confidence': nan}),
                  ('billing', {'type': 'noul', 'noul': 0.5, 'confidence': 'high'}),
                  ('tone', {'type': 'choice', 'choice': 'furious', 'confidence': 0.5, 'probabilities': {}}),
                  ('tone', {'type': 'choice', 'choice': 3}),
                  ('tone', {'type': 'choice'}),
                  ('tone', {'type': 'choice', 'choice': 'calm', 'probabilities': {'calm': 1.2}}),
                  ('tone', {'type': 'choice', 'choice': 'calm', 'probabilities': {'calm': inf}}),
                  ('tone', {'type': 'choice', 'choice': 'calm', 'probabilities': {'calm': False}}),
                  ('tone', {'type': 'choice', 'choice': 'calm', 'probabilities': [0.5, 0.5]}),
                  ('tone', {'type': 'choice', 'choice': 'calm', 'confidence': -1}),
                  ('urgency', {'type': 'score', 'score': 2.01}),
                  ('urgency', {'type': 'score', 'score': -0.5}),
                  ('urgency', {'type': 'score', 'score': nan}),
                  ('urgency', {'type': 'score', 'score': '1'}),
                  ('urgency', {'type': 'score', 'score': 1, 'legend': ['Can wait']}),
                  ('urgency', {'type': 'score', 'score': 1, 'probabilities': {'0': 2}}),
                  ('red_rectangle', {'type': 'box'}),
                  ('red_rectangle', {'type': 'box', 'box': [0, 1, 0, 1]}),
                  ('red_rectangle', {'type': 'box', 'box': {'top': 0.2, 'bottom': 0.6, 'left': 0.1}}),
                  ('red_rectangle', {'type': 'box', 'box': dict(box, right=1.2)}),
                  ('red_rectangle', {'type': 'box', 'box': dict(box, top=-0.1)}),
                  ('red_rectangle', {'type': 'box', 'box': dict(box, left=nan)}),
                  ('red_rectangle', {'type': 'box', 'box': dict(box, top=0.7)}),
                  ('red_rectangle', {'type': 'box', 'box': dict(box, left=0.9)}),
                  ('red_rectangle', {'type': 'box', 'box': box, 'confidence': 2})]
        for name, value in cases:
            with self.subTest(name=name, value=value):
                parsed = answer(name, value)
                self.assertEqual(parsed.type, 'invalid')
                self.assertTrue(parsed.note)
                self.assertEqual(list(parsed.extra), list(value))
        others = result(dict(RESPONSE['answers'], billing={'type': 'noul', 'noul': 7})).answers
        self.assertEqual([a.type for a in others], ['invalid', 'choice', 'score', 'box'])

    def test_edges_of_the_ranges_are_valid(self):
        self.assertEqual(answer('urgency', {'type': 'score', 'score': 0}).value, 0.0)
        self.assertEqual(answer('urgency', {'type': 'score', 'score': 2}).value, 2.0)
        edge = {'top': 0, 'bottom': 0, 'left': 1, 'right': 1}
        self.assertEqual(answer('red_rectangle', {'type': 'box', 'box': edge}).box,
                         {'top': 0.0, 'bottom': 0.0, 'left': 1.0, 'right': 1.0})

    def test_unknown_fields_are_kept(self):
        raw = copy.deepcopy(RESPONSE)
        raw['answers']['billing']['reasoning'] = {'steps': 2}
        raw['answers']['tone']['native'] = 'calm.'
        raw['answers']['unasked'] = {'type': 'noul', 'noul': 0.1}
        raw['id'] = 'gen-1'
        raw['usage']['total_tokens'] = 305
        parsed = parse_result(raw, parse_questions(QUESTIONS))
        self.assertEqual(parsed.answers[0], Answer('billing', 'noul', 0.98, extra={'reasoning': {'steps': 2}}))
        self.assertEqual(parsed.answers[1].extra, {'native': 'calm.'})
        self.assertEqual(parsed.answers[2].extra, {})
        self.assertEqual(parsed.extra, {'id': 'gen-1', 'answers': {'unasked': {'type': 'noul', 'noul': 0.1}},
                                        'usage': {'total_tokens': 305}})
        raw['answers']['billing']['reasoning']['steps'] = 3
        self.assertEqual(parsed.answers[0].extra, {'reasoning': {'steps': 2}})

    def test_cost_from_either_place(self):
        usage = {'input_tokens': 9, 'output_tokens': 2}
        self.assertEqual(result({}, usage=dict(usage, cost=0.00012)).cost, 0.00012)
        self.assertEqual(result({}, usage=dict(usage, cost=0)).cost, 0.0)
        gateway = {'gateway': {'cost': '0.0000409', 'routing': {'provider': 'typesafe'}}}
        parsed = result({}, usage=usage, provider_metadata=gateway)
        self.assertEqual(parsed.cost, 0.0000409)
        self.assertEqual(parsed.extra, {'provider_metadata': gateway})
        self.assertEqual(result({}, usage=dict(usage, cost=0.5), provider_metadata=gateway).cost, 0.5)
        self.assertEqual(result({}, usage=dict(usage, cost='free'), provider_metadata=gateway).cost, 0.0000409)
        for cost in ('free', '', 'nan', 'inf', -1, '-0.5', True, None, [0.1], float('nan')):
            with self.subTest(cost=cost):
                self.assertIsNone(result({}, usage=dict(usage, cost=cost)).cost)
                self.assertIsNone(result({}, usage=usage, provider_metadata={'gateway': {'cost': cost}}).cost)
        for metadata in (None, 'text', {}, {'gateway': None}, {'gateway': '0.1'}):
            with self.subTest(metadata=metadata):
                self.assertIsNone(result({}, usage=usage, provider_metadata=metadata).cost)

    def test_usage_that_is_absent_or_malformed(self):
        for usage in (None, [], 'none', {}):
            with self.subTest(usage=usage):
                parsed = parse_result({'answers': {}, 'usage': usage}, [])
                self.assertEqual((parsed.model, parsed.answers, parsed.input_tokens, parsed.output_tokens),
                                 ('', (), None, None))
        self.assertIsNone(parse_result({'answers': {}}, []).input_tokens)
        parsed = result({}, usage={'input_tokens': -1, 'output_tokens': 2.5})
        self.assertEqual((parsed.input_tokens, parsed.output_tokens), (None, None))
        parsed = result({}, usage={'input_tokens': True, 'output_tokens': 0})
        self.assertEqual((parsed.input_tokens, parsed.output_tokens), (None, 0))
        self.assertEqual(parse_result({'model': 7, 'answers': {}}, []).model, '')


class BoxRectTests(unittest.TestCase):
    BOX = {'top': 0.25, 'bottom': 0.75, 'left': 0.125, 'right': 0.625}

    def test_exact_fit(self):
        self.assertEqual(box_rect(self.BOX, 200, 100, 200, 100), (25.0, 25.0, 100.0, 50.0))
        self.assertEqual(box_rect(self.BOX, 400, 200, 200, 100), (25.0, 25.0, 100.0, 50.0))

    def test_bars_left_and_right_in_a_wide_widget(self):
        # A 100x100 image shown 200x200 in the middle of 400x200.
        self.assertEqual(box_rect(self.BOX, 100, 100, 400, 200), (125.0, 50.0, 100.0, 100.0))

    def test_bars_above_and_below_in_a_tall_widget(self):
        # A 200x100 image shown 300x150 in the middle of 300x450.
        self.assertEqual(box_rect(self.BOX, 200, 100, 300, 450), (37.5, 187.5, 150.0, 75.0))

    def test_whole_image_and_empty_sizes(self):
        whole = {'top': 0, 'bottom': 1, 'left': 0, 'right': 1}
        self.assertEqual(box_rect(whole, 320, 320, 100, 300), (0.0, 100.0, 100.0, 100.0))
        for sizes in ((0, 100, 200, 200), (100, 0, 200, 200), (100, 100, 0, 200), (100, 100, 200, -1)):
            with self.subTest(sizes=sizes):
                self.assertEqual(box_rect(self.BOX, *sizes), (0.0, 0.0, 0.0, 0.0))


class SizeHintTests(unittest.TestCase):
    def test_images_outside_the_usual_sides_are_advised_on_only_with_a_box_question(self):
        hint = 'Some models need images between 256 and 1568 pixels on each side.'
        for sizes in ([(255, 400)], [(400, 255)], [(1569, 400)], [(400, 1569)], [(320, 320), (100, 2000)]):
            with self.subTest(sizes=sizes):
                self.assertEqual(box_size_hint(True, sizes), hint)
                self.assertEqual(box_size_hint(False, sizes), '')
        for sizes in ([], [(256, 256)], [(1568, 1568)], [(320, 320), (256, 1568)]):
            with self.subTest(sizes=sizes):
                self.assertEqual(box_size_hint(True, sizes), '')


if __name__ == '__main__':
    unittest.main()
