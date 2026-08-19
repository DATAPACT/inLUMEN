import ast
import asyncio
import base64
import hashlib
import json
import runpy
import sys
import types
from typing import ClassVar

from fastapi.testclient import TestClient

from app.generator import (
    build_pipeline_generation_plan,
    deterministic_pipeline_payload,
    evaluate_pipeline_draft,
    expected_outputs_for_node,
    fallback_script_payload,
    generate_pipeline_script_bundles,
    normalize_pipeline_payload,
    normalize_requirements,
)
from app.main import app
from app.llm import NODE_SYSTEM_PROMPT
from app.multimodal import build_multimodal_user_content
from app.pipeline_compiler import (
    compile_pipeline_nodes,
    compose_pipeline_program,
    function_name_for_flow_id,
    isolate_pipeline_nodes,
    validate_pipeline_source,
)
from app.schemas import (
    ExpectedArtifact,
    FileDescriptor,
    FileSample,
    GeneratePipelineScriptsRequest,
    NodeDescriptor,
    ValidationReport,
)
from app.task_profiles import (
    classify_node_task,
    source_semantic_errors,
    task_profile_payload,
)
from app.trusted_adapters import (
    apply_trusted_adapters,
    faster_whisper_function_source,
    input_boundary_function_source,
    roberta_sentiment_function_source,
)


def pipeline_payload() -> dict:
    return {
        "context": {
            "pipeline": {"name": "Patient media"},
            "graph": {
                "nodes": [
                    {
                        "flow_id": "ingest",
                        "label": "Image Ingestion",
                        "description": "Load an image.",
                        "type": "input",
                        "files": [
                            {
                                "filename": "scan.png",
                                "kind": "image",
                                "format": "png",
                            }
                        ],
                    },
                    {
                        "flow_id": "resize",
                        "label": "Resize Image",
                        "description": "Resize the input image.",
                        "type": "action",
                    },
                ],
                "edges": [{"source": "ingest", "target": "resize"}],
            },
        }
    }


def file_content(node: dict, filename: str) -> str:
    return next(
        item["content"]
        for item in node["generated_artifact"]["files"]
        if item["filename"] == filename
    )


def test_node_prompt_requires_filesystem_workspace_contract() -> None:
    assert "PIPELINE_INPUT_DIR" in NODE_SYSTEM_PROMPT
    assert "PIPELINE_OUTPUT_DIR" in NODE_SYSTEM_PROMPT
    assert "PIPELINE_INPUT_DIR (recursively, including port subdirectories)" in NODE_SYSTEM_PROMPT
    assert "Files written there are" in NODE_SYSTEM_PROMPT


def test_targeted_generation_reuses_validated_packages() -> None:
    initial_payload = pipeline_payload()
    initial_payload["options"] = {
        "validation_mode": "static",
        "allow_deterministic_fallback": True,
    }
    initial = asyncio.run(
        generate_pipeline_script_bundles(
            GeneratePipelineScriptsRequest.model_validate(initial_payload)
        )
    )
    reusable = next(
        node for node in initial.nodes if node.flow_id == "ingest"
    )
    targeted_payload = pipeline_payload()
    targeted_payload["options"] = {
        "validation_mode": "static",
        "allow_deterministic_fallback": True,
        "generation_strategy": "node_first",
        "target_flow_ids": ["resize"],
    }
    targeted_payload["reusable_nodes"] = [reusable.model_dump(mode="json")]

    targeted = asyncio.run(
        generate_pipeline_script_bundles(
            GeneratePipelineScriptsRequest.model_validate(targeted_payload)
        )
    )

    steps = {step.flow_id: step for step in targeted.generation_run.steps}
    assert steps["ingest"].attempts == 0
    assert steps["ingest"].stage == "reused_validated_bundle"
    assert steps["ingest"].status == "valid"
    assert steps["resize"].attempts == 1
    assert steps["resize"].status == "valid"


def test_targeted_generation_forwards_llm_config(monkeypatch) -> None:
    initial_payload = pipeline_payload()
    initial_payload["options"] = {
        "validation_mode": "static",
        "allow_deterministic_fallback": True,
    }
    initial = asyncio.run(
        generate_pipeline_script_bundles(
            GeneratePipelineScriptsRequest.model_validate(initial_payload)
        )
    )
    reusable = next(node for node in initial.nodes if node.flow_id == "ingest")
    targeted_payload = pipeline_payload()
    targeted_payload["llm_config"] = {
        "provider": "test",
        "model": "test-code-model",
        "base_url": "https://example.invalid/v1",
        "api_key": "test-key",
    }
    targeted_payload["options"] = {
        "validation_mode": "static",
        "generation_strategy": "node_first",
        "target_flow_ids": ["resize"],
    }
    targeted_payload["reusable_nodes"] = [reusable.model_dump(mode="json")]
    seen_models: list[str] = []

    async def generate_from_config(config, request, _usage_callback=None):
        seen_models.append(config.model)
        return fallback_script_payload(request)

    monkeypatch.setattr("app.generator.generate_node_payload", generate_from_config)

    targeted = asyncio.run(
        generate_pipeline_script_bundles(
            GeneratePipelineScriptsRequest.model_validate(targeted_payload)
        )
    )

    assert targeted.generation_run.status == "valid"
    assert seen_models == ["test-code-model"]


def reviewed_audio_pipeline_payload() -> dict:
    return {
        "context": {
            "pipeline": {"name": "Reviewed audio analysis"},
            "design": {
                "contracts": {"status": "approved"},
                "pipeline_summary": {"primary_goal": "Transcribe real audio"},
            },
            "graph": {
                "nodes": [
                    {
                        "flow_id": "audio",
                        "label": "Audio Input",
                        "type": "input",
                        "files": [
                            {
                                "filename": "conversation.wav",
                                "kind": "binary",
                                "format": "wav",
                            }
                        ],
                    },
                    {
                        "flow_id": "asr",
                        "label": "Speech-to-Text Transcription",
                        "description": "Transcribe with reviewed Whisper large-v3.",
                        "type": "action",
                        "parameters": {
                            "model_plan": {
                                "framework": "transformers",
                                "model_id": "openai/whisper-large-v3",
                                "model_revision": "reviewed-revision",
                                "required_packages": [
                                    "transformers>=4.39.0",
                                    "torch>=2.1.0",
                                    "librosa>=0.10.0",
                                    "accelerate>=0.26.0",
                                ],
                            }
                        },
                    },
                ],
                "edges": [{"source": "audio", "target": "asr"}],
            },
        },
        "options": {"validation_mode": "static"},
    }


def transcript_sentiment_pipeline_payload() -> dict:
    stale_asr_plan = {
        "schema_version": "inlumen.implementation-plan@1",
        "execution_profile": "trusted_heavy_model",
        "adapter_id": "faster-whisper",
        "model_id": "Systran/faster-whisper-large-v3",
        "model_revision": "stale-revision",
    }
    return {
        "context": {
            "pipeline": {"name": "Audio transcript sentiment"},
            "graph": {
                "nodes": [
                    {
                        "flow_id": "audio",
                        "label": "Audio Recording Input",
                        "type": "input",
                        "files": [
                            {
                                "filename": "recording.wav",
                                "kind": "binary",
                                "format": "wav",
                            }
                        ],
                    },
                    {
                        "flow_id": "preprocess",
                        "label": "Audio Preprocessing",
                        "type": "action",
                    },
                    {
                        "flow_id": "asr",
                        "label": "Speech-to-Text Transcription",
                        "type": "action",
                    },
                    {
                        "flow_id": "sentiment",
                        "label": "Transcript Sentiment Analysis",
                        "type": "action",
                        # Reproduce a plan persisted by the old broad
                        # `transcri...` heuristic. Codegen must correct it.
                        "implementation": stale_asr_plan,
                    },
                    {
                        "flow_id": "report",
                        "label": "Transcription Sentiment Report",
                        "type": "output",
                        "implementation": stale_asr_plan,
                    },
                ],
                "edges": [
                    {"source": "audio", "target": "preprocess"},
                    {"source": "preprocess", "target": "asr"},
                    {"source": "asr", "target": "sentiment"},
                    {"source": "sentiment", "target": "report"},
                ],
            },
        },
        "options": {"validation_mode": "static"},
    }


def remote_patient_training_payload() -> dict:
    return {
        "context": {
            "pipeline": {"name": "Remote Patient Monitoring"},
            "graph": {
                "nodes": [
                    {
                        "flow_id": "1",
                        "label": "Ingestion",
                        "type": "input",
                        "files": [
                            {
                                "filename": "patient_vitals.csv",
                                "kind": "table",
                                "format": "csv",
                                "columns": [
                                    "heart_rate",
                                    "spo2",
                                    "temperature",
                                    "deteriorated",
                                ],
                            }
                        ],
                    },
                    {
                        "flow_id": "2",
                        "label": "Preprocessing",
                        "description": "Clean and scale patient vital signs.",
                        "type": "action",
                    },
                    {
                        "flow_id": "3",
                        "label": "Model Training",
                        "description": (
                            "Trains a clinical deterioration prediction model on "
                            "preprocessed patient vitals."
                        ),
                        "type": "action",
                        "parameters": {
                            "model_plan": {
                                "task": "clinical-deterioration-prediction",
                                "domain": "healthcare-clinical-time-series",
                                "framework": "pytorch",
                                "model_id": (
                                    "microsoft/biomednlp-pubmedbert-ts-clinical"
                                ),
                                "model_revision": "main",
                                "required_packages": [
                                    "torch>=2",
                                    "transformers>=4",
                                ],
                            }
                        },
                    },
                    {
                        "flow_id": "4",
                        "label": "Alerting",
                        "description": "Create alerts from measured risk scores.",
                        "type": "output",
                    },
                ],
                "edges": [
                    {"source": "1", "target": "2"},
                    {"source": "2", "target": "3"},
                    {"source": "3", "target": "4"},
                ],
            },
        },
        "options": {"validation_mode": "static"},
    }


def test_tabular_training_routes_to_classical_ml_without_heavy_model() -> None:
    request = GeneratePipelineScriptsRequest.model_validate(
        remote_patient_training_payload()
    )
    plan, contexts = build_pipeline_generation_plan(request)
    training = plan["nodes"][2]
    implementation = training["implementation_plan"]

    assert training["task_profile"]["name"] == "model_training"
    assert implementation["execution_profile"] == "classical_ml"
    assert implementation["framework"] == "scikit-learn"
    assert "model_id" not in implementation
    assert not any(
        requirement.startswith(("torch", "transformers", "huggingface"))
        for requirement in plan["required_packages"]
    )
    assert "scikit-learn>=1.4,<2" in plan["required_packages"]
    assert any(
        requirement.startswith("scikit-learn")
        for requirement in contexts["3"].runtime_constraints.allowed_packages
    )


def test_deterministic_remote_patient_pipeline_contains_real_estimator_fit() -> None:
    response = TestClient(app).post(
        "/v1/generate/pipeline-scripts",
        json=remote_patient_training_payload(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["integration_validation"]["status"] == "valid"
    training_source = file_content(body["nodes"][2], "main.py")
    assert "RandomForestClassifier" in training_source
    assert ".fit(" in training_source


def test_classical_ml_validation_requires_fit_but_not_replaced_model_id() -> None:
    request = GeneratePipelineScriptsRequest.model_validate(
        remote_patient_training_payload()
    )
    plan, _ = build_pipeline_generation_plan(request)
    node = request.context.graph.nodes[2]
    node_plan = plan["nodes"][2]
    source = """
from pathlib import Path
from sklearn.ensemble import RandomForestClassifier

def node_n_3(inputs, output_dir, context):
    estimator = RandomForestClassifier(random_state=42)
    estimator.fit([[70, 98.0]], [0])
    output_path = Path(output_dir) / 'model.joblib'
    output_path.write_bytes(b'fitted-model')
    return [{'name': 'model_training', 'filename': 'model.joblib', 'path': str(output_path), 'kind': 'model', 'format': 'joblib'}]
"""

    errors = source_semantic_errors(
        node,
        [FileDescriptor.model_validate(item) for item in node_plan["inputs"]],
        source,
        function_name=node_plan["function_name"],
    )

    assert not any("reviewed model_id" in error for error in errors)
    assert not any("does not fit" in error for error in errors)


def test_generated_nodes_cannot_download_models_during_runtime() -> None:
    node = NodeDescriptor(
        flow_id="custom-model",
        label="Custom Model Inference",
        type="action",
    )
    source = """
from huggingface_hub import snapshot_download
from transformers import AutoModel

def node_custom_model(inputs, output_dir, context):
    snapshot = snapshot_download('owner/model')
    model = AutoModel.from_pretrained(snapshot)
    return []
"""

    errors = source_semantic_errors(node, [], source)

    assert any("imports huggingface_hub at runtime" in error for error in errors)
    assert any("downloads model artifacts during runtime" in error for error in errors)
    assert any("local_files_only=True" in error for error in errors)


def test_generated_nodes_can_load_verified_local_model_paths() -> None:
    node = NodeDescriptor(
        flow_id="local-model",
        label="Custom Model Inference",
        type="action",
    )
    source = """
from transformers import AutoModel

def node_local_model(inputs, output_dir, context):
    snapshot_path = context['verified_model_path']
    model = AutoModel.from_pretrained(snapshot_path, local_files_only=True)
    return []
"""

    errors = source_semantic_errors(node, [], source)

    assert not any("model" in error.lower() for error in errors)


def test_structured_risk_prediction_routes_to_classical_ml_without_training_label() -> None:
    node = NodeDescriptor(
        flow_id="risk",
        label="Clinical Risk Prediction",
        description="Predict deterioration risk from the supplied vital-sign rows.",
        type="action",
        parameters={
            "model_plan": {
                "framework": "pytorch",
                "model_id": "unverified/clinical-transformer",
            }
        },
    )
    inputs = [FileDescriptor(filename="vitals.csv", kind="table", format="csv")]

    assert classify_node_task(node, inputs) == "model_training"
    profile = task_profile_payload(node, inputs)
    assert profile["selected_implementation_plan"]["execution_profile"] == (
        "classical_ml"
    )
    assert "model_id" not in profile["selected_implementation_plan"]


def test_subpipeline_profile_requires_nested_graph_and_interface_execution() -> None:
    node = NodeDescriptor(
        flow_id="conversation",
        label="Conversation Understanding",
        type="subpipeline",
        subpipeline={
            "reference": {"pipeline_uid": "reusable-1", "version_uid": "version-1"},
            "interface": {"inputs": [{"id": "audio"}], "outputs": [{"id": "analysis"}]},
            "resolved_graph": {"nodes": [{"id": "nested-source"}], "edges": []},
        },
    )

    assert classify_node_task(node, []) == "subpipeline"
    profile = task_profile_payload(node, [])
    assert profile["name"] == "subpipeline"
    assert any("pinned reusable pipeline graph" in rule for rule in profile["implementation_rules"])
    assert any("public interface" in rule for rule in profile["implementation_rules"])


def test_model_training_contract_includes_row_level_predictions() -> None:
    outputs = expected_outputs_for_node(
        NodeDescriptor(
            flow_id="train",
            label="Patient Health Model Training",
            description="Train a deterioration classifier on preprocessed vitals data.",
            type="task",
        ),
        ["alert"],
        [
            FileDescriptor(
                filename="vitals.csv",
                kind="table",
                format="csv",
                columns=["timestamp", "patient_id", "deterioration_risk"],
            )
        ],
    )

    predictions = next(
        output for output in outputs if output.semantic_role == "model_predictions"
    )
    assert predictions.kind == "table"
    assert predictions.required_columns == [
        "timestamp",
        "patient_id",
        "deterioration_risk",
        "prediction",
        "prediction_score",
    ]


def test_reviewed_model_dependencies_are_compiler_owned() -> None:
    request = GeneratePipelineScriptsRequest.model_validate(
        reviewed_audio_pipeline_payload()
    )
    plan, contexts = build_pipeline_generation_plan(request)

    assert plan["design"]["contracts"]["status"] == "approved"
    assert plan["nodes"][1]["implementation_plan"]["schema_version"] == (
        "inlumen.implementation-plan@1"
    )
    assert plan["nodes"][1]["implementation_plan"]["adapter_id"] == "faster-whisper"
    assert plan["nodes"][1]["implementation_plan"]["execution_profile"] == (
        "trusted_heavy_model"
    )
    assert plan["nodes"][1]["implementation_plan"]["model_id"] == (
        "Systran/faster-whisper-large-v3"
    )
    assert plan["nodes"][1]["implementation_plan"]["model_revision"] == (
        "edaa852ec7e145841d8ffdb056a99866b5f0a478"
    )
    assert plan["nodes"][1]["implementation_plan"]["artifact_policy"] == {
        "schema_version": "inlumen.model-artifact-policy@1",
        "acquisition": "deployment-preflight",
        "source": "huggingface",
        "runtime_access": "verified-local-only",
        "model_root_env": "INLUMEN_MODEL_ROOT",
        "integrity": "sha256-tree",
        "runtime_network": "disabled",
    }
    assert plan["required_packages"] == [
        "faster-whisper==1.2.1",
        "ctranslate2==4.8.1",
        "huggingface-hub==1.25.1",
    ]
    assert (
        contexts["asr"].runtime_constraints.allowed_packages[-3:]
        == (plan["required_packages"])
    )


def test_sentiment_nodes_resolve_to_pinned_roberta_adapter() -> None:
    node = NodeDescriptor(
        flow_id="sentiment",
        label="Sentiment Analysis",
        description="Classify the complete transcript.",
        type="action",
        parameters={
            "model_plan": {
                "model_id": "unreviewed/example-model",
                "model_revision": "unverified",
            }
        },
    )
    request_payload = reviewed_audio_pipeline_payload()
    request_payload["context"]["graph"]["nodes"].append(node.model_dump(mode="json"))
    request_payload["context"]["graph"]["edges"].append(
        {"source": "asr", "target": "sentiment"}
    )
    request = GeneratePipelineScriptsRequest.model_validate(request_payload)

    plan, _ = build_pipeline_generation_plan(request)
    sentiment_plan = plan["nodes"][2]["implementation_plan"]

    assert sentiment_plan["adapter_id"] == "transformers-roberta-sentiment"
    assert sentiment_plan["execution_profile"] == "trusted_heavy_model"
    assert sentiment_plan["model_id"] == (
        "cardiffnlp/twitter-roberta-base-sentiment-latest"
    )
    assert sentiment_plan["model_revision"] == (
        "3216a57f2a0d9c45a2e6c20157c20c49fb4bf9c7"
    )


def test_transcript_consumers_route_by_node_responsibility() -> None:
    request = GeneratePipelineScriptsRequest.model_validate(
        transcript_sentiment_pipeline_payload()
    )

    plan, _ = build_pipeline_generation_plan(request)
    nodes = {node["flow_id"]: node for node in plan["nodes"]}

    assert nodes["preprocess"]["task_profile"]["name"] == "audio_preprocessing"
    assert nodes["asr"]["task_profile"]["name"] == "speech_to_text"
    assert nodes["asr"]["implementation_plan"]["adapter_id"] == "faster-whisper"
    assert nodes["sentiment"]["task_profile"]["name"] == "sentiment"
    assert nodes["sentiment"]["implementation_plan"]["adapter_id"] == (
        "transformers-roberta-sentiment"
    )
    assert nodes["report"]["task_profile"]["name"] == "report"
    assert nodes["report"]["implementation_plan"] == {}
    assert nodes["sentiment"]["outputs"][0]["semantic_role"] == "sentiment"
    assert nodes["report"]["outputs"] == []

    normalized = normalize_pipeline_payload(deterministic_pipeline_payload(plan), plan)
    compiled = {
        item.flow_id: item.source
        for item in compile_pipeline_nodes(
            normalized["pipeline_py"],
            {node["flow_id"]: node["function_name"] for node in plan["nodes"]},
        )
    }
    assert "WhisperModel" in compiled["asr"]
    assert "AutoTokenizer" in compiled["sentiment"]
    assert "WhisperModel" not in compiled["sentiment"]
    assert "WhisperModel" not in compiled["report"]
    assert "AutoTokenizer" not in compiled["report"]
    assert "delivery-receipt.json" in compiled["report"]


def test_trusted_sentiment_adapter_emits_declared_canonical_contract() -> None:
    sentiment_node = NodeDescriptor(
        flow_id="sentiment",
        label="Sentiment Analysis",
        description="Classify the complete transcript.",
        type="action",
    )
    request_payload = reviewed_audio_pipeline_payload()
    request_payload["context"]["graph"]["nodes"].append(
        sentiment_node.model_dump(mode="json")
    )
    request_payload["context"]["graph"]["edges"].append(
        {"source": "asr", "target": "sentiment"}
    )
    request = GeneratePipelineScriptsRequest.model_validate(request_payload)
    plan, _ = build_pipeline_generation_plan(request)
    node_plan = plan["nodes"][2]
    source = roberta_sentiment_function_source(node_plan)
    assert "snapshot_download" not in source
    assert "huggingface_hub" not in source
    assert "resolve_local_model" in source
    assert "model_tree_sha256" in source
    expected_outputs = [
        ExpectedArtifact.model_validate(item) for item in node_plan["outputs"]
    ]

    errors = source_semantic_errors(
        sentiment_node,
        [FileDescriptor.model_validate(item) for item in node_plan["inputs"]],
        source,
        expected_outputs=expected_outputs,
        function_name=node_plan["function_name"],
    )

    assert errors == []
    tree = ast.parse(source)
    result_dict = next(
        item.value
        for item in ast.walk(tree)
        if isinstance(item, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "result"
            for target in item.targets
        )
        and isinstance(item.value, ast.Dict)
    )
    result_keys = {
        key.value
        for key in result_dict.keys
        if isinstance(key, ast.Constant) and isinstance(key.value, str)
    }
    assert {
        "transcript",
        "sentiment_label",
        "confidence",
        "sentiment_scores",
        "processing_metadata",
    } <= result_keys


def test_static_contract_check_rejects_deferred_json_payload_mismatch() -> None:
    node = NodeDescriptor(
        flow_id="report",
        label="Report Assembly",
        description="Assemble the final report.",
        type="action",
    )
    source = (
        "import json\n"
        "from pathlib import Path\n\n"
        "def node_report(inputs, output_dir, context):\n"
        "    result = {'label': 'positive', 'score': 0.9}\n"
        "    path = Path(output_dir) / 'report.json'\n"
        "    path.write_text(json.dumps(result), encoding='utf-8')\n"
        "    return []\n"
    )
    expected = ExpectedArtifact(
        name="report",
        filename="report.json",
        kind="json",
        format="json",
        schema={
            "type": "object",
            "required": ["transcription", "sentiment_label", "confidence_score"],
        },
    )

    errors = source_semantic_errors(
        node,
        [],
        source,
        expected_outputs=[expected],
        function_name="node_report",
    )

    assert errors == [
        (
            "Node report writes JSON output 'report' without declared required "
            "fields: confidence_score, sentiment_label, transcription."
        )
    ]


def test_static_contract_check_ignores_runtime_manifest_wrapper_json() -> None:
    node = NodeDescriptor(
        flow_id="report",
        label="Report Assembly",
        description="Assemble the final report.",
        type="action",
    )
    source = (
        "import json\n\n"
        "def _materialize(inputs, output_dir, specs, context):\n"
        "    return []\n\n"
        "def node_report(inputs, output_dir, context):\n"
        "    return _materialize(inputs, output_dir, [], context)\n\n"
        "def _inlumen_node_main():\n"
        "    manifest = {'schema_version': 'inlumen.output-manifest@1', "
        "'outputs': []}\n"
        "    return json.dumps(manifest)\n"
    )
    expected = ExpectedArtifact(
        name="report",
        filename="report.json",
        kind="json",
        format="json",
        schema={
            "type": "object",
            "required": ["transcription", "sentiment_label", "confidence_score"],
        },
    )

    errors = source_semantic_errors(
        node,
        [],
        source,
        expected_outputs=[expected],
        function_name="node_report",
    )

    assert errors == []


def test_trusted_faster_whisper_adapter_emits_confidence_metrics(
    tmp_path,
    monkeypatch,
) -> None:
    calls = {}

    class FakeWord:
        start = 0.0
        end = 0.4
        word = " hello"
        probability = 0.91

    class FakeSegment:
        id = 0
        start = 0.0
        end = 0.4
        text = "Hello"
        avg_logprob = -0.2
        no_speech_prob = 0.05
        words: ClassVar[list[FakeWord]] = [FakeWord()]

    class FakeInfo:
        language = "en"
        language_probability = 0.94
        duration = 0.5
        duration_after_vad = 0.4

    class FakeWhisperModel:
        def __init__(
            self,
            path,
            *,
            device,
            compute_type,
            cpu_threads,
            num_workers,
        ):
            calls["model"] = (
                path,
                device,
                compute_type,
                cpu_threads,
                num_workers,
            )

        def transcribe(self, path, **kwargs):
            calls["transcribe"] = (path, kwargs)
            return iter([FakeSegment()]), FakeInfo()

    def install_verified_model(model_id: str, revision: str) -> tuple[str, str]:
        model_root = tmp_path / "models"
        spec_sha256 = hashlib.sha256(
            f"{model_id}@{revision}".encode()
        ).hexdigest()
        snapshot = model_root / "snapshots" / spec_sha256
        snapshot.mkdir(parents=True)
        (snapshot / "model.bin").write_bytes(b"reviewed-model")
        tree_sha256 = hashlib.sha256(b"reviewed-model-tree").hexdigest()
        artifact_dir = model_root / "artifacts" / spec_sha256
        artifact_dir.mkdir(parents=True)
        manifest_path = artifact_dir / "inlumen-model-manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "schema_version": "inlumen.model-artifact@1",
                    "model_id": model_id,
                    "model_revision": revision,
                    "spec_sha256": spec_sha256,
                    "snapshot_path": str(snapshot.relative_to(model_root)),
                    "tree_sha256": tree_sha256,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        (artifact_dir / "VERIFIED").write_text(
            hashlib.sha256(manifest_path.read_bytes()).hexdigest() + "\n",
            encoding="utf-8",
        )
        return str(snapshot), tree_sha256

    monkeypatch.setitem(
        sys.modules,
        "ctranslate2",
        types.SimpleNamespace(get_cuda_device_count=lambda: 0),
    )
    monkeypatch.setitem(
        sys.modules,
        "faster_whisper",
        types.SimpleNamespace(WhisperModel=FakeWhisperModel),
    )
    balanced_snapshot, balanced_tree = install_verified_model(
        "Systran/faster-whisper-medium",
        "08e178d48790749d25932bbc082711ddcfdfbc4f",
    )
    accuracy_snapshot, _ = install_verified_model(
        "Systran/faster-whisper-large-v3",
        "edaa852ec7e145841d8ffdb056a99866b5f0a478",
    )
    monkeypatch.setenv("INLUMEN_MODEL_ROOT", str(tmp_path / "models"))
    request = GeneratePipelineScriptsRequest.model_validate(
        reviewed_audio_pipeline_payload()
    )
    plan, _ = build_pipeline_generation_plan(request)
    node_plan = plan["nodes"][1]
    namespace = {}
    source = faster_whisper_function_source(node_plan)
    assert "snapshot_download" not in source
    assert "huggingface_hub" not in source
    assert "resolve_local_model" in source
    exec(source, namespace)  # noqa: S102
    audio = tmp_path / "conversation.wav"
    audio.write_bytes(b"RIFF-test")
    output_dir = tmp_path / "outputs"

    outputs = namespace[node_plan["function_name"]](
        [{"path": str(audio), "metadata": {"reference_transcript": "Hello"}}],
        str(output_dir),
        {},
    )

    result = json.loads((output_dir / outputs[0]["filename"]).read_text())
    assert result["text"] == "Hello"
    assert result["transcript"] == "Hello"
    assert result["processing_metadata"]["adapter_id"] == "faster-whisper"
    assert result["quality_gate"]["status"] == "pass"
    assert result["confidence_metrics"]["confidence_proxy"] > 0.0
    assert (
        result["confidence_metrics"]["reference_evaluation"]["word_error_rate"]
        == 0.0
    )
    assert calls["model"][0] == balanced_snapshot
    assert calls["model"][1:] == ("cpu", "int8", 2, 1)
    assert result["processing_metadata"]["profile"] == "balanced"
    assert result["processing_metadata"]["model_tree_sha256"] == balanced_tree
    assert calls["transcribe"][1]["vad_filter"] is True

    monkeypatch.setenv("INLUMEN_ASR_PROFILE", "accuracy")
    namespace[node_plan["function_name"]](
        [{"path": str(audio)}],
        str(output_dir),
        {},
    )
    assert calls["model"][0] == accuracy_snapshot


def test_task_labels_are_not_overridden_by_description_mentions() -> None:
    audio_input = [FileDescriptor(filename="audio.wav", kind="binary", format="wav")]

    assert (
        classify_node_task(
            NodeDescriptor(
                flow_id="2",
                label="Audio Preprocessing",
                description="Prepares audio for optimal transcription.",
                type="action",
            ),
            audio_input,
        )
        == "audio_preprocessing"
    )
    assert (
        classify_node_task(
            NodeDescriptor(
                flow_id="5",
                label="Report Generation",
                description="Consolidates transcription and sentiment output.",
                type="output",
            ),
            [],
        )
        == "report"
    )


def test_report_destination_has_no_generated_business_output_contract() -> None:
    outputs = expected_outputs_for_node(
        NodeDescriptor(
            flow_id="delivery",
            label="Structured Report Delivery",
            description="Deliver transcript, sentiment, confidence, and metadata.",
            type="destination",
        ),
        [],
        [
            FileDescriptor(
                filename="sentiment.json",
                kind="json",
                format="json",
            )
        ],
    )

    assert outputs == []


def test_document_pipeline_tasks_receive_semantic_runtime_contracts() -> None:
    pdf_input = [
        FileDescriptor(
            filename="operations_handbook.pdf",
            kind="binary",
            format="pdf",
        )
    ]
    chunks = expected_outputs_for_node(
        NodeDescriptor(
            flow_id="chunk",
            label="Text Chunking",
            description="Split the ingested PDF into overlapping chunks.",
            type="task",
        ),
        [],
        pdf_input,
    )
    embeddings = expected_outputs_for_node(
        NodeDescriptor(
            flow_id="embed",
            label="Generate Embeddings",
            description="Convert chunks with a pretrained model.",
            type="task",
        ),
        [],
        [FileDescriptor(filename="chunks.json", kind="json", format="json")],
    )
    answers = expected_outputs_for_node(
        NodeDescriptor(
            flow_id="answer",
            label="Question Answering",
            description="Retrieve context and answer a question.",
            type="task",
        ),
        [],
        [FileDescriptor(filename="index.json", kind="json", format="json")],
    )

    assert chunks[0].semantic_role == "document_chunks"
    assert chunks[0].schema["required"] == ["chunks"]
    assert embeddings[0].semantic_role == "embedding_records"
    assert embeddings[0].kind == "json"
    assert answers[0].semantic_role == "grounded_answer"
    assert answers[0].schema["required"] == ["answers"]
    assert answers[0].schema["properties"]["answers"]["items"]["required"] == [
        "question",
        "answer",
        "citations",
    ]
    answer_profile = task_profile_payload(
        NodeDescriptor(
            flow_id="answer",
            label="Question Answering",
            type="task",
        ),
        [FileDescriptor(filename="index.json", kind="json", format="json")],
    )
    assert any(
        "questions array carried inside an upstream index" in rule
        for rule in answer_profile["implementation_rules"]
    )

    chunk_profile = task_profile_payload(
        NodeDescriptor(
            flow_id="chunk",
            label="Text Chunking",
            type="task",
        ),
        pdf_input,
    )
    assert "pypdf>=5,<7" in chunk_profile["required_packages"]
    assert any(
        "pypdf.PdfReader" in rule
        for rule in chunk_profile["implementation_rules"]
    )


def test_pdf_chunking_rejects_raw_byte_decoding() -> None:
    node = NodeDescriptor(
        flow_id="chunk",
        label="Text Chunking",
        type="task",
    )
    inputs = [
        FileDescriptor(
            filename="operations_handbook.pdf",
            kind="binary",
            format="pdf",
        )
    ]
    unsafe_source = """
def node_chunk(inputs, output_dir, context):
    text = open(inputs[0]['path'], 'rb').read().decode('latin-1')
    return []
"""
    safe_source = """
from pypdf import PdfReader

def node_chunk(inputs, output_dir, context):
    reader = PdfReader(inputs[0]['path'])
    text = '\\n'.join(page.extract_text() or '' for page in reader.pages)
    return []
"""

    unsafe_errors = source_semantic_errors(node, inputs, unsafe_source)
    safe_errors = source_semantic_errors(node, inputs, safe_source)

    assert any("pypdf.PdfReader" in error for error in unsafe_errors)
    assert any("raw PDF bytes" in error for error in unsafe_errors)
    assert not safe_errors


def test_model_requirements_are_constrained_to_string_allowlist_entries() -> None:
    assert normalize_requirements(
        ["scikit-learn", -1, "invented-package", "pypdf>=6"],
        ["scikit-learn>=1.4,<2", "pypdf>=5,<7"],
    ) == ["scikit-learn>=1.4,<2", "pypdf>=5,<7"]


def test_audio_preprocessing_semantics_require_stable_polyphase_dsp() -> None:
    node = NodeDescriptor(
        flow_id="audio-clean",
        label="Audio Preprocessing",
        description="Resample and filter noisy audio before transcription.",
        type="action",
    )
    inputs = [FileDescriptor(filename="audio.wav", kind="binary", format="wav")]
    unsafe_source = (
        "import librosa\n"
        "from scipy import signal\n\n"
        "def node_audio_clean(inputs, output_dir, context):\n"
        "    target_sr = 16000\n"
        "    nyquist = target_sr / 2\n"
        "    samples = librosa.resample([], orig_sr=48000, target_sr=target_sr)\n"
        "    b, a = signal.butter(4, [80 / nyquist, 8000 / nyquist], btype='band')\n"
        "    signal.lfilter(b, a, samples)\n"
        "    return []\n"
    )

    errors = source_semantic_errors(node, inputs, unsafe_source)

    assert any("resample_poly" in error for error in errors)
    assert any("explicit fs" in error for error in errors)
    assert any("output='sos'" in error for error in errors)
    assert any("sosfilt" in error for error in errors)


def test_audio_preprocessing_semantics_accept_stable_polyphase_dsp() -> None:
    node = NodeDescriptor(
        flow_id="audio-clean",
        label="Audio Preprocessing",
        description="Resample and filter noisy audio before transcription.",
        type="action",
    )
    inputs = [FileDescriptor(filename="audio.wav", kind="binary", format="wav")]
    source = (
        "from scipy import signal\n\n"
        "def node_audio_clean(inputs, output_dir, context):\n"
        "    samples = signal.resample_poly([0.0, 0.1], 1, 3)\n"
        "    sos = signal.butter(4, 80, btype='highpass', fs=16000, output='sos')\n"
        "    signal.sosfilt(sos, samples)\n"
        "    return []\n"
    )

    assert source_semantic_errors(node, inputs, source) == []


def test_audio_preprocessing_rejects_ambiguous_fft_axis() -> None:
    node = NodeDescriptor(
        flow_id="audio-clean",
        label="Audio Preprocessing",
        description="Denoise and resample audio before transcription.",
        type="action",
    )
    inputs = [FileDescriptor(filename="audio.wav", kind="binary", format="wav")]
    source = (
        "import numpy as np\n"
        "from scipy import signal\n"
        "from scipy.fft import rfft, irfft\n\n"
        "def node_audio_clean(inputs, output_dir, context):\n"
        "    data = np.zeros((1000, 1))\n"
        "    spectrum = rfft(data)\n"
        "    restored = irfft(spectrum, n=len(data))\n"
        "    signal.resample_poly(restored, 1, 3, axis=0)\n"
        "    return []\n"
    )

    errors = source_semantic_errors(node, inputs, source)

    assert any("explicit sample axis" in error for error in errors)
    assert any("multi-terabyte array" in error for error in errors)


def test_audio_preprocessing_uses_shape_safe_trusted_adapter() -> None:
    node_plan = {
        "flow_id": "audio-clean",
        "function_name": "node_audio_clean",
        "descriptor": {
            "flow_id": "audio-clean",
            "label": "Audio Preprocessing",
            "description": (
                "Resample to 16kHz mono, apply noise reduction, normalize "
                "amplitude, and trim silence."
            ),
            "type": "action",
            "parameters": {},
        },
        "task_profile": {"name": "audio_preprocessing"},
        "implementation_plan": {},
        "outputs": [
            {
                "name": "prepared_audio",
                "filename": "prepared_audio.wav",
                "kind": "binary",
                "format": "wav",
            }
        ],
    }
    unsafe_source = (
        "from scipy.fft import rfft, irfft\n\n"
        "def node_audio_clean(inputs, output_dir, context):\n"
        "    data = [[0.0]]\n"
        "    return irfft(rfft(data), n=len(data))\n"
    )

    source = apply_trusted_adapters(unsafe_source, {"nodes": [node_plan]})

    ast.parse(source)
    assert "irfft" not in source
    assert 'always_2d=True' in source
    assert "np.mean(samples, axis=1" in source
    assert "signal.resample_poly(" in source
    assert "axis=0" in source
    assert "'target_sample_rate': 16000" in source
    assert "'denoise': True" in source
    assert "'normalize': True" in source
    assert "'trim_silence': True" in source


def test_pipeline_boundary_nodes_use_compiler_owned_adapters() -> None:
    nodes = [
        {
            "flow_id": "source",
            "function_name": "node_source",
            "descriptor": {
                "flow_id": "source",
                "label": "Uploaded CSV",
                "description": "Persisted source input.",
                "type": "source",
            },
            "task_profile": {"name": "generic"},
            "implementation_plan": {},
            "outputs": [
                {
                    "name": "raw_data",
                    "filename": "raw_data.csv",
                    "kind": "table",
                    "format": "csv",
                }
            ],
        },
        {
            "flow_id": "destination",
            "function_name": "node_destination",
            "descriptor": {
                "flow_id": "destination",
                "label": "Notification Sink",
                "description": "Managed notification destination.",
                "type": "destination",
            },
            "task_profile": {"name": "generic"},
            "implementation_plan": {},
            "outputs": [
                {
                    "name": "delivery_receipt",
                    "filename": "delivery_receipt.json",
                    "kind": "json",
                    "format": "json",
                    "schema": {
                        "type": "object",
                        "required": ["alerts"],
                        "properties": {"alerts": {"type": "array"}},
                    },
                }
            ],
        },
    ]
    unsafe_source = (
        "def node_source(inputs, output_dir, context):\n"
        "    open('hard-coded.csv', 'rb').read()\n"
        "    return []\n\n"
        "def node_destination(inputs, output_dir, context):\n"
        "    raise RuntimeError('send directly')\n"
    )

    source = apply_trusted_adapters(unsafe_source, {"nodes": nodes})

    ast.parse(source)
    assert "hard-coded.csv" not in source
    assert "item.get('path')" in source
    assert "'status': 'delivered'" in source
    assert "'alerts'" in source


def test_input_boundary_packages_pdf_and_questions_for_downstream_tasks(tmp_path) -> None:
    pdf_path = tmp_path / "knowledge.pdf"
    pdf_bytes = b"%PDF-1.4\nreal fixture\n"
    pdf_path.write_bytes(pdf_bytes)
    questions_path = tmp_path / "questions.json"
    questions_path.write_text(
        json.dumps({"questions": ["What is retained?"]}),
        encoding="utf-8",
    )
    node_plan = {
        "flow_id": "source",
        "function_name": "node_source",
        "outputs": [
            {
                "name": "source_package",
                "filename": "source_package.json",
                "kind": "json",
                "format": "json",
            }
        ],
    }
    namespace: dict[str, object] = {}
    exec(input_boundary_function_source(node_plan), namespace)

    outputs = namespace["node_source"](
        [
            {
                "path": str(pdf_path),
                "filename": pdf_path.name,
                "kind": "binary",
                "format": "pdf",
            },
            {
                "path": str(questions_path),
                "filename": questions_path.name,
                "kind": "json",
                "format": "json",
            },
        ],
        tmp_path / "outputs",
        {},
    )

    payload = json.loads((tmp_path / "outputs" / "source_package.json").read_text())
    assert base64.b64decode(payload["pdf_base64"]) == pdf_bytes
    assert payload["source"] == "knowledge.pdf"
    assert payload["questions"] == ["What is retained?"]
    assert outputs[0]["filename"] == "source_package.json"


def test_alert_task_semantics_override_stale_classical_ml_plan() -> None:
    node = NodeDescriptor(
        flow_id="alert",
        label="Patient Alert Evaluation",
        description="Create patient notifications from risk predictions.",
        type="task",
        implementation={"execution_profile": "classical_ml"},
    )

    assert classify_node_task(node, []) == "alerting"


def test_pipeline_compiler_injects_omitted_reviewed_dependencies() -> None:
    request = GeneratePipelineScriptsRequest.model_validate(
        reviewed_audio_pipeline_payload()
    )
    plan, _ = build_pipeline_generation_plan(request)
    output_specs = plan["nodes"][1]["outputs"]
    payload = {
        "pipeline_py": (
            "import json\n"
            "from pathlib import Path\n"
            "from transformers import pipeline\n\n"
            "def node_audio(inputs, output_dir, context):\n"
            "    return []\n\n"
            "def node_asr(inputs, output_dir, context):\n"
            "    recognizer = pipeline(\n"
            "        'automatic-speech-recognition',\n"
            "        model='openai/whisper-large-v3',\n"
            "        revision='reviewed-revision',\n"
            "    )\n"
            "    result = recognizer(inputs[0]['path'], return_timestamps='word')\n"
            "    path = Path(output_dir) / 'speech_to_text_transcription.json'\n"
            "    path.write_text(json.dumps(result), encoding='utf-8')\n"
            f"    return [{{**{output_specs[0]!r}, 'path': str(path)}}]\n"
        ),
        "requirements": [],
        "nodes": [
            {
                "flow_id": node["flow_id"],
                "function_name": node["function_name"],
            }
            for node in plan["nodes"]
        ],
    }
    payload = normalize_pipeline_payload(payload, plan)

    requirements, _, validation = asyncio.run(
        evaluate_pipeline_draft(request, plan, payload)
    )

    assert validation.status == "valid"
    assert requirements[:3] == plan["required_packages"]
    assert "from faster_whisper import WhisperModel" in payload["pipeline_py"]
    assert "from transformers import pipeline" not in payload["pipeline_py"]


def test_pipeline_first_fallback_compiles_independent_nodes() -> None:
    body = (
        TestClient(app)
        .post(
            "/v1/generate/pipeline-scripts",
            json=pipeline_payload(),
        )
        .json()
    )

    assert body["integration_validation"]["status"] == "valid"
    assert body["generation_run"]["mode"] == "pipeline_first_single_script"
    assert [item["flow_id"] for item in body["nodes"]] == ["ingest", "resize"]

    ingest_source = file_content(body["nodes"][0], "main.py")
    resize_source = file_content(body["nodes"][1], "main.py")
    assert "def node_ingest(" in ingest_source
    assert "def node_resize(" not in ingest_source
    assert "def node_resize(" in resize_source
    assert "def node_ingest(" not in resize_source


def test_compiled_node_executes_as_standalone_component(
    tmp_path,
    monkeypatch,
) -> None:
    body = (
        TestClient(app)
        .post(
            "/v1/generate/pipeline-scripts",
            json=pipeline_payload(),
        )
        .json()
    )
    source = file_content(body["nodes"][0], "main.py")
    input_dir = tmp_path / "inputs"
    output_dir = tmp_path / "outputs"
    input_dir.mkdir()
    output_dir.mkdir()
    image = input_dir / "scan.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"pipeline-first")
    manifest = input_dir / "input_manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "inputs": [
                    {
                        "filename": "scan.png",
                        "path": str(image),
                        "kind": "image",
                        "format": "png",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    context_path = tmp_path / "context.json"
    context_path.write_text("{}\n", encoding="utf-8")
    script = tmp_path / "main.py"
    script.write_text(source, encoding="utf-8")
    monkeypatch.setenv("INLUMEN_INPUT_MANIFEST", str(manifest))
    monkeypatch.setenv("INLUMEN_OUTPUT_DIR", str(output_dir))
    monkeypatch.setenv(
        "INLUMEN_OUTPUT_MANIFEST",
        str(output_dir / "output_manifest.json"),
    )
    monkeypatch.setenv("INLUMEN_CONTEXT_PATH", str(context_path))

    runpy.run_path(str(script), run_name="__main__")

    output_manifest = json.loads(
        (output_dir / "output_manifest.json").read_text(encoding="utf-8")
    )
    assert output_manifest["outputs"][0]["kind"] == "image"
    generated = output_dir / output_manifest["outputs"][0]["filename"]
    assert generated.read_bytes() == image.read_bytes()


def test_compiler_keeps_only_transitive_shared_dependencies() -> None:
    source = (
        "import json\n"
        "import math\n\n"
        "def json_helper(value):\n"
        "    return json.dumps(value)\n\n"
        "def math_helper(value):\n"
        "    return math.ceil(value)\n\n"
        "def node_left(inputs, output_dir, context):\n"
        "    return json_helper(inputs)\n\n"
        "def node_right(inputs, output_dir, context):\n"
        "    return math_helper(len(inputs))\n"
    )
    compiled = {
        item.flow_id: item.source
        for item in compile_pipeline_nodes(
            source,
            {"left": "node_left", "right": "node_right"},
        )
    }
    left_top_level_imports = {
        alias.name
        for statement in ast.parse(compiled["left"]).body
        if isinstance(statement, ast.Import)
        for alias in statement.names
    }
    right_top_level_imports = {
        alias.name
        for statement in ast.parse(compiled["right"]).body
        if isinstance(statement, ast.Import)
        for alias in statement.names
    }

    assert "json" in left_top_level_imports
    assert "def json_helper" in compiled["left"]
    assert "math" not in left_top_level_imports
    assert "def math_helper" not in compiled["left"]
    assert "math" in right_top_level_imports
    assert "def math_helper" in compiled["right"]
    assert "json" not in right_top_level_imports
    assert "def json_helper" not in compiled["right"]


def test_canonical_pipeline_program_executes_complete_graph(
    tmp_path,
    monkeypatch,
) -> None:
    request = GeneratePipelineScriptsRequest.model_validate(pipeline_payload())
    plan, _ = build_pipeline_generation_plan(request)
    draft = deterministic_pipeline_payload(plan)
    program = compose_pipeline_program(draft["pipeline_py"], plan)
    input_dir = tmp_path / "inputs"
    output_dir = tmp_path / "outputs"
    input_dir.mkdir()
    output_dir.mkdir()
    image = input_dir / "scan.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"complete-pipeline")
    manifest = input_dir / "input_manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "inputs": [
                    {
                        "filename": "scan.png",
                        "path": str(image),
                        "kind": "image",
                        "format": "png",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    context_path = tmp_path / "context.json"
    context_path.write_text("{}\n", encoding="utf-8")
    pipeline_script = tmp_path / "pipeline.py"
    pipeline_script.write_text(program, encoding="utf-8")
    monkeypatch.setenv("INLUMEN_INPUT_MANIFEST", str(manifest))
    monkeypatch.setenv("INLUMEN_OUTPUT_DIR", str(output_dir))
    monkeypatch.setenv(
        "INLUMEN_OUTPUT_MANIFEST",
        str(output_dir / "output_manifest.json"),
    )
    monkeypatch.setenv("INLUMEN_CONTEXT_PATH", str(context_path))

    runpy.run_path(str(pipeline_script), run_name="__main__")

    assert (output_dir / "nodes" / "ingest" / "output_manifest.json").is_file()
    assert (output_dir / "nodes" / "resize" / "output_manifest.json").is_file()
    final_manifest = json.loads(
        (output_dir / "output_manifest.json").read_text(encoding="utf-8")
    )
    assert final_manifest["outputs"][0]["kind"] == "image"
    assert (
        output_dir / "nodes" / "resize" / final_manifest["outputs"][0]["filename"]
    ).read_bytes() == image.read_bytes()


def test_canonical_runtime_stages_filename_compatibility_aliases(
    tmp_path,
    monkeypatch,
) -> None:
    plan = {
        "pipeline": {},
        "nodes": [
            {
                "flow_id": "source",
                "function_name": "node_source",
                "parents": [],
                "input_filenames": ["telemetry.csv"],
                "descriptor": {"type": "source"},
                "outputs": [
                    {
                        "name": "raw",
                        "filename": "raw.csv",
                        "kind": "table",
                        "format": "csv",
                    }
                ],
            }
        ],
        "edges": [],
    }
    source = (
        "from pathlib import Path\n\n"
        "def node_source(inputs, output_dir, context):\n"
        "    payload = Path('telemetry.csv').read_text(encoding='utf-8')\n"
        "    output = Path(output_dir) / 'raw.csv'\n"
        "    output.write_text(payload, encoding='utf-8')\n"
        "    return [{'name': 'raw', 'path': str(output)}]\n"
    )
    program = compose_pipeline_program(source, plan)
    input_dir = tmp_path / "inputs"
    output_dir = tmp_path / "outputs"
    input_dir.mkdir()
    output_dir.mkdir()
    telemetry = input_dir / "telemetry.csv"
    telemetry.write_text("patient_id,risk\nPT-1,1\n", encoding="utf-8")
    manifest = input_dir / "input_manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "inputs": [
                    {
                        "filename": "telemetry.csv",
                        "path": str(telemetry),
                        "kind": "table",
                        "format": "csv",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    context_path = tmp_path / "context.json"
    context_path.write_text("{}\n", encoding="utf-8")
    pipeline_script = tmp_path / "pipeline.py"
    pipeline_script.write_text(program, encoding="utf-8")
    monkeypatch.setenv("INLUMEN_INPUT_MANIFEST", str(manifest))
    monkeypatch.setenv("INLUMEN_OUTPUT_DIR", str(output_dir))
    monkeypatch.setenv(
        "INLUMEN_OUTPUT_MANIFEST",
        str(output_dir / "output_manifest.json"),
    )
    monkeypatch.setenv("INLUMEN_CONTEXT_PATH", str(context_path))

    runpy.run_path(str(pipeline_script), run_name="__main__")

    generated = output_dir / "nodes" / "source" / "raw.csv"
    assert generated.read_text(encoding="utf-8") == telemetry.read_text(
        encoding="utf-8"
    )
    output_manifest = json.loads(
        (output_dir / "output_manifest.json").read_text(encoding="utf-8")
    )
    assert output_manifest["outputs"][0]["filename"] == "raw.csv"
    assert output_manifest["outputs"][0]["kind"] == "table"
    assert output_manifest["outputs"][0]["format"] == "csv"


def test_compiled_node_runtime_enriches_declared_output_contract(
    tmp_path,
    monkeypatch,
) -> None:
    source = (
        "from pathlib import Path\n\n"
        "def node_task(inputs, output_dir, context):\n"
        "    output = Path(output_dir) / 'result.json'\n"
        "    output.write_text('{\"ok\": true}', encoding='utf-8')\n"
        "    return [{'name': 'result', 'path': str(output)}]\n"
    )
    compiled = compile_pipeline_nodes(source, {"task": "node_task"})[0].source
    input_dir = tmp_path / "inputs"
    output_dir = tmp_path / "outputs"
    input_dir.mkdir()
    output_dir.mkdir()
    input_manifest = input_dir / "input_manifest.json"
    input_manifest.write_text('{"inputs": []}\n', encoding="utf-8")
    node_manifest = tmp_path / "node-manifest.json"
    node_manifest.write_text(
        json.dumps(
            {
                "data_contract": {
                    "outputs": [
                        {
                            "name": "result",
                            "filename": "result.json",
                            "kind": "json",
                            "format": "json",
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    script = tmp_path / "main.py"
    script.write_text(compiled, encoding="utf-8")
    monkeypatch.setenv("INLUMEN_INPUT_MANIFEST", str(input_manifest))
    monkeypatch.setenv("INLUMEN_OUTPUT_DIR", str(output_dir))
    monkeypatch.setenv(
        "INLUMEN_OUTPUT_MANIFEST",
        str(output_dir / "output_manifest.json"),
    )
    monkeypatch.setenv("INLUMEN_CONTEXT_PATH", str(node_manifest))

    runpy.run_path(str(script), run_name="__main__")

    output_manifest = json.loads(
        (output_dir / "output_manifest.json").read_text(encoding="utf-8")
    )
    assert output_manifest["outputs"][0] == {
        "name": "result",
        "filename": "result.json",
        "kind": "json",
        "format": "json",
        "path": str(output_dir / "result.json"),
    }


def test_pipeline_sample_uses_one_whole_pipeline_sandbox(monkeypatch) -> None:
    calls = []

    def pipeline_sandbox(**kwargs):
        calls.append(kwargs)
        return ValidationReport(
            status="valid",
            checks=["whole_pipeline_sample_run"],
        )

    def node_sandbox(**_kwargs):
        raise AssertionError("per-node Docker validation must not run")

    monkeypatch.setattr(
        "app.generator.validate_pipeline_program_with_docker",
        pipeline_sandbox,
    )
    monkeypatch.setattr(
        "app.generator.validate_node_with_docker",
        node_sandbox,
    )
    payload = pipeline_payload()
    payload["options"] = {"validation_mode": "pipeline_sample"}

    response = TestClient(app).post(
        "/v1/generate/pipeline-scripts",
        json=payload,
    )

    assert response.status_code == 200
    assert response.json()["integration_validation"]["status"] == "valid"
    assert len(calls) == 1
    assert "def node_ingest(" in calls[0]["pipeline_source"]
    assert "def node_resize(" in calls[0]["pipeline_source"]


def test_reviewed_model_download_is_deferred_by_default(
    monkeypatch,
) -> None:
    dependency_calls = []
    pipeline_calls = []

    def dependency_sandbox(**kwargs):
        dependency_calls.append(kwargs)
        return ValidationReport(
            status="valid",
            checks=["reviewed_model_dependency_installation"],
        )

    def pipeline_sandbox(**kwargs):
        pipeline_calls.append(kwargs)
        return ValidationReport(
            status="valid",
            checks=["model_free_sample_run"],
        )

    monkeypatch.setattr(
        "app.generator.validate_pipeline_dependencies_with_docker",
        dependency_sandbox,
    )
    monkeypatch.setattr(
        "app.generator.validate_pipeline_program_with_docker",
        pipeline_sandbox,
    )
    payload = reviewed_audio_pipeline_payload()
    payload["context"]["runtime_constraints"] = {
        "network_allowed": True,
    }
    payload["options"] = {
        "validation_mode": "pipeline_sample",
    }
    response = TestClient(app).post(
        "/v1/generate/pipeline-scripts",
        json=payload,
    )

    assert response.status_code == 200
    assert response.json()["integration_validation"]["status"] == "valid"
    assert len(dependency_calls) == 1
    assert len(pipeline_calls) == 1
    assert [
        node["flow_id"] for node in pipeline_calls[0]["plan"]["nodes"]
    ] == ["audio"]
    assert "from transformers import" not in pipeline_calls[0]["pipeline_source"]
    assert "node_asr" not in pipeline_calls[0]["pipeline_source"]
    assert pipeline_calls[0]["requirements"] == []
    assert pipeline_calls[0]["network_allowed"] is False
    assert dependency_calls[0]["requirements"][:3] == [
        "faster-whisper==1.2.1",
        "ctranslate2==4.8.1",
        "huggingface-hub==1.25.1",
    ]


def test_pipeline_compiler_isolates_selected_nodes_and_dependencies() -> None:
    source = (
        "from pathlib import Path\n"
        "from transformers import pipeline\n\n"
        "def copy_input(inputs, output_dir):\n"
        "    return Path(inputs[0]['path'])\n\n"
        "def node_audio(inputs, output_dir, context):\n"
        "    copy_input(inputs, output_dir)\n"
        "    return []\n\n"
        "def node_asr(inputs, output_dir, context):\n"
        "    recognizer = pipeline('automatic-speech-recognition')\n"
        "    recognizer(inputs[0]['path'])\n"
        "    return []\n"
    )

    isolated = isolate_pipeline_nodes(
        source,
        {"audio": "node_audio", "asr": "node_asr"},
        {"audio"},
    )

    assert "from pathlib import Path" in isolated
    assert "def copy_input" in isolated
    assert "def node_audio" in isolated
    assert "transformers" not in isolated
    assert "node_asr" not in isolated


def test_compiler_rejects_top_level_execution_and_cross_node_calls() -> None:
    mapping = {"a": "node_a", "b": "node_b"}
    source = (
        "print('unsafe')\n\n"
        "def node_a(inputs, output_dir, context):\n"
        "    return node_b(inputs, output_dir, context)\n\n"
        "def node_b(inputs, output_dir, context):\n"
        "    return []\n"
    )

    report = validate_pipeline_source(source, mapping)

    assert report.status == "invalid"
    assert any("top-level" in error for error in report.errors)
    assert any("directly calls" in error for error in report.errors)


def test_compiler_preserves_shared_helpers_but_removes_other_nodes() -> None:
    source = (
        "def helper(value):\n"
        "    return value\n\n"
        "def node_a(inputs, output_dir, context):\n"
        "    return helper([])\n\n"
        "def node_b(inputs, output_dir, context):\n"
        "    return helper([])\n"
    )

    compiled = compile_pipeline_nodes(
        source,
        {"a": "node_a", "b": "node_b"},
    )

    assert function_name_for_flow_id("1-clean data") == "node_n_1_clean_data"
    assert "def helper(" in compiled[0].source
    assert "def node_a(" in compiled[0].source
    assert "def node_b(" not in compiled[0].source


def test_multimodal_prompt_contains_bounded_image_and_audio_parts() -> None:
    image_data = base64.b64encode(b"small-image").decode("ascii")
    audio_data = base64.b64encode(b"small-audio").decode("ascii")
    files = [
        FileDescriptor(
            filename="scan.png",
            kind="image",
            format="png",
            sample=FileSample(
                data_uri=f"data:image/png;base64,{image_data}",
                width=64,
                height=64,
            ),
        ),
        FileDescriptor(
            filename="speech.wav",
            kind="binary",
            format="wav",
            sample=FileSample(
                data_uri=f"data:audio/wav;base64,{audio_data}",
                duration_seconds=1.0,
            ),
        ),
    ]

    content = build_multimodal_user_content({"task": "inspect"}, files)

    assert isinstance(content, list)
    assert [part["type"] for part in content] == [
        "text",
        "image_url",
        "input_audio",
    ]
