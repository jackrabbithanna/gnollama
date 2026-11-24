import unittest
from src.records import RequestSettings
from src.session import RequestState, api_messages


def settings(**values):
    return RequestSettings.from_stored(dict(dict(
        host='http://localhost:11434', host_id='host', model='test', options={},
        thinking=None, system=None, logprobs=False, top_logprobs=None, show_stats=True), **values))


class SessionTests(unittest.TestCase):
    def test_requests_have_independent_snapshots(self):
        config = settings()
        first = RequestState(config, 'first')
        second = RequestState(config, 'second')
        config.wire.options['temperature'] = 0.3
        first.consume({'message': {'thinking': 'reason', 'content': 'one'}})
        second.consume({'response': 'two', 'done': True, 'prompt_eval_cached_count': 0})
        self.assertEqual(first.content, 'one')
        self.assertEqual(first.thinking, 'reason')
        self.assertEqual(second.content, 'two')
        self.assertEqual(first.settings.wire.options, {})
        self.assertEqual(second.metadata['metrics']['prompt_eval_cached_count'], 0)
        self.assertTrue(first.finish('stopped'))
        self.assertFalse(first.finish('complete'))
        self.assertEqual(first.metadata['status'], 'stopped')

    def test_local_metadata_is_not_sent_to_ollama(self):
        result = api_messages([{'role': 'assistant', 'content': 'partial',
                                'response_metadata': {'status': 'failed', 'error': 'secret'},
                                'api_details': {'host': 'local'}, 'model': 'test'},
                               {'role': 'assistant', 'content': '', 'response_metadata': {'status': 'stopped'}},
                               {'role': 'user', 'content': 'continue', 'images': ['image']}])
        self.assertEqual(result, [{'role': 'assistant', 'content': 'partial'},
                                  {'role': 'user', 'content': 'continue', 'images': ['image']}])
