import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from provenance_report import (  # noqa: E402
    _representative_snapshot_events,
    build_provenance_pdf,
)


def _event(uid: str, nodes: list[dict], edges: list[dict]) -> dict:
    return {
        "uid": uid,
        "actor": "manual",
        "action": "node_created",
        "summary": f"Applied {uid}.",
        "created_at": "2026-06-24T10:00:00Z",
        "details": {
            "graph_snapshot": {
                "node_count": len(nodes),
                "edge_count": len(edges),
                "nodes": nodes,
                "edges": edges,
            }
        },
    }


class ProvenanceReportTest(unittest.TestCase):
    def test_deduplicates_consecutive_graph_snapshots(self):
        one_node = [{"id": "1", "label": "Input", "type": "input", "x": 0, "y": 0}]
        two_nodes = one_node + [
            {"id": "2", "label": "Train", "type": "action", "x": 300, "y": 0}
        ]
        events = [
            _event("event-1", one_node, []),
            _event("event-2", one_node, []),
            _event("event-3", two_nodes, [{"source": "1", "target": "2"}]),
        ]

        snapshots, total = _representative_snapshot_events(events)

        self.assertEqual(2, total)
        self.assertEqual([1, 3], [entry["event_index"] for entry in snapshots])

    def test_generates_pdf_with_graph_snapshots(self):
        events = [
            _event(
                "event-1",
                [{"id": "1", "label": "Input", "type": "input", "x": 0, "y": 0}],
                [],
            ),
            _event(
                "event-2",
                [
                    {"id": "1", "label": "Input", "type": "input", "x": 0, "y": 0},
                    {"id": "2", "label": "Train", "type": "action", "x": 300, "y": 0},
                ],
                [{"source": "1", "target": "2"}],
            ),
        ]
        pdf = build_provenance_pdf({
            "pipeline": {"uid": "pipeline-1", "name": "Demo"},
            "version": {"uid": "main", "name": "Main"},
            "events": events,
        })

        self.assertTrue(pdf.startswith(b"%PDF-"))
        self.assertTrue(pdf.rstrip().endswith(b"%%EOF"))
        self.assertGreater(len(pdf), 3000)

    def test_adds_current_state_when_historical_snapshots_are_missing(self):
        current_snapshot = {
            "node_count": 1,
            "edge_count": 0,
            "nodes": [
                {"id": "1", "label": "Input", "type": "input", "x": 0, "y": 0}
            ],
            "edges": [],
        }

        snapshots, total = _representative_snapshot_events(
            [{"uid": "old-event", "details": {}}],
            current_snapshot,
            "2026-06-24T12:00:00Z",
        )

        self.assertEqual(1, total)
        self.assertIsNone(snapshots[0]["event_index"])
        self.assertEqual("current_state", snapshots[0]["event"]["action"])


if __name__ == "__main__":
    unittest.main()
