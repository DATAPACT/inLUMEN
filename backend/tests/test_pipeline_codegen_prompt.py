import unittest
from unittest.mock import Mock, patch

import inlumen_api


class PipelineCodegenPromptTests(unittest.TestCase):
    def graph(self):
        return {
            "nodes": [
                {
                    "id": "pdf",
                    "data": {
                        "label": "PDF ingestion",
                        "description": "Extract text from a user-supplied PDF.",
                        "type": "input",
                        "file_buckets": [
                            {"filename": "customer-document.pdf"},
                            {"filename": "existing_runtime.py"},
                        ],
                    },
                },
                {
                    "id": "answer",
                    "data": {
                        "label": "Answer questions",
                        "description": "Produce an answer from extracted text.",
                        "type": "output",
                    },
                },
            ],
            "edges": [{"source": "pdf", "target": "answer"}],
        }

    def test_high_level_prompt_is_combined_with_attachment_contract(self):
        codegen_payload, metadata = inlumen_api._build_pipeline_codegen_payload(
            {"nodes": [], "edges": []},
            {
                "high_level_prompt": "Build a PDF question-answering pipeline.",
                "include_sample_data": False,
            },
        )

        instruction = codegen_payload["options"]["user_instruction"]
        self.assertIn("main.py", instruction)
        self.assertIn("requirements.txt", instruction)
        self.assertIn("user supplies input files", instruction)
        self.assertIn("current working directory", instruction)
        self.assertIn("finite, non-interactive batch program", instruction)
        self.assertIn("before loading large models", instruction)
        self.assertIn("Design the entire pipeline as one coherent program", instruction)
        self.assertIn("self-check every graph edge", instruction)
        self.assertIn("Build a PDF question-answering pipeline.", instruction)
        self.assertEqual(
            "Build a PDF question-answering pipeline.",
            metadata["high_level_prompt"],
        )

    def test_attachment_contract_is_sent_without_a_custom_prompt(self):
        codegen_payload, metadata = inlumen_api._build_pipeline_codegen_payload(
            {"nodes": [], "edges": []},
            {},
        )

        self.assertIn(
            "Create the files needed to run every pipeline node",
            codegen_payload["options"]["user_instruction"],
        )
        self.assertEqual("", metadata["high_level_prompt"])

    def test_binary_input_is_transported_for_real_execution_without_text_decoding(self):
        response = Mock()
        response.content = b"%PDF-real-binary"
        response.raise_for_status.return_value = None
        graph = self.graph()

        with patch.object(inlumen_api, "_proxy", return_value=response):
            context = inlumen_api._build_pipeline_codegen_context(
                graph,
                include_samples=True,
            )

        sample = context["graph"]["nodes"][0]["files"][0]["sample"]
        self.assertEqual("JVBERi1yZWFsLWJpbmFyeQ==", sample["content_base64"])
        self.assertEqual(
            "90704a77e395c029c86b4be8ca1cfb4c39dcc64a5bb717e3cfeac3c5d2f532a7",
            sample["content_sha256"],
        )
        self.assertEqual(len(response.content), context["graph"]["nodes"][0]["files"][0]["size_bytes"])

    def test_runtime_catalog_supports_real_document_and_audio_processing(self):
        context = inlumen_api._build_pipeline_codegen_context(
            {"nodes": [], "edges": []},
            include_samples=False,
        )

        allowed = set(context["runtime_constraints"]["allowed_packages"])
        self.assertTrue(
            {"pypdf", "SpeechRecognition", "pocketsphinx", "textblob"} <= allowed
        )

    def test_external_ai_prompt_is_ready_for_manual_per_node_uploads(self):
        prompt = inlumen_api._build_external_ai_runtime_prompt(
            self.graph(),
            "Build a PDF question-answering pipeline.",
        )

        self.assertIn("Build a PDF question-answering pipeline.", prompt)
        self.assertIn("main.py", prompt)
        self.assertIn("requirements.txt", prompt)
        self.assertIn("one ZIP", prompt)
        self.assertIn("nodes/<flow_id>/", prompt)
        self.assertIn('"flow_id": "pdf"', prompt)
        self.assertIn('"flow_id": "answer"', prompt)
        self.assertIn('"source": "pdf"', prompt)
        self.assertIn('"target": "answer"', prompt)
        self.assertIn("customer-document.pdf", prompt)
        self.assertNotIn("existing_runtime.py", prompt)
        self.assertIn("Return code files only", prompt)
        self.assertIn("Never create or return input data", prompt)
        self.assertIn("must not call input()", prompt)
        self.assertIn("invalid inputs must fail immediately", prompt)
        self.assertIn("SOURCE INPUT MAP", prompt)
        self.assertIn("attaches these files to the corresponding Source node", prompt)
        self.assertIn("Design the entire pipeline as one coherent program", prompt)
        self.assertIn("source chunks or records alongside vectors", prompt)

    def test_internal_and_external_paths_share_the_behavior_contract(self):
        codegen_payload, _ = inlumen_api._build_pipeline_codegen_payload(
            self.graph(),
            {"high_level_prompt": "Build a PDF question-answering pipeline."},
        )
        external_prompt = inlumen_api._build_external_ai_runtime_prompt(
            self.graph(),
            "Build a PDF question-answering pipeline.",
        )
        internal_instruction = codegen_payload["options"]["user_instruction"]

        self.assertIn(
            inlumen_api.PIPELINE_RUNTIME_BEHAVIOR_INSTRUCTION,
            internal_instruction,
        )
        self.assertIn(
            inlumen_api.PIPELINE_RUNTIME_BEHAVIOR_INSTRUCTION,
            external_prompt,
        )

    def test_external_ai_prompt_can_infer_behavior_without_saved_chat_prompt(self):
        prompt = inlumen_api._build_external_ai_runtime_prompt(self.graph())

        self.assertIn(
            "Infer the intended pipeline behavior from the node labels",
            prompt,
        )


class PipelineCodegenResponseTests(unittest.TestCase):
    @staticmethod
    def generated_node(flow_id, *, include_script=True):
        files = (
            [{"filename": "main.py", "content": "print('ok')\n"}]
            if include_script
            else [{"filename": "requirements.txt", "content": ""}]
        )
        return {
            "flow_id": flow_id,
            "generated_artifact": {
                "files": files,
                "validation_report": {"status": "valid"},
            },
        }

    def test_every_pipeline_node_must_receive_generated_files(self):
        invalid = inlumen_api._invalid_codegen_nodes(
            [self.generated_node("one")],
            ["one", "two"],
        )

        self.assertEqual(["two"], [item["flow_id"] for item in invalid])
        self.assertIn("No generated files", invalid[0]["errors"][0])

    def test_generated_node_must_include_non_empty_python_script(self):
        invalid = inlumen_api._invalid_codegen_nodes(
            [self.generated_node("one", include_script=False)],
            ["one"],
        )

        self.assertEqual("one", invalid[0]["flow_id"])
        self.assertIn("non-empty Python script", invalid[0]["errors"][0])

    def test_complete_generated_pipeline_is_accepted(self):
        invalid = inlumen_api._invalid_codegen_nodes(
            [self.generated_node("one"), self.generated_node("two")],
            ["one", "two"],
        )

        self.assertEqual([], invalid)


if __name__ == "__main__":
    unittest.main()
