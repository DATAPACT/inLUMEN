import json
import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from provenance_provo import (  # noqa: E402
    PROV_NAMESPACE,
    build_prov_o_jsonld,
    provenance_prov_o_filename,
)


class ProvenanceProvOTest(unittest.TestCase):
    def test_builds_prov_o_state_chain_with_user_query(self):
        document = build_prov_o_jsonld({
            "pipeline": {
                "uid": "pipeline-1",
                "name": "Clinical pipeline",
                "created_at": "2026-06-20T08:00:00Z",
                "updated_at": "2026-06-20T08:10:00Z",
            },
            "version": {
                "uid": "main",
                "name": "Main",
                "created_at": "2026-06-20T08:00:00Z",
                "updated_at": "2026-06-20T08:10:00Z",
            },
            "events": [
                {
                    "uid": "event-1",
                    "actor": "manual",
                    "action": "node_created",
                    "summary": "Created an input step.",
                    "details": {"flow_id": "1"},
                    "version_uid": "main",
                    "created_at": "2026-06-20T08:05:00Z",
                },
                {
                    "uid": "event-2",
                    "actor": "agent",
                    "action": "create_step",
                    "summary": "Agent created a report step.",
                    "details": {
                        "user_query": "Add a report step",
                        "query_type": "create_step",
                        "session_id": "session-1",
                    },
                    "version_uid": "main",
                    "created_at": "2026-06-20T08:10:00Z",
                },
                {
                    "uid": "event-3",
                    "actor": "manual",
                    "action": "undo_applied",
                    "summary": "Restored the previous graph snapshot with Undo.",
                    "details": {
                        "direction": "undo",
                        "source_snapshot_fingerprint": "fnv1a-state2",
                        "target_snapshot_fingerprint": "fnv1a-state1",
                    },
                    "version_uid": "main",
                    "created_at": "2026-06-20T08:12:00Z",
                },
            ],
        })

        self.assertEqual(PROV_NAMESPACE, document["@context"]["prov"])
        self.assertEqual(1.1, document["@context"]["@version"])
        self.assertEqual("prov:Bundle", document["@type"])
        json.loads(json.dumps(document))

        graph = document["@graph"]
        activities = [
            node for node in graph
            if "prov:Activity" in node.get("@type", [])
        ]
        self.assertEqual(3, len(activities))
        self.assertTrue(all(activity.get("used") for activity in activities))
        self.assertTrue(all(activity.get("wasAssociatedWith") for activity in activities))
        self.assertEqual("session-1", activities[1]["sessionId"])
        self.assertEqual("undo", activities[2]["historyDirection"])
        self.assertEqual("fnv1a-state2", activities[2]["sourceSnapshotFingerprint"])
        self.assertEqual("fnv1a-state1", activities[2]["targetSnapshotFingerprint"])

        query_entities = [
            node for node in graph
            if "inlumen:UserQuery" in node.get("@type", [])
        ]
        self.assertEqual(1, len(query_entities))
        self.assertEqual("Add a report step", query_entities[0]["value"])
        self.assertIn(query_entities[0]["@id"], activities[1]["used"])

        version_entities = [
            node for node in graph
            if "inlumen:PipelineVersion" in node.get("@type", [])
        ]
        self.assertEqual(1, len(version_entities))
        self.assertEqual(activities[-1]["@id"], version_entities[0]["wasGeneratedBy"])
        self.assertIn("wasRevisionOf", version_entities[0])

    def test_filename_is_safe(self):
        self.assertEqual(
            "inlumen-provenance-release-1.jsonld",
            provenance_prov_o_filename("Release 1", "version-1"),
        )


if __name__ == "__main__":
    unittest.main()
