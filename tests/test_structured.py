import copy
import unittest
from unittest.mock import patch

from src.structured import InvalidSchema, request_format, validate_response, formatted_json
from src.session import RequestState, api_messages
from test_session import settings


SCHEMA = {'type': 'object', 'properties': {'count': {'type': 'integer'}}, 'required': ['count']}


class StructuredTests(unittest.TestCase):
    def test_empty_schema_has_actionable_error(self):
        for text in ('', ' \n\t'):
            with self.assertRaisesRegex(InvalidSchema, 'Paste or import.*Apply'):
                request_format('schema', text)

    def test_modes_and_schema_errors(self):
        self.assertIsNone(request_format('text', 'invalid dormant schema'))
        self.assertEqual(request_format('json'), 'json')
        for text in ('', '{', '[]', 'false', '{"type":"wrong"}', '{"$schema":"https://unknown"}',
                     '{"x":NaN}', '{"type":"object", "type":"array"}'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                request_format('schema', text)

    def test_self_contained_refs_validate_without_retrieval(self):
        schema = request_format('schema', '{"$defs":{"integer":{"type":"integer"}},"properties":{"count":{"$ref":"#/$defs/integer"}}}')
        self.assertEqual(validate_response('{"count":2}', schema)['status'], 'valid')
        self.assertEqual(validate_response('{"count":"two"}', schema)['status'], 'schema_mismatch')
        for ref in ('https://example.org/schema', 'file:///tmp/schema.json', '#/$defs/missing'):
            with self.assertRaisesRegex(ValueError, 'Unresolved'):
                request_format('schema', '{"properties":{"optional":{"$ref":"'+ref+'"}}}')

    def test_validation_and_export_preserve_raw_text(self):
        raw = '{ "count" : 2 }'
        self.assertEqual(validate_response(raw, SCHEMA), {'status': 'valid'})
        mismatch = validate_response('{"count":"two"}', SCHEMA)
        self.assertEqual(mismatch['path'], '/count')
        self.assertEqual(mismatch['status'], 'schema_mismatch')
        for invalid in ('{', '```json\n{}\n```', 'NaN', 'Infinity', '{"a":1,"a":2}'):
            self.assertEqual(validate_response(invalid, 'json')['status'], 'invalid_json')
        self.assertEqual(formatted_json(raw), '{\n  "count": 2\n}\n')
        self.assertEqual(raw, '{ "count" : 2 }')
        self.assertIsNone(validate_response('ordinary text', None))

    def test_request_snapshot_retains_format_and_incomplete_status(self):
        config = settings()
        config.update(format=copy.deepcopy(SCHEMA), output_mode='schema', schema_text='schema source', keep_alive=0)
        state = RequestState(config, 'test')
        config['format']['type'] = 'array'
        state.content = '{'
        state.finish('stopped')
        self.assertEqual(state.metadata['validation']['status'], 'incomplete')
        self.assertEqual(state.api_details()['format']['type'], 'object')
        self.assertNotIn('schema_text', state.api_details())
        self.assertEqual(state.api_details()['keep_alive'], 0)

    def test_text_only_serialization_keeps_saved_images(self):
        history = [{'role': 'user', 'content': 'describe', 'images': ['original']},
                   {'role': 'assistant', 'content': 'a cat', 'response_metadata': {'validation': {'status': 'valid'}}}]
        original = copy.deepcopy(history)
        self.assertEqual(api_messages(history, include_images=False),
                         [{'role': 'user', 'content': 'describe'}, {'role': 'assistant', 'content': 'a cat'}])
        self.assertEqual(api_messages(history)[0]['images'], ['original'])
        self.assertEqual(history, original)
