import json
import os
import re
from pathlib import Path

INPUT_DIR = Path(os.environ["PIPELINE_INPUT_DIR"])
OUTPUT_DIR = Path(os.environ["PIPELINE_OUTPUT_DIR"])
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_FILE = OUTPUT_DIR / "entities.json"

# Lightweight offline NER suitable for a self-contained bundle. It identifies common
# structured entities plus conservative proper-name candidates.
DATE_RE = re.compile(
    r"\b(?:\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|"
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
    r"\s+\d{1,2}(?:,\s*\d{4})?)\b", re.I
)
TIME_RE = re.compile(r"\b(?:[01]?\d|2[0-3]):[0-5]\d(?:\s?(?:am|pm))?\b", re.I)
MONEY_RE = re.compile(r"(?<!\w)(?:[$€£]\s?\d+(?:[.,]\d+)*|\d+(?:[.,]\d+)*\s?(?:USD|EUR|NOK|GBP))\b", re.I)
PERCENT_RE = re.compile(r"\b\d+(?:[.,]\d+)?\s?%")
PROPER_RE = re.compile(r"\b(?:[A-Z][a-z]{2,})(?:\s+[A-Z][a-z]{2,}){0,3}\b")
PLACEHOLDER_RE = re.compile(r"\[(?:EMAIL|URL|IP|PHONE|CARD)_\d+\]")

def load_records():
    files = sorted(p for p in INPUT_DIR.iterdir() if p.is_file() and p.suffix.lower() == ".json")
    if not files:
        raise RuntimeError(f"No JSON input found directly in {INPUT_DIR}")
    records = []
    for path in files:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("records"), list):
            records.extend(data["records"])
        elif isinstance(data, list):
            records.extend(data)
        elif isinstance(data, dict):
            records.append(data)
    return records

def extract(text):
    entities = []
    occupied = []

    def add_matches(regex, label):
        for m in regex.finditer(text):
            span = (m.start(), m.end())
            if any(not (span[1] <= a or span[0] >= b) for a, b in occupied):
                continue
            entities.append({"text": m.group(0), "label": label, "start": m.start(), "end": m.end()})
            occupied.append(span)

    # Do not expose anonymization placeholders as named entities.
    for m in PLACEHOLDER_RE.finditer(text):
        occupied.append((m.start(), m.end()))

    add_matches(DATE_RE, "DATE")
    add_matches(TIME_RE, "TIME")
    add_matches(MONEY_RE, "MONEY")
    add_matches(PERCENT_RE, "PERCENT")
    add_matches(PROPER_RE, "PROPER_NAME")

    entities.sort(key=lambda x: (x["start"], x["end"]))
    return entities

def main():
    out = []
    for rec in load_records():
        rec = dict(rec)
        rec["entities"] = extract(str(rec.get("text", "")))
        out.append(rec)

    payload = {
        "schema_version": 1,
        "task": "ner_tagging",
        "file_count": len(out),
        "records": out,
    }
    OUTPUT_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

if __name__ == "__main__":
    main()
