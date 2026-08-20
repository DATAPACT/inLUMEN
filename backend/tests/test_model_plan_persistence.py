import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import neo4j_api  # noqa: E402
from model_plans import (  # noqa: E402
    FASTER_WHISPER_PLAN,
    infer_implementation_plan_from_python_source,
    resolve_implementation_plan,
    unresolved_model_plan_errors_from_python_source,
)


class ModelPlanPersistenceTest(unittest.TestCase):
    def test_uploaded_faster_whisper_source_infers_its_literal_default_model(self):
        plan = infer_implementation_plan_from_python_source(
            """
from faster_whisper import WhisperModel

def run(input, params=None):
    model_size = str((params or {}).get("model_size", "small"))
    return WhisperModel(model_size)
"""
        )

        self.assertEqual("faster-whisper", plan["adapter_id"])
        self.assertEqual("Systran/faster-whisper-small", plan["model_id"])
        self.assertEqual(
            "536b0662742c02347bc0e980a01041f333bce120",
            plan["model_revision"],
        )
        self.assertEqual({}, plan["model_variants"])
        self.assertEqual(
            "inlumen-uploaded-python-inference",
            plan["resolution"]["source"],
        )

    def test_dynamic_or_unrecognized_model_source_is_not_guessed(self):
        self.assertEqual(
            {},
            infer_implementation_plan_from_python_source(
                "from faster_whisper import WhisperModel\n"
                "WhisperModel(os.environ['MODEL'])\n"
            ),
        )

    def test_uploaded_model_parameter_overrides_the_detected_default(self):
        plan = infer_implementation_plan_from_python_source(
            """
from faster_whisper import WhisperModel
model_size = params.get("model_size", "small")
WhisperModel(model_size)
""",
            parameters={"model_size": "medium"},
        )
        self.assertEqual("Systran/faster-whisper-medium", plan["model_id"])

    def test_uploaded_cli_model_default_is_inferred(self):
        plan = infer_implementation_plan_from_python_source(
            """
import argparse
import os
from faster_whisper import WhisperModel
parser = argparse.ArgumentParser()
parser.add_argument("--model", default=os.getenv("WHISPER_MODEL", "tiny"))
args = parser.parse_args()
WhisperModel(args.model)
"""
        )
        self.assertEqual("Systran/faster-whisper-tiny", plan["model_id"])
        self.assertEqual(
            "d90ca5fe260221311c53c58e660288d3deb8d356",
            plan["model_revision"],
        )

    def test_uploaded_transformers_source_requires_an_explicit_revision(self):
        plan = infer_implementation_plan_from_python_source(
            """
from transformers import AutoModel
AutoModel.from_pretrained("owner/model", revision="0123456789abcdef")
"""
        )
        self.assertEqual("huggingface-transformers", plan["adapter_id"])
        self.assertEqual("owner/model", plan["model_id"])
        self.assertEqual("0123456789abcdef", plan["model_revision"])
        self.assertEqual(
            {},
            infer_implementation_plan_from_python_source(
                "from transformers import AutoModel\n"
                "AutoModel.from_pretrained('owner/model')\n"
            ),
        )

    def test_uploaded_cli_transformers_pipeline_infers_reviewed_model_default(self):
        plan = infer_implementation_plan_from_python_source(
            """
import argparse
import os
from transformers import pipeline

parser = argparse.ArgumentParser()
parser.add_argument(
    "--model",
    default=os.getenv(
        "SENTIMENT_MODEL",
        "distilbert/distilbert-base-uncased-finetuned-sst-2-english",
    ),
)
args = parser.parse_args()
pipeline("sentiment-analysis", model=args.model, tokenizer=args.model)
"""
        )
        self.assertEqual("transformers-sst2-sentiment", plan["adapter_id"])
        self.assertEqual(
            "distilbert/distilbert-base-uncased-finetuned-sst-2-english",
            plan["model_id"],
        )
        self.assertEqual(
            "714eb0fa89d2f80546fda750413ed43d93601a13",
            plan["model_revision"],
        )

    def test_unpinned_uploaded_model_is_detected_for_a_runtime_warning(self):
        warnings = unresolved_model_plan_errors_from_python_source(
            """
from transformers import pipeline
pipeline("sentiment-analysis", model="unreviewed/example-model")
"""
        )
        self.assertEqual(1, len(warnings))
        self.assertIn("no reviewed, pinned local model plan", warnings[0])

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
