import os
import re
import json
from pathlib import Path

INPUT_DIR = Path(os.environ["PIPELINE_INPUT_DIR"])
OUTPUT_DIR = Path(os.environ["PIPELINE_OUTPUT_DIR"])
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

PATTERNS = [
    ("EMAIL", re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)),
    ("PHONE", re.compile(r"(?<!\w)(?:\+?\d[\d\s().-]{6,}\d)(?!\w)")),
    ("IP", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
    ("URL", re.compile(r"\bhttps?://[^\s]+", re.I)),
    ("CARD", re.compile(r"\b(?:\d[ -]*?){13,19}\b")),
]

def anonymize(text):
    counts = {}
    for label, pattern in PATTERNS:
        def repl(match, label=label):
            counts[label] = counts.get(label, 0) + 1
            return f"[{label}_{counts[label]}]"
        text = pattern.sub(repl, text)
    return text


def bound_input():
    manifest = json.loads(Path(os.environ["INLUMEN_INPUT_MANIFEST"]).read_text(encoding="utf-8"))
    inputs = manifest.get("inputs", [])
    if len(inputs) != 1:
        raise RuntimeError(f"Expected exactly one bound artifact, received {len(inputs)}")
    path = Path(inputs[0]["path"])
    if not path.is_file():
        raise RuntimeError("Expected one file artifact")
    return path

def main():
    json_files = [bound_input()]

    for path in json_files:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("text"), str):
            raise RuntimeError("Bound transcript must contain a text string")
        data["text"] = anonymize(str(data.get("text", "")))
        # Avoid leaking original segment text after anonymization.
        data.pop("segments", None)
        data["anonymized"] = True

        out = OUTPUT_DIR / "anonymized.json"
        out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

if __name__ == "__main__":
    main()
