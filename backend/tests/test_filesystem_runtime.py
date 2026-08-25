import os
import tempfile
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from filesystem_runtime import (  # noqa: E402
    PIPELINE_INPUT_DIR,
    PIPELINE_OUTPUT_DIR,
    discover_artifacts,
    direct_parameter_environment_name,
    filesystem_shell_component_source,
    parameter_environment,
    prepare_workspace,
    normalize_single_output_port,
    stage_input_bindings,
    stage_input_directories,
    task_environment,
)


class FilesystemRuntimeTest(unittest.TestCase):
    def test_port_bindings_remap_source_outputs_to_target_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            parent = root / "parent"
            (parent / "data").mkdir(parents=True)
            (parent / "ignored").mkdir()
            (parent / "data" / "cities.csv").write_text("city\nOslo\n")
            (parent / "ignored" / "private.txt").write_text("hidden")
            staged = stage_input_bindings(
                [
                    {
                        "source_dir": str(parent),
                        "source_port": "data",
                        "target_port": "input",
                    }
                ],
                root / "workspace" / "input",
            )
            self.assertTrue((staged / "input" / "cities.csv").is_file())
            self.assertFalse((staged / "ignored").exists())

    def test_single_output_port_normalizes_legacy_root_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "output"
            output.mkdir()
            (output / "enriched.csv").write_text("city,temp\nOslo,12\n")
            moved = normalize_single_output_port(output, ["result"])
            self.assertEqual(["enriched.csv"], moved)
            self.assertTrue((output / "result" / "enriched.csv").is_file())

    def test_multiple_output_ports_reject_ambiguous_root_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "output"
            output.mkdir()
            (output / "result.json").write_text("{}")
            with self.assertRaisesRegex(RuntimeError, "outside declared"):
                normalize_single_output_port(output, ["left", "right"])

    def test_workspace_contract_stages_artifacts_and_discovers_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            (source / "nested").mkdir()
            (source / "nested" / "input.txt").write_text("input", encoding="utf-8")

            input_dir, output_dir = prepare_workspace(root / "workspace", [source])
            self.assertEqual("input", (input_dir / "nested" / "input.txt").read_text())
            self.assertEqual(
                {
                    PIPELINE_INPUT_DIR: str(input_dir.resolve()),
                    PIPELINE_OUTPUT_DIR: str(output_dir.resolve()),
                },
                task_environment(input_dir, output_dir),
            )

            (output_dir / "result.json").write_text("{}", encoding="utf-8")
            artifacts = discover_artifacts(output_dir)
            self.assertEqual("result.json", artifacts[0]["path"])
            self.assertEqual(2, artifacts[0]["size_bytes"])
            self.assertIn("sha256", artifacts[0])

    def test_multi_parent_collision_requires_an_explicit_merge_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()
            (first / "result.txt").write_text("first", encoding="utf-8")
            (second / "result.txt").write_text("second", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "collide"):
                stage_input_directories([first, second], root / "input")

    def test_identical_multi_parent_artifacts_are_deduplicated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()
            for directory in (first, second):
                (directory / "shared.txt").write_text("same", encoding="utf-8")
            staged = stage_input_directories([first, second], root / "input")
            self.assertEqual("same", (staged / "shared.txt").read_text())

    def test_exported_dagster_runner_uses_the_same_directory_contract(self):
        source = filesystem_shell_component_source().replace("import dagster as dg", "")
        helpers = source.split("\nclass ShellCommand", 1)[0]
        namespace: dict[str, object] = {}
        exec(compile(helpers, "shell_command.py", "exec"), namespace)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            upstream = root / "upstream"
            upstream.mkdir()
            (upstream / "result.txt").write_text("result", encoding="utf-8")
            staged = root / "workspace" / "input"
            namespace["_stage_inputs"]([upstream], staged)
            self.assertEqual("result", (staged / "result.txt").read_text())

    def test_parameters_are_available_by_their_exact_safe_names(self):
        environment = parameter_environment({"QUESTION": "When is support available?"})
        self.assertEqual("When is support available?", environment["QUESTION"])
        self.assertEqual("When is support available?", environment["PIPELINE_PARAM_QUESTION"])
        self.assertEqual("QUESTION", direct_parameter_environment_name("QUESTION"))
        self.assertEqual("", direct_parameter_environment_name("question-text"))
        self.assertEqual("", direct_parameter_environment_name("PIPELINE_INPUT_DIR"))


if __name__ == "__main__":
    unittest.main()
