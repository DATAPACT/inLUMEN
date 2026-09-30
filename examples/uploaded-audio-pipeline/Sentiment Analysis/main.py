import os
import json
from pathlib import Path

INPUT_DIR = Path(os.environ["PIPELINE_INPUT_DIR"])
OUTPUT_DIR = Path(os.environ["PIPELINE_OUTPUT_DIR"])
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


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
    from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

    analyzer = SentimentIntensityAnalyzer()
    json_files = [bound_input()]

    for path in json_files:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("text"), str):
            raise RuntimeError("Bound transcript must contain a text string")
        scores = analyzer.polarity_scores(str(data.get("text", "")))
        compound = scores["compound"]
        label = "positive" if compound >= 0.05 else "negative" if compound <= -0.05 else "neutral"

        data["sentiment"] = {
            "label": label,
            "compound": compound,
            "scores": scores,
        }

        out = OUTPUT_DIR / "sentiment.json"
        out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

if __name__ == "__main__":
    main()
