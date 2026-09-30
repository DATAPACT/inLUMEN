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
    import spacy

    model_name = os.getenv("SPACY_MODEL", "en_core_web_sm")
    try:
        nlp = spacy.load(model_name)
    except OSError as e:
        raise RuntimeError(
            f"spaCy model '{model_name}' is unavailable. "
            "The default requirements install en_core_web_sm."
        ) from e

    json_files = [bound_input()]

    for path in json_files:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("text"), str):
            raise RuntimeError("Bound transcript must contain a text string")
        text = str(data.get("text", ""))
        doc = nlp(text)
        data["entities"] = [
            {
                "text": ent.text,
                "label": ent.label_,
                "start": ent.start_char,
                "end": ent.end_char,
            }
            for ent in doc.ents
        ]

        out = OUTPUT_DIR / "entities.json"
        out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

if __name__ == "__main__":
    main()
