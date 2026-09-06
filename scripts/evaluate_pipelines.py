#!/usr/bin/env python3
"""Score recorded pipeline candidates against explicit intent requirements.

Input: JSON list of {id, graph, expected_kinds, expected_connections,
execution_success, elapsed_seconds, cost_usd}. Never calls a model or executes code.
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from pipeline_graph_validation import validate_pipeline_graph


def evaluate(case):
    graph = case['graph']
    nodes = graph.get('nodes', [])
    kinds = [(node.get('data') or node).get('type') for node in nodes]
    connections = {(str(edge['source']), str(edge['target'])) for edge in graph.get('edges', [])}
    validation = validate_pipeline_graph(graph)
    checks = {
        'valid_graph': validation['valid'],
        'required_components': all(kinds.count(kind) >= count for kind, count in case.get('expected_kinds', {}).items()),
        'required_connections': all(tuple(edge) in connections for edge in case.get('expected_connections', [])),
        'execution_verified': case.get('execution_success') is True,
        'required_tasks': all(any(re.search(pattern, str((node.get('data') or node).get('label', '')), re.I)
                                  for node in nodes) for pattern in case.get('required_label_patterns', [])),
        'cost_within_budget': case.get('max_cost_usd') is None or (
            isinstance(case.get('cost_usd'), (int, float)) and 0 <= case['cost_usd'] <= case['max_cost_usd']),
        'time_within_budget': case.get('max_elapsed_seconds') is None or (
            isinstance(case.get('elapsed_seconds'), (int, float)) and 0 <= case['elapsed_seconds'] <= case['max_elapsed_seconds']),
    }
    return {'id': case['id'], 'checks': checks, 'passed': all(checks.values()),
            'elapsed_seconds': case.get('elapsed_seconds'), 'cost_usd': case.get('cost_usd')}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('candidates', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--suite', type=Path, help='Intent requirements, keyed by id; missing candidates fail')
    args = parser.parse_args()
    candidates = json.loads(args.candidates.read_text())
    if args.suite:
        by_id = {case['id']: case for case in candidates}
        candidates = [{**by_id.get(case['id'], {'graph': {'nodes': [], 'edges': []}}), **case}
                      for case in json.loads(args.suite.read_text())]
    results = [evaluate(case) for case in candidates]
    report = {'cases': results, 'pass_rate': sum(case['passed'] for case in results) / max(len(results), 1)}
    encoded = json.dumps(report, indent=2)
    if args.output: args.output.write_text(encoded + '\n')
    else: print(encoded)
    raise SystemExit(0 if results and all(case['passed'] for case in results) else 1)
