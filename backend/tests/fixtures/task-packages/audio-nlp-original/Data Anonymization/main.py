import json
import os
import re
from pathlib import Path

INPUT_DIR = Path(os.environ["PIPELINE_INPUT_DIR"])
OUTPUT_DIR = Path(os.environ["PIPELINE_OUTPUT_DIR"])
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_FILE = OUTPUT_DIR / "anonymized.json"

PATTERNS = [
    ("EMAIL", re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)),
    ("URL", re.compile(r"\bhttps?://[^\s<>()]+", re.I)),
    ("IP", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
    ("PHONE", re.compile(r"(?<!\w)(?:\+?\d[\d .()/-]{6,}\d)(?!\w)")),
    ("CARD", re.compile(r"\b(?:\d[ -]*?){13,19}\b")),
]

def luhn_candidate(value):
    digits = re.sub(r"\D", "", value)
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    parity = len(digits) % 2
    for i, ch in enumerate(digits):
        n = int(ch)
        if i % 2 == parity:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0

def anonymize(text):
    counts = {}
    result = text
    for label, pattern in PATTERNS:
        def repl(match):
            if label == "CARD" and not luhn_candidate(match.group(0)):
                return match.group(0)
            counts[label] = counts.get(label, 0) + 1
            return f"[{label}_{counts[label]}]"
        result = pattern.sub(repl, result)
    return result, counts

def load_input():
    json_files = sorted(p for p in INPUT_DIR.iterdir() if p.is_file() and p.suffix.lower() == ".json")
    if not json_files:
        raise RuntimeError(f"No JSON input found directly in {INPUT_DIR}")
    # Supports multiple upstream JSON files if the task is reused independently.
    records = []
    for path in json_files:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("records"), list):
            records.extend(data["records"])
        elif isinstance(data, list):
            records.extend(data)
        elif isinstance(data, dict):
            records.append(data)
    return records

def main():
    output_records = []
    for i, rec in enumerate(load_input()):
        rec = dict(rec)
        original = str(rec.get("text", ""))
        clean, counts = anonymize(original)
        rec["text"] = clean
        rec["anonymization"] = {"replacement_counts": counts}
        # Segment text may contain PII too, so remove raw segment text from downstream payload.
        rec.pop("segments", None)
        output_records.append(rec)

    payload = {
        "schema_version": 1,
        "task": "data_anonymization",
        "file_count": len(output_records),
        "records": output_records,
    }
    OUTPUT_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

if __name__ == "__main__":
    main()
