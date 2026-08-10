import base64
import json

from app.sandbox import (
    persist_descriptors_for_handoff,
    sample_rows_for_descriptor,
    validate_output_shape,
    write_sample_inputs,
)
from app.schemas import ExpectedArtifact, FileDescriptor, FileSample


def test_write_sample_inputs_preserves_existing_inherited_file(tmp_path) -> None:
    inputs_dir = tmp_path / "inputs"
    inherited_dir = inputs_dir / "ingest"
    inherited_dir.mkdir(parents=True)
    inherited_file = inherited_dir / "raw_vitals.csv"
    inherited_file.write_text(
        "patient_id,abnormal_condition\np1,normal\n", encoding="utf-8"
    )

    write_sample_inputs(
        inputs_dir / "input_manifest.json",
        inputs_dir,
        [
            FileDescriptor(
                filename="ingest/raw_vitals.csv",
                kind="table",
                format="csv",
                columns=["patient_id", "abnormal_condition"],
                required_columns=["abnormal_condition"],
                semantic_role="raw_dataset",
                sample=FileSample(text=str(inherited_file)),
            )
        ],
    )

    assert inherited_file.read_text(encoding="utf-8") == (
        "patient_id,abnormal_condition\np1,normal\n"
    )
    manifest = json.loads(
        (inputs_dir / "input_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["inputs"][0]["required_columns"] == ["abnormal_condition"]
    assert manifest["inputs"][0]["semantic_role"] == "raw_dataset"


def test_write_sample_inputs_uses_embedded_binary_instead_of_fixture(
    tmp_path,
) -> None:
    inputs_dir = tmp_path / "inputs"
    inputs_dir.mkdir()
    content = b"RIFF-real-upload-WAVE"

    write_sample_inputs(
        inputs_dir / "input_manifest.json",
        inputs_dir,
        [
            FileDescriptor(
                filename="conversation.wav",
                kind="binary",
                format="wav",
                sample=FileSample(
                    data_uri=(
                        "data:audio/wav;base64,"
                        + base64.b64encode(content).decode("ascii")
                    )
                ),
            )
        ],
    )

    assert (inputs_dir / "conversation.wav").read_bytes() == content


def test_validate_output_shape_rejects_missing_required_columns(tmp_path) -> None:
    output_path = tmp_path / "preprocessing.csv"
    output_path.write_text("patient_id,heart_rate\np1,80\n", encoding="utf-8")

    errors = validate_output_shape(
        output_path,
        ExpectedArtifact(
            name="preprocessing",
            kind="table",
            format="csv",
            required_columns=["abnormal_condition"],
        ),
    )

    assert errors == [
        (
            "Table output preprocessing is missing required columns: abnormal_condition. "
            "Found columns: patient_id, heart_rate"
        )
    ]


def test_validate_output_shape_rejects_missing_json_required_keys(tmp_path) -> None:
    output_path = tmp_path / "metrics.json"
    output_path.write_text('{"accuracy": 0.9}\n', encoding="utf-8")

    errors = validate_output_shape(
        output_path,
        ExpectedArtifact(
            name="model_training_metrics",
            kind="json",
            format="json",
            schema={"type": "object", "required": ["metrics", "target_column"]},
        ),
    )

    assert errors == [
        "JSON output model_training_metrics is missing required keys: metrics, target_column"
    ]


def test_validate_output_shape_rejects_wrong_json_property_type(tmp_path) -> None:
    output_path = tmp_path / "metrics.json"
    output_path.write_text(
        '{"metrics": [], "target_column": "abnormal_condition"}\n',
        encoding="utf-8",
    )

    errors = validate_output_shape(
        output_path,
        ExpectedArtifact(
            name="model_training_metrics",
            kind="json",
            format="json",
            schema={
                "type": "object",
                "required": ["metrics", "target_column"],
                "properties": {
                    "metrics": {"type": "object"},
                    "target_column": {
                        "type": "string",
                        "enum": ["abnormal_condition"],
                    },
                },
            },
            semantic_role="model_metrics",
        ),
    )

    assert (
        "JSON output model_training_metrics key metrics must be object, got list."
        in errors
    )
    assert "JSON output model_training_metrics metrics must be an object." in errors


def test_validate_output_shape_rejects_wrong_json_enum(tmp_path) -> None:
    output_path = tmp_path / "metrics.json"
    output_path.write_text(
        '{"metrics": {}, "target_column": "heart_rate"}\n',
        encoding="utf-8",
    )

    errors = validate_output_shape(
        output_path,
        ExpectedArtifact(
            name="model_training_metrics",
            kind="json",
            format="json",
            schema={
                "type": "object",
                "required": ["metrics", "target_column"],
                "properties": {
                    "metrics": {"type": "object"},
                    "target_column": {
                        "type": "string",
                        "enum": ["abnormal_condition"],
                    },
                },
            },
            semantic_role="model_metrics",
        ),
    )

    assert errors == [
        (
            "JSON output model_training_metrics key target_column must be one of: "
            "abnormal_condition"
        )
    ]


def test_validate_output_shape_rejects_wrong_alerts_type(tmp_path) -> None:
    output_path = tmp_path / "alerts.json"
    output_path.write_text('{"alerts": {}}\n', encoding="utf-8")

    errors = validate_output_shape(
        output_path,
        ExpectedArtifact(
            name="alerting",
            kind="json",
            format="json",
            schema={
                "type": "object",
                "required": ["alerts"],
                "properties": {"alerts": {"type": "array"}},
            },
            semantic_role="alerts",
        ),
    )

    assert "JSON output alerting key alerts must be array, got dict." in errors
    assert "JSON output alerting alerts must be an array." in errors


def test_validate_output_shape_rejects_missing_nested_citation_source(tmp_path) -> None:
    output = tmp_path / "answer.json"
    output.write_text(
        '{"question":"q","answer":"a","citations":[{"page":1}]}',
        encoding="utf-8",
    )

    errors = validate_output_shape(
        output,
        ExpectedArtifact(
            name="answer",
            kind="json",
            format="json",
            schema={
                "type": "object",
                "required": ["question", "answer", "citations"],
                "properties": {
                    "citations": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["source", "page"],
                            "properties": {
                                "source": {"type": "string"},
                                "page": {"type": "integer"},
                            },
                        },
                    }
                },
            },
        ),
    )

    assert (
        "JSON output answer key citations[0] is missing required keys: source"
        in errors
    )


def test_persist_descriptors_for_handoff_copies_outputs(tmp_path) -> None:
    source = tmp_path / "node-output.csv"
    source.write_text("patient_id,abnormal_condition\np1,normal\n", encoding="utf-8")

    persisted = persist_descriptors_for_handoff(
        [
            FileDescriptor(
                filename="node-output.csv",
                kind="table",
                format="csv",
                columns=["patient_id", "abnormal_condition"],
                required_columns=["abnormal_condition"],
                sample=FileSample(text=str(source)),
            )
        ],
        tmp_path / "handoff",
    )

    assert len(persisted) == 1
    copied_path = persisted[0].sample.text
    assert copied_path is not None
    assert copied_path != str(source)
    assert copied_path.endswith("node-output.csv")
    assert "abnormal_condition" in persisted[0].required_columns
    assert persisted[0].sample.rows == [
        {"patient_id": "p1", "abnormal_condition": "normal"}
    ]


def test_sample_rows_for_descriptor_reads_json_object(tmp_path) -> None:
    source = tmp_path / "metrics.json"
    source.write_text(
        '{"metrics": {"accuracy": 1}, "target_column": "label"}\n',
        encoding="utf-8",
    )

    rows = sample_rows_for_descriptor(
        source,
        FileDescriptor(
            filename="metrics.json",
            kind="json",
            format="json",
        ),
    )

    assert rows == [{"metrics": {"accuracy": 1}, "target_column": "label"}]
