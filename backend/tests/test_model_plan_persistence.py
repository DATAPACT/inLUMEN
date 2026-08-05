import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import neo4j_api  # noqa: E402
from model_plans import FASTER_WHISPER_PLAN, resolve_implementation_plan  # noqa: E402


class ModelPlanPersistenceTest(unittest.TestCase):
    def test_transcript_consumers_do_not_inherit_the_asr_adapter(self):
        sentiment = resolve_implementation_plan(
            FASTER_WHISPER_PLAN,
            label="Transcript Sentiment Analysis",
            description="Analyze the generated transcript.",
        )
        report = resolve_implementation_plan(
            FASTER_WHISPER_PLAN,
            label="Transcription Sentiment Report",
            description="Compile upstream transcription and sentiment outputs.",
        )

        self.assertEqual("transformers-roberta-sentiment", sentiment["adapter_id"])
        self.assertEqual(
            "cardiffnlp/twitter-roberta-base-sentiment-latest",
            sentiment["model_id"],
        )
        self.assertEqual({}, report)

    def test_tabular_model_training_replaces_unverified_neural_plan(self):
        plan = resolve_implementation_plan(
            {
                "task": "clinical-deterioration-prediction",
                "domain": "healthcare-clinical-time-series",
                "framework": "pytorch",
                "model_id": "microsoft/biomednlp-pubmedbert-ts-clinical",
                "model_revision": "main",
                "required_packages": ["torch", "transformers"],
            },
            label="Model Training",
            description=(
                "Trains a clinical deterioration prediction model on "
                "preprocessed patient vitals."
            ),
        )

        self.assertEqual("classical_ml", plan["execution_profile"])
        self.assertEqual("scikit-learn", plan["framework"])
        self.assertNotIn("model_id", plan)
        self.assertNotIn("torch", " ".join(plan["required_packages"]))
        self.assertEqual(
            "microsoft/biomednlp-pubmedbert-ts-clinical",
            plan["resolution"]["replaced_proposed_model_id"],
        )

    def test_explicit_deep_learning_training_remains_custom_and_advisory(self):
        plan = resolve_implementation_plan(
            {"framework": "pytorch", "model_id": "owner/explicit-model"},
            label="Model Training",
            description="Train a PyTorch transformer requested by the user.",
        )

        self.assertEqual(
            {"framework": "pytorch", "model_id": "owner/explicit-model"},
            plan,
        )

    def test_visible_action_node_serializes_model_plan(self):
        model_plan = {
            "framework": "transformers",
            "model_id": "cardiffnlp/twitter-roberta-base-sentiment-latest",
            "model_revision": "pinned-revision",
        }
        nodes, _ = neo4j_api._parse_visible_graph(
            {
                "nodes": [
                    {
                        "id": "4",
                        "position": {"x": 10, "y": 20},
                        "data": {
                            "type": "action",
                            "label": "Sentiment Analysis",
                            "param": {"model_plan": model_plan},
                        },
                    }
                ],
                "edges": [],
            }
        )

        self.assertEqual(
            "cardiffnlp/twitter-roberta-base-sentiment-latest",
            json.loads(nodes[0]["props"]["param_json"])["model_plan"]["model_id"],
        )
        self.assertEqual(
            "3216a57f2a0d9c45a2e6c20157c20c49fb4bf9c7",
            json.loads(nodes[0]["props"]["param_json"])["model_plan"][
                "model_revision"
            ],
        )

    def test_visible_action_node_recovers_plan_from_implementation(self):
        model_plan = {
            "framework": "transformers",
            "model_id": "openai/whisper-large-v3",
            "model_revision": "pinned-revision",
        }
        nodes, _ = neo4j_api._parse_visible_graph(
            {
                "nodes": [
                    {
                        "id": "3",
                        "data": {
                            "type": "action",
                            "label": "Speech-to-Text",
                            "implementation": model_plan,
                        },
                    }
                ],
                "edges": [],
            }
        )

        self.assertEqual(
            "Systran/faster-whisper-large-v3",
            json.loads(nodes[0]["props"]["param_json"])["model_plan"]["model_id"],
        )
        self.assertEqual(
            "edaa852ec7e145841d8ffdb056a99866b5f0a478",
            json.loads(nodes[0]["props"]["param_json"])["model_plan"][
                "model_revision"
            ],
        )
        self.assertEqual(
            "trusted_heavy_model",
            json.loads(nodes[0]["props"]["param_json"])["model_plan"][
                "execution_profile"
            ],
        )
        self.assertEqual(
            "verified-local-only",
            json.loads(nodes[0]["props"]["param_json"])["model_plan"][
                "artifact_policy"
            ]["runtime_access"],
        )


if __name__ == "__main__":
    unittest.main()
