import json, os, sys
from pathlib import Path


def fail(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    raise SystemExit(1)


def get_input(port):
    manifest_path = os.environ.get("INLUMEN_INPUT_MANIFEST")
    if not manifest_path:
        fail("INLUMEN_INPUT_MANIFEST is not set")
    try:
        manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    except Exception as e:
        fail(f"Cannot read input manifest: {e}")
    matches = [x for x in manifest.get("inputs", []) if x.get("target_port") == port]
    if len(matches) != 1:
        fail(f"Expected exactly one input for port '{port}', found {len(matches)}")
    p = Path(matches[0]["path"])
    if not p.is_file():
        fail(f"Input file does not exist: {p}")
    return p


def main():
    inp = get_input("input")
    outdir = Path(os.environ["PIPELINE_OUTPUT_DIR"])
    outdir.mkdir(parents=True, exist_ok=True)
    try:
        payload = json.loads(inp.read_text(encoding="utf-8"))
        text = payload["transcription"]["text"]
    except Exception as e:
        fail(f"Invalid transcription JSON: {e}")
    if not isinstance(text, str) or not text.strip():
        fail("transcription.text must be a non-empty string")

    try:
        from transformers import pipeline
    except Exception as e:
        fail(f"Cannot import transformers: {e}")

    model_ref = os.environ.get("INLUMEN_MODEL_0_PATH") or os.environ.get("SENTIMENT_MODEL_PATH") or "cardiffnlp/twitter-roberta-base-sentiment-latest"
    try:
        clf = pipeline("sentiment-analysis", model=model_ref, tokenizer=model_ref, device=-1)
        pred = clf(text, truncation=True, max_length=512)[0]
    except Exception as e:
        fail(f"Sentiment analysis failed: {e}")

    label = str(pred["label"]).lower()
    confidence = float(pred["score"])
    signed = confidence if label == "positive" else (-confidence if label == "negative" else 0.0)
    payload["sentiment"] = {
        "label": label,
        "confidence": confidence,
        "score": signed,
        "score_scale": "-1 negative to +1 positive; neutral = 0"
    }
    (outdir / "analysis.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

if __name__ == "__main__":
    main()
