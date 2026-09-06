"""Validate canvas document shape before any destructive graph synchronization."""
import math


def validate_graph_document(graph):
    if not isinstance(graph, dict) or not isinstance(graph.get('nodes'), list) or not isinstance(graph.get('edges'), list):
        raise ValueError('graph must contain nodes and edges arrays')
    if len(graph['nodes']) > 2000 or len(graph['edges']) > 10000:
        raise ValueError('Graph exceeds the supported size (2000 nodes, 10000 edges)')
    ids = set()
    for node in graph['nodes']:
        if not isinstance(node, dict) or not str(node.get('id') or '').strip():
            raise ValueError('Every node requires an id')
        node_id = str(node['id'])
        if node_id in ids:
            raise ValueError('Node ids must be unique')
        ids.add(node_id)
        if 'data' in node and not isinstance(node['data'], dict):
            raise ValueError('Node data must be an object')
        if 'position' in node:
            position = node['position']
            if not isinstance(position, dict) or any(
                not isinstance(position.get(axis), (int, float)) or not math.isfinite(position[axis])
                for axis in ('x', 'y')
            ):
                raise ValueError('Node positions must have finite x and y coordinates')
    for edge in graph['edges']:
        if not isinstance(edge, dict) or str(edge.get('source')) not in ids or str(edge.get('target')) not in ids:
            raise ValueError('Every connection must reference existing nodes')
