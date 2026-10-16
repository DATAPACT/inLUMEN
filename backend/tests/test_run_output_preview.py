import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from run_output_preview import build_preview, preview_kind  # noqa: E402


def reader(content: bytes):
    calls = []

    def read_window(offset, length):
        calls.append((offset, length))
        end = None if length is None else offset + length
        return content[offset:end], len(content)

    read_window.calls = calls
    return read_window


class RunOutputPreviewTest(unittest.TestCase):
    def test_kind_detection(self):
        self.assertEqual("table", preview_kind("a.csv"))
        self.assertEqual("table", preview_kind("a.TSV"))
        self.assertEqual("jsonl", preview_kind("a.jsonl"))
        self.assertEqual("json", preview_kind("a.json"))
        self.assertEqual("markdown", preview_kind("README.md"))
        self.assertEqual("text", preview_kind("graph.ttl"))
        self.assertEqual("image", preview_kind("plot.png", "image/png"))
        self.assertEqual("pdf", preview_kind("report.pdf"))
        self.assertEqual("binary", preview_kind("model.bin", "application/octet-stream"))

    def test_csv_pages_by_byte_offset_and_handles_quoted_newlines(self):
        body = 'id,note,score\n1,"two\nlines",3.5\n2,plain,4\n3,"x, y",5\n'.encode()
        read = reader(body)

        first = build_preview(read, {"path": "o/a.csv", "filename": "a.csv"}, limit=2)

        self.assertEqual("table", first["kind"])
        self.assertEqual(["id", "note", "score"], [c["name"] for c in first["columns"]])
        self.assertEqual([["1", "two\nlines", "3.5"], ["2", "plain", "4"]], first["rows"])
        self.assertEqual("integer", first["columns"][0]["type"])
        self.assertEqual("number", first["columns"][2]["type"])
        self.assertTrue(first["has_more"])

        second = build_preview(
            read, {"path": "o/a.csv", "filename": "a.csv"}, offset=first["next_offset"], limit=2
        )
        self.assertEqual([["3", "x, y", "5"]], second["rows"])
        self.assertFalse(second["has_more"])
        self.assertIsNone(second["next_offset"])

    def test_csv_never_reads_more_than_the_window(self):
        rows = "".join(f"{index},value-{index}\n" for index in range(50_000))
        read = reader(("id,value\n" + rows).encode())
        with patch.dict("os.environ", {"INLUMEN_PREVIEW_MAX_BYTES": "4096"}):
            preview = build_preview(read, {"filename": "big.csv"}, limit=10)
        self.assertEqual(10, len(preview["rows"]))
        self.assertTrue(all(length is not None and length <= 65536 for _offset, length in read.calls))

    def test_partial_last_row_moves_to_next_page(self):
        body = b"a,b\n" + b"1,2\n" * 3 + b'4,"unfinished\nvalue"\n'
        with patch.dict("os.environ", {"INLUMEN_PREVIEW_MAX_BYTES": "20"}):
            preview = build_preview(reader(body), {"filename": "t.csv"})
        self.assertEqual([["1", "2"]] * 3, preview["rows"])
        self.assertEqual(16, preview["next_offset"])
        self.assertTrue(preview["has_more"])

    def test_tsv_and_bom(self):
        preview = build_preview(reader("﻿name\tage\nAda\t36\n".encode()), {"filename": "p.tsv"})
        self.assertEqual(["name", "age"], [c["name"] for c in preview["columns"]])
        self.assertEqual([["Ada", "36"]], preview["rows"])

    def test_json_is_parsed_below_the_size_limit_and_windowed_above(self):
        small = build_preview(reader(b'{"a": [1, 2]}'), {"filename": "x.json"})
        self.assertEqual({"a": [1, 2]}, small["value"])

        big = json.dumps({"rows": list(range(5000))}).encode()
        with patch.dict("os.environ", {"INLUMEN_PREVIEW_JSON_MAX_BYTES": "100"}):
            windowed = build_preview(reader(big), {"filename": "x.json"})
        self.assertEqual("text", windowed["kind"])
        self.assertNotIn("value", windowed)

    def test_json_lines_become_table_rows(self):
        body = b'{"a": 1, "b": {"c": 2}}\n{"a": 3}\n'
        preview = build_preview(reader(body), {"filename": "x.jsonl"})
        self.assertEqual(["a", "b"], [c["name"] for c in preview["columns"]])
        self.assertEqual([["1", '{"c": 2}'], ["3", ""]], preview["rows"])

    def test_text_windows_stop_at_a_line_boundary(self):
        body = b"line one\nline two\nline three\n"
        preview = build_preview(reader(body), {"filename": "x.log"}, limit=12)
        self.assertEqual("line one\n", preview["text"])
        self.assertEqual(9, preview["next_offset"])

    def test_media_is_marked_inline_when_small(self):
        preview = build_preview(reader(b"\x89PNG...."), {"filename": "p.png", "content_type": "image/png"})
        self.assertEqual("image", preview["kind"])
        self.assertTrue(preview["inline"])
        self.assertNotIn("text", preview)

    def test_binary_and_unknown_text(self):
        self.assertEqual(
            "binary", build_preview(reader(b"\x00\x01\x02"), {"filename": "m.bin"})["kind"]
        )
        self.assertEqual(
            "text", build_preview(reader(b"plain words"), {"filename": "notes"})["kind"]
        )


class RunOutputPreviewRouteTest(unittest.TestCase):
    def setUp(self):
        import inlumen_api

        self.api = inlumen_api
        self.client = inlumen_api.app.test_client()

    def test_preview_reads_only_outputs_listed_in_the_run(self):
        run = {"result": {"outputs": [{"path": "outputs/n/a.csv", "filename": "a.csv"}]}}

        class Headers(dict):
            pass

        def raw(method, path, *, params=None):
            body = b"x,y\n1,2\n"[params["offset"]:][: params.get("length")]
            return body, Headers({"X-InLumen-Output-Size": "8"})

        with patch.object(self.api, "runner_request", return_value=run), patch.object(
            self.api, "runner_raw_response", side_effect=raw
        ) as raw_mock:
            response = self.client.get("/api/pipeline-runs/run-1/preview/outputs/n/a.csv")
            missing = self.client.get("/api/pipeline-runs/run-1/preview/outputs/n/other.csv")

        self.assertEqual(200, response.status_code, response.json)
        self.assertEqual([["1", "2"]], response.json["rows"])
        self.assertEqual(
            "/v1/pipeline-runs/run-1/outputs/outputs/n/a.csv", raw_mock.call_args.args[1]
        )
        self.assertEqual(404, missing.status_code)

    def test_preview_rejects_bad_paging_arguments(self):
        response = self.client.get("/api/pipeline-runs/run-1/preview/a.csv?offset=-1")
        self.assertEqual(400, response.status_code)

    def test_inline_output_is_sandboxed(self):
        with patch.object(
            self.api, "runner_raw_request", return_value=(b"<svg/>", "image/svg+xml", 'inline; filename="a.svg"')
        ) as raw:
            response = self.client.get(
                "/api/pipeline-runs/run-1/outputs/outputs/n/a.svg?disposition=inline"
            )
        self.assertEqual(200, response.status_code)
        self.assertEqual({"disposition": "inline"}, raw.call_args.kwargs["params"])
        self.assertIn("sandbox", response.headers["Content-Security-Policy"])
        self.assertEqual("nosniff", response.headers["X-Content-Type-Options"])
        bad = self.client.get("/api/pipeline-runs/run-1/outputs/a.svg?disposition=other")
        self.assertEqual(400, bad.status_code)


if __name__ == "__main__":
    unittest.main()
