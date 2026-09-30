import json
import os
import re
from pathlib import Path

INPUT_DIR = Path(os.environ["PIPELINE_INPUT_DIR"])
OUTPUT_DIR = Path(os.environ["PIPELINE_OUTPUT_DIR"])
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_FILE = OUTPUT_DIR / "result.json"

POSITIVE = {
    "good","great","excellent","amazing","awesome","happy","love","like","liked","helpful",
    "positive","perfect","wonderful","best","better","success","successful","satisfied",
    "enjoy","enjoyed","fantastic","nice","glad","thanks","thank","appreciate","recommend"
}
NEGATIVE = {
    "bad","terrible","awful","horrible","sad","hate","dislike","negative","worst","worse",
    "fail","failed","failure","angry","annoyed","problem","problems","issue","issues",
    "broken","poor","disappointed","disappointing","frustrated","frustrating","cancel"
}
NEGATIONS = {"not","no","never","neither","hardly","without"}

def sentiment(text):
    tokens = re.findall(r"[A-Za-z']+", text.lower())
    score = 0
    hits = 0
    for i, token in enumerate(tokens):
        val = 1 if token in POSITIVE else -1 if token in NEGATIVE else 0
        if val:
            if any(t in NEGATIONS for t in tokens[max(0, i-3):i]):
                val *= -1
            score += val
            hits += 1
    normalized = score / hits if hits else 0.0
    if normalized > 0.15:
        label = "positive"
    elif normalized < -0.15:
        label = "negative"
    else:
        label = "neutral"
    return {"label": label, "score": round(normalized, 4), "matched_terms": hits}

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

def main():
    out = []
    for rec in load_records():
        rec = dict(rec)
        rec["sentiment"] = sentiment(str(rec.get("text", "")))
        out.append(rec)

    payload = {
        "schema_version": 1,
        "task": "sentiment_analysis",
        "file_count": len(out),
        "records": out,
    }
    OUTPUT_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

if __name__ == "__main__":
    main()
