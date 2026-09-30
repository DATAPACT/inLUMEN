import os
import json
from pathlib import Path

INPUT_DIR = Path(os.environ["PIPELINE_INPUT_DIR"])
OUTPUT_DIR = Path(os.environ["PIPELINE_OUTPUT_DIR"])
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".webm", ".mp4", ".mpeg", ".mpga"}


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
    from faster_whisper import WhisperModel

    audio_path = bound_input()
    if audio_path.suffix.lower() not in AUDIO_EXTENSIONS:
        raise RuntimeError("Bound artifact is not a supported audio file")
    audio_files = [audio_path]

    model_name = os.getenv("WHISPER_MODEL", "base")
    device = os.getenv("WHISPER_DEVICE", "cpu")
    compute_type = os.getenv("WHISPER_COMPUTE_TYPE", "int8")
    language = os.getenv("WHISPER_LANGUAGE") or None

    model = WhisperModel(model_name, device=device, compute_type=compute_type)

    for audio_path in audio_files:
        segments, info = model.transcribe(audio_path, language=language, vad_filter=True)
        segments = list(segments)
        text = " ".join(s.text.strip() for s in segments).strip()

        result = {
            "source_file": audio_path.name,
            "language": getattr(info, "language", None),
            "language_probability": getattr(info, "language_probability", None),
            "text": text,
            "segments": [
                {"start": s.start, "end": s.end, "text": s.text.strip()}
                for s in segments
            ],
        }

        out = OUTPUT_DIR / "transcript.json"
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

if __name__ == "__main__":
    main()
