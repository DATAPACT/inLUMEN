import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
from evaluate_pipelines import evaluate


class PipelineEvaluationTests(unittest.TestCase):
    def candidate(self):
        return {'id': 'test', 'graph': {
            'nodes': [{'id': name, 'data': {'type': kind, 'label': label}} for name, kind, label in
                      [('s', 'source', 'Input'), ('t', 'task', 'Normalize'), ('d', 'destination', 'Output')]],
            'edges': [{'source': 's', 'target': 't', 'sourceHandle': 'data', 'targetHandle': 'input'},
                      {'source': 't', 'target': 'd', 'sourceHandle': 'output', 'targetHandle': 'data'}]},
            'expected_kinds': {'source': 1, 'task': 1, 'destination': 1},
            'required_label_patterns': ['normaliz'], 'expected_connections': [['s', 't'], ['t', 'd']],
            'execution_success': True, 'cost_usd': 1, 'max_cost_usd': 2}

    def test_scores_structure_intent_execution_and_cost_independently(self):
        candidate = self.candidate()
        self.assertTrue(evaluate(candidate)['passed'])
        candidate['cost_usd'] = 3
        self.assertFalse(evaluate(candidate)['checks']['cost_within_budget'])
        candidate['execution_success'] = False
        self.assertFalse(evaluate(candidate)['checks']['execution_verified'])
        candidate['required_label_patterns'] = ['sentiment']
        self.assertFalse(evaluate(candidate)['checks']['required_tasks'])
        candidate['graph']['edges'].pop()
        self.assertFalse(evaluate(candidate)['checks']['valid_graph'])
        self.assertFalse(evaluate(candidate)['checks']['required_connections'])
