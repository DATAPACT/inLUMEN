"""A node's declared outputs decide its contract; wording only when it declares nothing."""

from inlumen.codegen.generator import expected_outputs_for_node
from inlumen.codegen.schemas import FileDescriptor, NodeDescriptor
from inlumen.codegen.task_profiles import classify_node_task, implementation_plan_for_node
from inlumen.contracts.node_definitions.artifacts import implementation_plan_from_data
from inlumen.contracts.node_ports import normalize_node_ports

TABLE = [FileDescriptor(filename="rows.csv", kind="table", format="csv", columns=["id", "x", "y"])]


def node(label, description="", *, outputs=None, artifacts=None):
    """A task node as the editor or an agent would store it, then as codegen sees it."""
    data = {"type": "task", "label": label, "description": description}
    if outputs is not None:
        data["ports"] = {"inputs": [{"id": "input", "type": "table"}], "outputs": outputs}
    if artifacts is not None:
        data["implementation"] = {"output_artifacts": artifacts}
    return NodeDescriptor(
        flow_id=label.lower().replace(" ", "_"), label=label, description=description, type="task",
        ports=normalize_node_ports(data.get("ports"), "task"),
        implementation=implementation_plan_from_data(data))


MODEL_AND_SCORES = node(
    "Fit Estimator", "Fit an estimator and score the held-out rows.",
    outputs=[{"id": "model", "name": "model.pkl", "type": "model"},
             {"id": "scores", "name": "scores.csv", "type": "table"}],
    artifacts=[{"name": "model", "filename": "model.pkl", "port": "model", "kind": "binary", "format": "pkl"},
               {"name": "scores", "filename": "scores.csv", "port": "scores", "kind": "table", "format": "csv"}])


def test_declared_outputs_survive_the_model_plan():
    assert classify_node_task(MODEL_AND_SCORES, TABLE) == "model_training"
    plan = implementation_plan_for_node(MODEL_AND_SCORES, TABLE)
    assert plan["execution_profile"] == "classical_ml"
    assert [a["filename"] for a in plan["output_artifacts"]] == ["model.pkl", "scores.csv"]


def test_each_declared_port_keeps_its_own_output():
    outputs = expected_outputs_for_node(MODEL_AND_SCORES, [], TABLE)
    assert [(o.port, o.filename) for o in outputs] == [("model", "model.pkl"), ("scores", "scores.csv")]


def test_a_declared_bundle_with_a_model_member_is_training():
    bundle = node("Learner", artifacts=[{
        "name": "learner", "filename": "learner", "kind": "directory", "representation": "directory",
        "members": [{"name": "weights", "filename": "weights.pickle", "kind": "model", "format": "pickle"}]}])
    assert classify_node_task(bundle, TABLE) == "model_training"
    assert [o.filename for o in expected_outputs_for_node(bundle, [], TABLE)] == ["learner"]


def test_steps_around_a_model_are_not_training_whatever_their_wording():
    for label, description in (
        ("Prepare Prediction Data", "Select the predictors used to train the model later."),
        ("Train/Test Split", "Split the rows before the model is fit."),
        ("Evaluate Regression Model", "Score the fitted model on the held-out rows."),
    ):
        step = node(label, description, outputs=[{"id": "output", "name": "out.json", "type": "json"}],
                    artifacts=[{"name": "out", "filename": "out.json", "port": "output", "kind": "json"}])
        assert classify_node_task(step, TABLE) != "model_training", label
        assert implementation_plan_for_node(step, TABLE).get("execution_profile") != "classical_ml", label


def test_several_typed_ports_without_artifacts_give_one_output_per_port():
    split = node("Divide Rows", outputs=[{"id": "train", "name": "train.csv", "type": "table"},
                                         {"id": "test", "type": "table"}])
    outputs = expected_outputs_for_node(split, [], TABLE)
    assert [(o.port, o.filename, o.kind) for o in outputs] == [
        ("train", "train.csv", "table"), ("test", "test.csv", "table")]


def test_a_node_that_declares_nothing_is_still_judged_by_its_words():
    bare = node("Churn Model Training", "Fit a classifier on the customer rows.")
    assert classify_node_task(bare, TABLE) == "model_training"
    outputs = expected_outputs_for_node(bare, [], TABLE)
    assert len(outputs) == 1 and outputs[0].representation == "directory"
