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
        fail(f"Input audio file does not exist: {p}")
    return p, matches[0]


def main():
    audio, desc = get_input("input")
    outdir = Path(os.environ["PIPELINE_OUTPUT_DIR"])
    outdir.mkdir(parents=True, exist_ok=True)

    # faster-whisper performs real Whisper ASR locally. Model files are expected
    # to be prepared by the runtime from the model declaration.
    try:
        from faster_whisper import WhisperModel
    except Exception as e:
        fail(f"Cannot import faster-whisper: {e}")

    model_ref = os.environ.get("INLUMEN_MODEL_0_PATH") or os.environ.get("WHISPER_MODEL_PATH") or "Systran/faster-whisper-small"
    try:
        model = WhisperModel(model_ref, device="cpu", compute_type="int8")
        segments_iter, info = model.transcribe(str(audio), beam_size=5, vad_filter=True)
        segments = []
        text_parts = []
        for s in segments_iter:
            txt = s.text.strip()
            if txt:
                text_parts.append(txt)
            segments.append({"start": float(s.start), "end": float(s.end), "text": txt})
    except Exception as e:
        fail(f"Audio transcription failed: {e}")

    transcript = " ".join(text_parts).strip()
    if not transcript:
        fail("Transcription produced no text")

    result = {
        "source": {
            "filename": desc.get("filename") or audio.name,
            "representation": desc.get("representation")
        },
        "transcription": {
            "text": transcript,
            "language": getattr(info, "language", None),
            "language_probability": float(getattr(info, "language_probability", 0.0)),
            "duration_seconds": float(getattr(info, "duration", 0.0)),
            "segments": segments
        }
    }
    (outdir / "transcription.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

if __name__ == "__main__":
    main()
