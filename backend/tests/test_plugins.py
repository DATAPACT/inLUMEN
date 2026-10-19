import copy
import hashlib
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(ROOT / "examples" / "plugins"))

import plugins  # noqa: E402
from artifact_runtime import validate_result  # noqa: E402
from connector_catalog import (  # noqa: E402
    connector_definition,
    missing_connector_parameters,
    require_supported_connector,
)
from node_ports import ports_for_template  # noqa: E402
from pipeline_graph_validation import validate_pipeline_graph  # noqa: E402
from plugins import (  # noqa: E402
    PluginManifestError,
    PluginNotInstalled,
    plugin_id_from_template,
    plugin_template,
    validate_plugin_manifest,
    with_plugin_secret_keys,
)
from plugins.manifest import default_parameters, required_parameter_names  # noqa: E402
from plugins.runtime import plugin_data_contract, plugin_main_source, plugin_runtime_spec  # noqa: E402

EXAMPLE_PATH = ROOT / "examples" / "plugins" / "text-anonymizer.plugin.json"
TEMPLATE = "plugin:example.text-anonymizer"


def example_manifest(**overrides):
    data = json.loads(EXAMPLE_PATH.read_text(encoding="utf-8"))
    data.update(overrides)
    return data


@contextmanager
def installed(*manifests):
    registry = {m["id"]: validate_plugin_manifest(m) for m in manifests}
    with patch.object(plugins, "installed_plugin_manifest", side_effect=registry.get):
        yield registry


@contextmanager
def mock_tool_server(api_key="demo-key"):
    import mock_tool_server as mock

    server = ThreadingHTTPServer(("127.0.0.1", 0), mock.Handler)
    url = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    with patch.dict(os.environ, {"MOCK_TOOL_API_KEY": api_key}):
        thread.start()
        try:
            yield url
        finally:
            server.shutdown()
            server.server_close()


class _FakeResponse(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def fake_opener(routes):
    calls = []

    def opener(request, timeout=None):
        calls.append(request)
        path = request.full_url
        for suffix, payload in routes.items():
            if path.split("?")[0].endswith(suffix):
                if isinstance(payload, int):
                    raise HTTPError(path, payload, "error", {}, None)
                return _FakeResponse(json.dumps(payload).encode())
        raise HTTPError(path, 404, "not found", {}, None)

    opener.calls = calls
    return opener


class PluginManifestTest(unittest.TestCase):
    def test_example_manifest_is_valid_and_normalized(self):
        manifest = validate_plugin_manifest(example_manifest())
        operation = manifest["operations"][0]
        self.assertEqual("input", operation["request"]["body"])
        self.assertEqual("file", operation["response"]["representation"])
        self.assertEqual({"type": "api_key", "header": "X-API-Key", "secret": "api_key"}, manifest["auth"])
        self.assertEqual(["text"], [p["name"] for p in manifest["ports"]["inputs"]])
        self.assertEqual([], required_parameter_names(manifest))
        self.assertEqual(
            {"operation": "anonymize", "replacement": "[REDACTED]", "entities": "all", "api_key": ""},
            default_parameters(manifest),
        )

    def test_defaults_fill_ports_and_bodies(self):
        manifest = validate_plugin_manifest({
            "schema_version": "inlumen.plugin@1",
            "id": "acme.weather",
            "version": "0.1.0",
            "name": "Weather",
            "description": "Daily forecast",
            "step_type": "source",
            "endpoint": {"base_url": "https://weather.example"},
            "operations": [{"id": "forecast", "method": "GET", "path": "/forecast/{city}"}],
            "parameters": [{"name": "city", "location": "path", "required": True}],
        })
        self.assertEqual("none", manifest["operations"][0]["request"]["body"])
        self.assertEqual("response.json", manifest["operations"][0]["response"]["filename"])
        self.assertEqual({"inputs": [], "outputs": [{"name": "data", "type": "any", "description": ""}]}, manifest["ports"])
        self.assertEqual({"type": "none"}, manifest["auth"])
        self.assertEqual(["city"], required_parameter_names(manifest))

    def test_reports_every_semantic_error(self):
        bad = example_manifest(
            step_type="source",
            auth={"type": "bearer", "secret": "token"},
            parameters=[{"name": "operation"}, {"name": "id", "location": "body"}],
        )
        bad["operations"][0]["path"] = "/v1/{missing}"
        with self.assertRaises(PluginManifestError) as raised:
            validate_plugin_manifest(bad)
        text = " ".join(raised.exception.errors)
        self.assertIn("a source has no inputs", text)
        self.assertIn("reserved", text)
        self.assertIn("{missing}", text)
        self.assertIn("'token' is not declared", text)
        self.assertIn("cannot declare inputs", text)

    def test_schema_errors_are_reported(self):
        with self.assertRaises(PluginManifestError) as raised:
            validate_plugin_manifest({"schema_version": "inlumen.plugin@1", "id": "Bad Id"})
        self.assertTrue(any("required property" in error for error in raised.exception.errors))

    def test_template_helpers(self):
        self.assertEqual(TEMPLATE, plugin_template("example.text-anonymizer"))
        self.assertEqual("example.text-anonymizer", plugin_id_from_template(" Plugin:example.text-anonymizer "))
        self.assertIsNone(plugin_id_from_template("REST API"))


WIKIFIER_PATH = Path(__file__).resolve().parents[1] / "plugins" / "manifests" / "jsi-wikifier.plugin.json"
MOOSE_PATH = Path(__file__).resolve().parents[1] / "plugins" / "manifests" / "datapact-moose-dpv.plugin.json"


class BuiltinPluginsTest(unittest.TestCase):
    def test_every_builtin_definition_is_valid(self):
        from plugins import store

        files = sorted(store.plugin_dir().glob("*.plugin.json"))
        self.assertIn(WIKIFIER_PATH, files)
        for path in files:
            with self.subTest(path=path.name):
                validate_plugin_manifest(path.read_text(encoding="utf-8"))

    def test_builtin_folder_merges_with_admin_definitions(self):
        from plugins import store

        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
            "INLUMEN_PLUGIN_DIR": str(Path(tmp) / "manifests"),
            "DATABASE_URL": "",
            "INLUMEN_SECRET_DB_PATH": str(Path(tmp) / "db.sqlite3"),
        }):
            folder = Path(tmp) / "manifests"
            folder.mkdir()
            (folder / "anon.plugin.json").write_text(EXAMPLE_PATH.read_text(), encoding="utf-8")
            (folder / "broken.plugin.json").write_text("{}", encoding="utf-8")
            with self.assertLogs("plugins.store", level="ERROR"):
                rows = store.list_installed_plugins()
            self.assertEqual([("example.text-anonymizer", "built-in")], [(r["id"], r["source"]) for r in rows])
            self.assertTrue(store.is_builtin_plugin("example.text-anonymizer"))
            store.install_plugin(example_manifest(version="2.0.0"), installed_by="admin")
            self.assertEqual("2.0.0", store.get_installed_manifest("example.text-anonymizer")["version"])
            self.assertTrue(store.uninstall_plugin("example.text-anonymizer"))
            # Removing the override falls back to the built-in definition.
            self.assertEqual("1.0.0", store.get_installed_manifest("example.text-anonymizer")["version"])
            store._ENGINE.dispose()
            store._ENGINE = None

    def _run_wikifier(self, filename, content, **param):
        """Run the Wikifier step against a local mock.

        Return the form it received, and under ``outputs`` the files it wrote.
        """
        import urllib.parse
        from http.server import BaseHTTPRequestHandler

        received = {}

        class Wikifier(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = self.rfile.read(int(self.headers["Content-Length"])).decode()
                received["form"] = dict(urllib.parse.parse_qsl(body))
                payload = json.dumps({"annotations": [{"title": "Rome"}], "words": [], "spaces": [""]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Wikifier)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        manifest = validate_plugin_manifest(json.loads(WIKIFIER_PATH.read_text().replace(
            "https://www.wikifier.org", f"http://127.0.0.1:{server.server_address[1]}")))
        step = {
            "flow_id": "w", "type": "task", "template": "plugin:jsi.wikifier",
            "param": {"operation": "annotate", "lang": "en", "user_key": "", **param},
            "secret_params": ["user_key"],
        }
        spec = plugin_runtime_spec(step, manifest)
        source = plugin_main_source(spec)
        received["declared"] = plugin_data_contract(spec, step)["outputs"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "in").mkdir()
            (root / "in" / filename).write_text(content, encoding="utf-8")
            with patch.dict(os.environ, {
                "PIPELINE_INPUT_DIR": str(root / "in"),
                "PIPELINE_OUTPUT_DIR": str(root / "out"),
                "INLUMEN_PARAM_USER_KEY": "k" * 30,
            }):
                os.environ.pop("INLUMEN_INPUT_MANIFEST", None)
                exec(compile(source, "wikifier-main.py", "exec"), {"__name__": "__main__"})
            received["outputs"] = {
                path.name: path.read_text(encoding="utf-8") for path in (root / "out").iterdir()
            }
        return received

    def _speech_to_text_result(self, transcript):
        return {
            "text": transcript, "transcript": transcript, "language": "en",
            "segments": [{"start": 0.0, "end": 2.5, "text": transcript}],
            "processing_metadata": {"model_id": "Systran/faster-whisper-medium"},
        }

    def test_annotations_only_output_publishes_the_response(self):
        result = self._run_wikifier(
            "speech_to_text.json", json.dumps(self._speech_to_text_result("Rome.")), output_mode="annotations")
        self.assertEqual(["annotations.json"], list(result["outputs"]))
        self.assertEqual(
            {"annotations": [{"title": "Rome"}], "words": [], "spaces": [""]},
            json.loads(result["outputs"]["annotations.json"]),
        )
        [declared] = result["declared"]
        self.assertEqual(("annotations", "annotations.json", "json"), (declared["name"], declared["filename"], declared["kind"]))

    def test_by_default_the_transcript_is_kept_and_the_annotations_added(self):
        stt = self._speech_to_text_result("Rome is the capital of Italy.")
        # A step created before output_mode existed has no value: it enriches too.
        result = self._run_wikifier("speech_to_text.json", json.dumps(stt))
        self.assertEqual(["annotated.json"], list(result["outputs"]))
        enriched = json.loads(result["outputs"]["annotated.json"])
        self.assertEqual({**stt, "wikifier": {"annotations": [{"title": "Rome"}], "words": [], "spaces": [""]}}, enriched)
        [declared] = result["declared"]
        self.assertEqual(
            ("annotated", "annotated.json", "json", "json"),
            (declared["name"], declared["filename"], declared["kind"], declared["format"]),
        )
        self.assertIn("'wikifier'", declared["description"])

    def test_enrich_wraps_plain_text_input(self):
        result = self._run_wikifier("notes.txt", "Rome is in Italy.", output_mode="input_with_annotations")
        self.assertEqual(
            {"text": "Rome is in Italy.", "wikifier": {"annotations": [{"title": "Rome"}], "words": [], "spaces": [""]}},
            json.loads(result["outputs"]["annotated.json"]),
        )

    def test_a_chain_passes_the_transcript_through_an_enriching_plugin(self):
        # Speech-to-Text -> Wikifier (enrich) -> a text step such as Sentiment Analysis.
        transcript = "Thank you for calling NordKart support."
        wikified = self._run_wikifier(
            "speech_to_text.json", json.dumps(self._speech_to_text_result(transcript)),
        )["outputs"]["annotated.json"]
        self.assertEqual(transcript, json.loads(wikified)["transcript"])
        downstream = self._run_wikifier("annotated.json", wikified)
        self.assertEqual(transcript, downstream["form"]["text"])

    def test_candidate_ranges_can_be_requested(self):
        result = self._run_wikifier("notes.txt", "Rome is in Italy.", ranges="true")
        self.assertEqual("true", result["form"]["ranges"])

    def test_merged_output_is_compact_json(self):
        stt = self._speech_to_text_result("Rome is the capital of Italy.")
        published = self._run_wikifier("speech_to_text.json", json.dumps(stt))["outputs"]["annotated.json"]
        self.assertEqual(json.dumps(json.loads(published), ensure_ascii=False, separators=(",", ":")), published)

    def test_unknown_output_mode_is_rejected(self):
        manifest = validate_plugin_manifest(WIKIFIER_PATH.read_text())
        with self.assertRaisesRegex(ValueError, "output_mode"):
            plugin_runtime_spec({"type": "task", "param": {"output_mode": "both"}}, manifest)

    def test_merge_requires_a_json_response_and_input_text(self):
        data = example_manifest()
        data["operations"][0]["response"] = {"format": "txt", "merge": {"key": "anon"}}
        with self.assertRaises(PluginManifestError) as raised:
            validate_plugin_manifest(data)
        message = str(raised.exception)
        self.assertIn("response.merge requires a JSON response", message)
        self.assertIn("response.merge requires request.input_field", message)

    def test_plugins_receive_the_transcript_of_a_speech_to_text_result(self):
        transcript = "Thank you for calling NordKart support. This is Sarah."
        result = {
            "text": transcript, "transcript": transcript, "language": "en",
            "segments": [{"start": 0.0, "end": 2.5, "text": transcript,
                          "words": [{"word": word, "start": 0.1, "probability": 0.99} for word in transcript.split()]}] * 40,
            "processing_metadata": {"model_id": "Systran/faster-whisper-medium"},
        }
        received = self._run_wikifier("speech_to_text.json", json.dumps(result))
        self.assertEqual(transcript, received["form"]["text"])

    def test_plugins_receive_plain_text_unchanged(self):
        received = self._run_wikifier("notes.txt", '{not json} Rome is in Italy.')
        self.assertEqual('{not json} Rome is in Italy.', received["form"]["text"])

    def test_json_without_text_fails_before_calling_the_tool(self):
        with self.assertRaises(BaseException) as raised:
            self._run_wikifier("table.json", json.dumps([{"a": 1}]))
        self.assertIn("without a text field", str(raised.exception))

    def test_wikifier_sends_a_form_with_text_and_user_key(self):
        import urllib.parse
        from http.server import BaseHTTPRequestHandler

        received = {}

        class Wikifier(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                received["path"] = self.path
                received["type"] = self.headers.get("Content-Type")
                body = self.rfile.read(int(self.headers["Content-Length"])).decode()
                received["form"] = dict(urllib.parse.parse_qsl(body))
                payload = json.dumps({"annotations": [{"title": "Rome"}], "words": ["Rome"], "spaces": ["", ""]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Wikifier)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        manifest = validate_plugin_manifest(json.loads(WIKIFIER_PATH.read_text().replace(
            "https://www.wikifier.org", f"http://127.0.0.1:{server.server_address[1]}")))
        step = {
            "flow_id": "w", "type": "task", "template": "plugin:jsi.wikifier",
            "param": {"operation": "annotate", "lang": "en", "wikiDataClasses": "true", "user_key": ""},
            "secret_params": ["user_key"],
        }
        source = plugin_main_source(plugin_runtime_spec(step, manifest))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "in").mkdir()
            (root / "in" / "doc.txt").write_text("Rome is the capital of Italy.", encoding="utf-8")
            with patch.dict(os.environ, {
                "PIPELINE_INPUT_DIR": str(root / "in"),
                "PIPELINE_OUTPUT_DIR": str(root / "out"),
                "INLUMEN_PARAM_USER_KEY": "k" * 30,
            }):
                os.environ.pop("INLUMEN_INPUT_MANIFEST", None)
                exec(compile(source, "wikifier-main.py", "exec"), {"__name__": "__main__"})
            result = json.loads((root / "out" / "annotated.json").read_text())
        self.assertEqual("Rome is the capital of Italy.", result["text"])
        self.assertEqual("Rome", result["wikifier"]["annotations"][0]["title"])
        self.assertEqual("/annotate-article", received["path"])
        self.assertEqual("application/x-www-form-urlencoded", received["type"])
        form = received["form"]
        self.assertEqual("Rome is the capital of Italy.", form["text"])
        self.assertEqual("k" * 30, form["userKey"])
        self.assertEqual("en", form["lang"])
        self.assertEqual("true", form["wikiDataClasses"])
        self.assertEqual("0.8", form["pageRankSqThreshold"])
        # Candidate ranges are megabytes for a short transcript: not asked for by default.
        self.assertEqual("false", form["ranges"])
        self.assertNotIn("k" * 30, source)

    def test_error_fields_fail_a_200_response(self):
        manifest = validate_plugin_manifest(WIKIFIER_PATH.read_text())
        spec = plugin_runtime_spec({"type": "task", "param": {}}, manifest)
        namespace = {"__name__": "wikifier_checks"}
        exec(compile(plugin_main_source(spec), "wikifier-main.py", "exec"), namespace)
        check = namespace["_check_payload"]
        operation = spec["operation"]
        with self.assertRaisesRegex(RuntimeError, "user-key-not-found"):
            check(b'{"userKey": "", "status": "user-key-not-found"}', operation)
        check(b'{"annotations": [], "words": [], "spaces": [""]}', operation)

    def _fake_moose(self, final_status="completed"):
        from http.server import BaseHTTPRequestHandler

        calls = []

        class Moose(BaseHTTPRequestHandler):
            polls = 0

            def _reply(self, payload, status=200):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                calls.append(("POST", self.path, self.headers, body))
                self._reply({"job_id": "job42", "status": "queued"})

            def do_GET(self):  # noqa: N802
                calls.append(("GET", self.path, self.headers, None))
                Moose.polls += 1
                if Moose.polls < 2:
                    return self._reply({"job_id": "job42", "status": "processing"})
                if final_status == "failed":
                    return self._reply({"job_id": "job42", "status": "failed", "error": "LLM quota exceeded"})
                # Shape of Moose's NER job result (moose_api/queue.py, moose/ner.py).
                return self._reply({"job_id": "job42", "status": "completed", "result": {
                    "entities": [{"start": 14, "end": 27, "text": "email address",
                                  "type_id": "dpv-pd:EmailAddress", "confidence": 0.93}]}})

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Moose)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}", calls

    def _run_moose(self, url, **param):
        manifest = validate_plugin_manifest(json.loads(MOOSE_PATH.read_text().replace(
            "https://moose.zooverse.dev", url)))
        manifest["operations"][0]["async"]["interval_seconds"] = 0.5
        step = {
            "flow_id": "m", "type": "task", "template": "plugin:datapact.moose-dpv",
            "param": {"operation": "annotate", "llm_model": "deepseek-chat", "moose_api_key": "", "llm_api_key": "", **param},
            "secret_params": ["moose_api_key", "llm_api_key"],
        }
        source = plugin_main_source(plugin_runtime_spec(step, manifest))
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "in").mkdir()
        (root / "in" / "notice.txt").write_text("We store your email address for marketing.", encoding="utf-8")
        with patch.dict(os.environ, {
            "PIPELINE_INPUT_DIR": str(root / "in"),
            "PIPELINE_OUTPUT_DIR": str(root / "out"),
            "INLUMEN_PARAM_MOOSE_API_KEY": "moose-secret",
            "INLUMEN_PARAM_LLM_API_KEY": "llm-secret",
        }):
            os.environ.pop("INLUMEN_INPUT_MANIFEST", None)
            exec(compile(source, "moose-main.py", "exec"), {"__name__": "__main__"})
        return root / "out", source

    def test_moose_submits_a_job_and_publishes_its_result(self):
        url, calls = self._fake_moose()
        out, source = self._run_moose(url, vocabulary="dpv_pd", llm_provider="deepseek")
        result = json.loads((out / "annotated.json").read_text())
        self.assertEqual("We store your email address for marketing.", result["text"])
        entity = result["dpv"]["entities"][0]
        self.assertEqual("dpv-pd:EmailAddress", entity["type_id"])
        self.assertEqual("email address", "We store your email address for marketing."[entity["start"]:entity["end"]])
        method, path, headers, body = calls[0]
        self.assertEqual(("POST", "/schemas/dpv_pd/ner"), (method, path))
        self.assertEqual({"text": "We store your email address for marketing.",
                          "llm": {"provider": "deepseek", "model": "deepseek-chat"}}, body)
        self.assertEqual("moose-secret", headers["X-API-Key"])
        self.assertEqual("llm-secret", headers["X-LLM-API-Key"])
        polls = [call for call in calls if call[0] == "GET"]
        self.assertEqual(["/jobs/job42", "/jobs/job42"], [call[1] for call in polls])
        self.assertEqual("moose-secret", polls[0][2]["X-API-Key"])
        self.assertNotIn("moose-secret", source)
        self.assertNotIn("llm-secret", source)

    def test_moose_job_failure_fails_the_step(self):
        url, _ = self._fake_moose(final_status="failed")
        with self.assertRaisesRegex(RuntimeError, "LLM quota exceeded"):
            self._run_moose(url)

    def test_form_options_are_validated(self):
        bad = example_manifest(auth={"type": "api_key", "form_field": "key", "secret": "api_key"})
        bad["operations"][0]["request"] = {"body": "input", "input_field": "text"}
        with self.assertRaises(PluginManifestError) as raised:
            validate_plugin_manifest(bad)
        text = " ".join(raised.exception.errors)
        self.assertIn("input_field requires body 'form' or 'parameters'", text)
        self.assertIn("form_field requires every operation", text)


class PluginStoreTest(unittest.TestCase):
    def test_install_upgrade_and_uninstall(self):
        from plugins import store

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {
                "DATABASE_URL": "",
                "INLUMEN_PLUGIN_DIR": str(Path(tmp) / "no-builtins"),
                "INLUMEN_SECRET_DB_PATH": str(Path(tmp) / "db.sqlite3"),
            },
        ):
            row = store.install_plugin(example_manifest(), installed_by="admin")
            self.assertEqual("1.0.0", row["version"])
            store.install_plugin(example_manifest(version="1.1.0"), installed_by="admin")
            self.assertEqual(["1.1.0"], [r["version"] for r in store.list_installed_plugins()])
            self.assertEqual("Text Anonymizer", store.get_installed_manifest("example.text-anonymizer")["name"])
            self.assertTrue(store.uninstall_plugin("example.text-anonymizer"))
            self.assertFalse(store.uninstall_plugin("example.text-anonymizer"))
            self.assertIsNone(store.get_installed_manifest("example.text-anonymizer"))
            with self.assertRaises(PluginManifestError):
                store.install_plugin({"id": "x"}, installed_by="admin")
            store._ENGINE.dispose()


class PluginGraphIntegrationTest(unittest.TestCase):
    def test_connector_catalog_resolves_installed_plugins(self):
        with installed(example_manifest()):
            require_supported_connector("task", TEMPLATE)
            self.assertFalse(connector_definition("task", TEMPLATE)["requires_attached_files"])
            with self.assertRaisesRegex(ValueError, "task plugin"):
                require_supported_connector("source", TEMPLATE)
            with self.assertRaises(PluginNotInstalled):
                require_supported_connector("task", "plugin:example.missing")

    def test_missing_required_parameters_follow_the_manifest(self):
        weather = {
            "schema_version": "inlumen.plugin@1", "id": "acme.weather", "version": "1.0.0",
            "name": "Weather", "description": "Forecast", "step_type": "source",
            "endpoint": {"base_url": "https://weather.example"},
            "operations": [{"id": "forecast", "method": "GET", "path": "/forecast"}],
            "parameters": [{"name": "city", "required": True}],
        }
        with installed(weather):
            self.assertEqual(["city"], missing_connector_parameters("source", "plugin:acme.weather", {}))
            self.assertEqual([], missing_connector_parameters("source", "plugin:acme.weather", {"city": "Oslo"}))

    def test_ports_come_from_the_manifest(self):
        with installed(example_manifest()):
            ports = ports_for_template(None, "task", TEMPLATE)
        self.assertEqual(["text"], [p["id"] for p in ports["inputs"]])
        self.assertEqual(["anonymized"], [p["id"] for p in ports["outputs"]])

    def test_plugin_secrets_are_always_secret(self):
        with installed(example_manifest()):
            keys = with_plugin_secret_keys([], TEMPLATE, {"api_key": "s3cr3t", "entities": "all"})
        self.assertEqual(["api_key"], keys)
        self.assertEqual(["x"], with_plugin_secret_keys(["x"], "REST API", {"x": 1}))

    def test_validation_accepts_plugin_tasks_and_flags_missing_plugins(self):
        graph = {
            "nodes": [
                {"id": "1", "data": {"type": "source", "label": "In", "template_label": "Custom"}},
                {"id": "2", "data": {"type": "task", "label": "Anon", "template_label": TEMPLATE,
                                     "ports": {"inputs": [{"id": "text", "name": "text"}], "outputs": [{"id": "anonymized", "name": "anonymized"}]}}},
                {"id": "3", "data": {"type": "destination", "label": "Out", "template_label": "Custom"}},
            ],
            "edges": [
                {"id": "e1", "source": "1", "target": "2", "targetHandle": "text"},
                {"id": "e2", "source": "2", "target": "3", "sourceHandle": "anonymized"},
            ],
        }
        with installed(example_manifest()):
            report = validate_pipeline_graph(graph)
        codes = {issue["code"] for issue in report["issues"] if issue.get("node_id") == "2"}
        self.assertNotIn("missing-implementation", codes)
        with installed():
            report = validate_pipeline_graph(graph)
        self.assertIn(
            ("2", "plugin-not-installed", "error"),
            {(i.get("node_id"), i["code"], i["severity"]) for i in report["issues"]},
        )


class PluginRuntimeTest(unittest.TestCase):
    def _step(self, **param):
        return {
            "flow_id": "2",
            "type": "task",
            "label": "Anonymize",
            "template": TEMPLATE,
            "ports": {"inputs": [{"id": "text"}], "outputs": [{"id": "anonymized"}]},
            "param": {"operation": "anonymize", "entities": "email", "api_key": "", **param},
            "secret_params": ["api_key"],
        }

    def test_spec_never_contains_secrets(self):
        manifest = validate_plugin_manifest(example_manifest())
        spec = plugin_runtime_spec(self._step(api_key="leaked", password="x"), manifest)
        self.assertEqual({"operation": "anonymize", "entities": "email"}, spec["values"])
        source = plugin_main_source(spec)
        self.assertNotIn("leaked", source)
        compile(source, "plugin-main.py", "exec")
        with self.assertRaisesRegex(ValueError, "task plugin"):
            plugin_runtime_spec({**self._step(), "type": "source"}, manifest)

    def _run_adapter(self, url, api_key, inputs):
        manifest = validate_plugin_manifest(
            json.loads(EXAMPLE_PATH.read_text().replace("http://mock-tool:8099", url))
        )
        source = plugin_main_source(plugin_runtime_spec(self._step(), manifest))
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        input_dir, output_dir, work_dir = root / "in", root / "out", root / "work"
        input_dir.mkdir()
        for name, text in inputs.items():
            (input_dir / name).write_text(text, encoding="utf-8")
        env = {
            "PIPELINE_INPUT_DIR": str(input_dir),
            "PIPELINE_OUTPUT_DIR": str(output_dir),
            "PIPELINE_WORK_DIR": str(work_dir),
            "INLUMEN_PARAM_API_KEY": api_key,
        }
        with patch.dict(os.environ, env):
            os.environ.pop("INLUMEN_INPUT_MANIFEST", None)
            exec(compile(source, "plugin-main.py", "exec"), {"__name__": "__main__"})
        return output_dir, work_dir

    def test_adapter_calls_the_deployed_tool_end_to_end(self):
        with mock_tool_server() as url:
            output_dir, work_dir = self._run_adapter(
                url, "demo-key", {"note.txt": "Mail ola@example.no or call +47 22 33 44 55"}
            )
        result = json.loads((output_dir / "anonymized.json").read_text())
        # entities=email comes from the step parameters embedded in the spec.
        self.assertEqual({"text": "Mail [REDACTED] or call +47 22 33 44 55", "removed": 1}, result)
        declarations = [{"name": "anonymized", "filename": "anonymized.json", "format": "json", "kind": "json"}]
        validate_result(output_dir, declarations)
        calls = json.loads((work_dir / "plugin-calls.json").read_text())
        self.assertEqual(200, calls["calls"][0]["status"])

    def test_adapter_surfaces_tool_errors_and_missing_secrets(self):
        with mock_tool_server() as url:
            with self.assertRaisesRegex(RuntimeError, "HTTP 401"):
                self._run_adapter(url, "wrong-key", {"a.txt": "x"})
            with self.assertRaisesRegex(RuntimeError, "requires the secret 'api_key'"):
                self._run_adapter(url, "", {"a.txt": "x"})
            with self.assertRaisesRegex(RuntimeError, "single file"):
                self._run_adapter(url, "demo-key", {"a.txt": "x", "b.txt": "y"})


class PluginDeploymentTest(unittest.TestCase):
    def test_plugin_task_exports_a_managed_runtime(self):
        try:
            from async_runtime import run_async
            from deployment_agents import generate_dockerfiles_with_agent
            from deployment_artifacts import build_deployment_bundle_files
        except ModuleNotFoundError as exc:  # pragma: no cover - optional app deps
            self.skipTest(f"deployment dependencies are unavailable: {exc}")
        graph = {
            "nodes": [
                {"id": "1", "data": {"label": "Notes", "type": "source", "template_label": "File"}},
                {"id": "2", "data": {
                    "label": "Anonymize", "type": "task", "template_label": TEMPLATE,
                    "ports": {"inputs": [{"id": "text", "name": "text"}], "outputs": [{"id": "anonymized", "name": "anonymized"}]},
                    "param": {"operation": "anonymize", "entities": "all", "api_key": ""},
                    "secret_params": ["api_key"],
                }},
                {"id": "3", "data": {"label": "Out", "type": "destination", "template_label": "File", "param": {"filename": "out.json"}}},
            ],
            "edges": [
                {"source": "1", "target": "2", "targetHandle": "text"},
                {"source": "2", "target": "3", "sourceHandle": "anonymized", "targetHandle": "data"},
            ],
        }
        with installed(example_manifest()):
            payload = run_async(generate_dockerfiles_with_agent([], [], pipeline_graph=graph, require_attached_runtime=True))
            result = payload.model_dump() if hasattr(payload, "model_dump") else payload.dict()
            artifact = next(a for a in result["runtime_artifacts"] if a["flow_id"] == "2")
            self.assertEqual("inlumen-managed-adapter", artifact["generator"])
            self.assertEqual("example.text-anonymizer", artifact["manifest"]["adapter"]["plugin"]["id"])
            self.assertEqual("anonymized.json", artifact["data_contract"]["outputs"][0]["filename"])
            body = "contact ola@example.no"
            result["input_files"] = [{
                "flow_id": "1", "filename": "notes.txt", "content": body,
                "size_bytes": len(body.encode()),
                "sha256": "sha256:" + hashlib.sha256(body.encode()).hexdigest(),
            }]
            bundle = build_deployment_bundle_files(graph, result, targets={"argo": True, "dagster": False})
        paths = {item["path"]: item for item in bundle["files"]}
        workflow = paths["argo/workflow.yaml"]["content"]
        self.assertIn("INLUMEN_PARAM_API_KEY", workflow)
        self.assertIn("secretKeyRef", workflow)
        # The bundle creates the Secret the workflow reads from the .env name.
        self.assertIn("'2.api_key|INLUMEN_SECRET_2_API_KEY|required'", paths["argo/create-secrets.sh"]["content"])
        self.assertIn("INLUMEN_SECRET_2_API_KEY=", paths[".env.example"]["content"])
        self.assertNotIn("demo-key", json.dumps(bundle))

        with installed():
            with self.assertRaisesRegex(Exception, "not installed"):
                run_async(generate_dockerfiles_with_agent([], [], pipeline_graph=copy.deepcopy(graph), require_attached_runtime=True))


class PluginAgentToolsTest(unittest.TestCase):
    def setUp(self):
        try:
            import pipeline_agent.tools as tools_module
        except ModuleNotFoundError as exc:  # pragma: no cover - optional app deps
            self.skipTest(str(exc))
        self.tools_module = tools_module

    def _tools(self):
        return {tool.__name__: tool for tool in self.tools_module.build_pipeline_editor_tools()}

    def test_agent_lists_plugins_and_creates_a_configured_plugin_step(self):
        import asyncio
        from unittest.mock import AsyncMock

        manifest = validate_plugin_manifest(example_manifest())
        with installed(example_manifest()), patch(
            "plugins.store.list_installed_plugins", return_value=[{"manifest": manifest}]
        ):
            tools = self._tools()
            catalog = json.loads(asyncio.run(tools["list_pipeline_components"]("{}")))
            self.assertEqual([TEMPLATE], [p["template"] for p in catalog["plugins"]])
            guidance = json.loads(asyncio.run(tools["get_plugin_instructions"](json.dumps({"template": TEMPLATE}))))
            self.assertIn("e-mail", guidance["publisher_notes_untrusted"])
            self.assertIn("api_key", guidance["configured_after_design"])

            run_query = AsyncMock(return_value="[]")
            with patch("pipeline_agent.tools.run_neo4j_query", run_query):
                try:
                    asyncio.run(tools["create_step"](json.dumps({
                        "type": "task", "label": "Anonymize", "description": "Remove PII",
                        "template": TEMPLATE,
                    })))
                except RuntimeError:
                    pass  # the mocked graph returns no rows; only the query matters
            query = run_query.call_args_list[0].args[0]
            self.assertIn(f"template_label:'{TEMPLATE}'", query)
            self.assertIn("secret_params_json: '[\"api_key\"]'", query)
            self.assertIn("primary_input_port: 'text'", query)
            with self.assertRaisesRegex(RuntimeError, "task plugin"):
                asyncio.run(tools["create_step"](json.dumps({
                    "type": "source", "label": "x", "description": "y", "template": TEMPLATE,
                })))

    def test_listing_survives_an_unavailable_registry(self):
        import asyncio

        with patch("plugins.store.list_installed_plugins", side_effect=RuntimeError("db down")):
            catalog = json.loads(asyncio.run(self._tools()["list_pipeline_components"]("{}")))
        self.assertEqual([], catalog["plugins"])


class PluginRoutesTest(unittest.TestCase):
    def setUp(self):
        try:
            from flask import Flask
        except ModuleNotFoundError as exc:  # pragma: no cover
            self.skipTest(str(exc))
        from plugins.routes import create_plugins_blueprint

        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {
            "AUTH_ENABLED": "false",
            "APP_ENV": "development",
            "DATABASE_URL": "",
            "INLUMEN_PLUGIN_DIR": str(Path(self.tmp.name) / "none"),
            "INLUMEN_SECRET_DB_PATH": str(Path(self.tmp.name) / "db.sqlite3"),
        })
        self.env.start()
        app = Flask(__name__)
        app.register_blueprint(create_plugins_blueprint(
            endpoint_checker=lambda m: {"reachable": True, "url": m["endpoint"]["base_url"]},
        ))
        self.client = app.test_client()

    def tearDown(self):
        from plugins import store

        if store._ENGINE is not None:
            store._ENGINE.dispose()
            store._ENGINE = None
        self.env.stop()
        self.tmp.cleanup()

    def test_add_list_check_and_remove(self):
        response = self.client.post("/api/admin/plugins", json={"manifest": example_manifest()})
        self.assertEqual(201, response.status_code, response.get_json())
        self.assertEqual("manual", response.get_json()["source"])
        listed = self.client.get("/api/plugins").get_json()
        self.assertEqual(["example.text-anonymizer"], [p["id"] for p in listed["plugins"]])
        self.assertTrue(listed["can_manage"])
        self.assertNotIn("catalog_configured", listed)
        self.assertEqual("plugin:example.text-anonymizer", listed["plugins"][0]["template"])
        check = self.client.post("/api/plugins/example.text-anonymizer/check").get_json()
        self.assertTrue(check["reachable"])
        self.assertEqual(200, self.client.delete("/api/admin/plugins/example.text-anonymizer").status_code)
        self.assertEqual(404, self.client.get("/api/plugins/example.text-anonymizer").status_code)
        self.assertEqual(404, self.client.get("/api/plugins/catalog").status_code)

    def test_invalid_definitions_report_errors(self):
        response = self.client.post("/api/admin/plugins", json={"manifest": {"id": "bad"}})
        self.assertEqual(400, response.status_code)
        self.assertTrue(response.get_json()["details"])
        self.assertEqual(400, self.client.post("/api/admin/plugins", json={"id": "x"}).status_code)

    def test_builtin_plugins_cannot_be_removed_in_the_ui(self):
        folder = Path(self.tmp.name) / "none"
        folder.mkdir()
        (folder / "anon.plugin.json").write_text(EXAMPLE_PATH.read_text(), encoding="utf-8")
        listed = self.client.get("/api/plugins").get_json()
        self.assertEqual("built-in", listed["plugins"][0]["source"])
        self.assertEqual(409, self.client.delete("/api/admin/plugins/example.text-anonymizer").status_code)

    def test_non_admins_cannot_add(self):
        with patch.dict(os.environ, {"APP_ENV": "production"}):
            response = self.client.post("/api/admin/plugins", json={"manifest": example_manifest()})
        self.assertEqual(403, response.status_code)


if __name__ == "__main__":
    unittest.main()
