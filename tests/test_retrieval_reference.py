import unittest
from tools.evaluate_retrieval import evaluate


class RetrievalReferenceTests(unittest.TestCase):
    def test_recorded_rankings_and_unanswerable_case(self):
        report = evaluate()
        self.assertEqual(report['recall_at_6'], 1)
        self.assertEqual(report['mean_reciprocal_rank'], 1)
        self.assertEqual({case['kind'] for case in report['cases']},
                         {'answerable', 'unanswerable', 'follow-up', 'misleading-source'})
        unanswered = next(case for case in report['cases'] if case['kind'] == 'unanswerable')
        self.assertIsNone(unanswered['recall_at_6'])
        self.assertEqual(unanswered['expected'], [])
