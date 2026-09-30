import json
import os
from pathlib import Path

INPUT_DIR = Path(os.environ["PIPELINE_INPUT_DIR"])
OUTPUT_DIR = Path(os.environ["PIPELINE_OUTPUT_DIR"])
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_FILE = OUTPUT_DIR / "transcription.json"

AUDIO_EXTENSIONS = {
    ".wav", ".mp3", ".m4a", ".flac", ".ogg", ".oga", ".opus",
    ".aac", ".wma", ".webm", ".mp4", ".mov", ".mkv"
}

def audio_files():
    return sorted(
        p for p in INPUT_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS
    )

def main():
    files = audio_files()
    if not files:
        raise RuntimeError(
            f"No supported audio/video files found directly in {INPUT_DIR}. "
            f"Supported extensions: {', '.join(sorted(AUDIO_EXTENSIONS))}"
        )

    # Import only at runtime so configuration errors remain easy to diagnose.
    from faster_whisper import WhisperModel

    model_name = os.getenv("WHISPER_MODEL", "small")
    device = os.getenv("WHISPER_DEVICE", "cpu")
    compute_type = os.getenv("WHISPER_COMPUTE_TYPE", "int8" if device == "cpu" else "float16")
    language = os.getenv("WHISPER_LANGUAGE") or None
    beam_size = int(os.getenv("WHISPER_BEAM_SIZE", "5"))

    model = WhisperModel(model_name, device=device, compute_type=compute_type)
    records = []

    for path in files:
        segments, info = model.transcribe(
            str(path),
            language=language,
            beam_size=beam_size,
            vad_filter=True,
        )
        segs = []
        text_parts = []
        for s in segments:
            txt = s.text.strip()
            if txt:
                text_parts.append(txt)
            segs.append({
                "start": round(float(s.start), 3),
                "end": round(float(s.end), 3),
                "text": txt,
            })

        records.append({
            "source_file": path.name,
            "language": getattr(info, "language", None),
            "language_probability": round(float(getattr(info, "language_probability", 0.0)), 4),
            "text": " ".join(text_parts).strip(),
            "segments": segs,
        })

    payload = {
        "schema_version": 1,
        "task": "speech_to_text",
        "file_count": len(records),
        "records": records,
    }
    OUTPUT_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

if __name__ == "__main__":
    main()
