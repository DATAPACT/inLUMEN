from __future__ import annotations

import ast
import re
import textwrap
from typing import Any

from .model_plans import trusted_adapter_id
from .schemas import is_input_node_kind, is_output_node_kind


def apply_trusted_adapters(source: str, plan: dict[str, Any]) -> str:
    """Replace recognized tasks with compiler-owned compatible adapters."""
    try:
        tree = ast.parse(source, filename="pipeline.py")
    except SyntaxError:
        return source

    replacements: dict[str, ast.FunctionDef] = {}
    for node in plan.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        implementation = node.get("implementation_plan")
        adapter_id = trusted_adapter_id(implementation)
        task_profile = node.get("task_profile")
        task_name = (
            str(task_profile.get("name") or "")
            if isinstance(task_profile, dict)
            else ""
        )
        descriptor = (
            dict(node.get("descriptor") or {})
            if isinstance(node.get("descriptor"), dict)
            else {}
        )
        if is_input_node_kind(str(descriptor.get("type") or "")):
            replacement_source = input_boundary_function_source(node)
        elif is_output_node_kind(str(descriptor.get("type") or "")):
            replacement_source = output_boundary_function_source(node)
        elif task_name == "audio_preprocessing":
            replacement_source = audio_preprocessing_function_source(node)
        elif adapter_id == "faster-whisper":
            replacement_source = faster_whisper_function_source(node)
        elif adapter_id == "transformers-roberta-sentiment":
            replacement_source = roberta_sentiment_function_source(node)
        else:
            continue
        parsed = ast.parse(replacement_source, filename="trusted_adapter.py")
        function = next(
            item for item in parsed.body if isinstance(item, ast.FunctionDef)
        )
        replacements[str(node.get("function_name") or "")] = function

    if not replacements:
        return source

    replaced: set[str] = set()
    body: list[ast.stmt] = []
    for statement in tree.body:
        if isinstance(statement, ast.FunctionDef) and statement.name in replacements:
            body.append(replacements[statement.name])
            replaced.add(statement.name)
        else:
            body.append(statement)
    for function_name, replacement in replacements.items():
        if function_name not in replaced:
            body.append(replacement)
    tree.body = _remove_unused_imports(body)
    ast.fix_missing_locations(tree)
    return ast.unparse(tree).strip() + "\n"


def input_boundary_function_source(node: dict[str, Any]) -> str:
    """Return the compiler-owned adapter for a persisted pipeline input."""
    output = _primary_output(node, "source-output.bin")
    function_name = str(node["function_name"])
    return textwrap.dedent(
        f"""
        def {function_name}(inputs, output_dir, context):
            import base64
            import json
            import shutil
            from pathlib import Path

            output = {output!r}
            candidates = [
                (item, Path(str(item.get("path") or "")))
                for item in inputs
                if isinstance(item, dict)
            ]
            readable = [(item, path) for item, path in candidates if path.is_file()]
            if not readable:
                raise FileNotFoundError(
                    "The source adapter did not receive a readable input artifact."
                )
            output_path = Path(output_dir) / str(output["filename"])
            output_path.parent.mkdir(parents=True, exist_ok=True)
            if (
                len(readable) > 1
                and (
                    str(output.get("kind") or "") == "json"
                    or str(output.get("format") or "") == "json"
                )
            ):
                package = {{"files": []}}
                json_documents = []
                for item, path in readable:
                    filename = str(item.get("filename") or path.name)
                    kind = str(item.get("kind") or "binary")
                    file_format = str(item.get("format") or path.suffix.lstrip(".")).lower()
                    package["files"].append({{
                        "filename": filename,
                        "kind": kind,
                        "format": file_format,
                        "size_bytes": path.stat().st_size,
                    }})
                    if file_format == "pdf":
                        package.setdefault(
                            "pdf_base64",
                            base64.b64encode(path.read_bytes()).decode("ascii"),
                        )
                        package.setdefault("source", filename)
                    elif kind == "json" or file_format == "json":
                        value = json.loads(path.read_text(encoding="utf-8"))
                        json_documents.append({{"filename": filename, "data": value}})
                        if isinstance(value, dict):
                            for key, nested_value in value.items():
                                package.setdefault(str(key), nested_value)
                    elif kind == "text" or file_format in {{"txt", "md"}}:
                        package.setdefault("text", path.read_text(encoding="utf-8"))
                    else:
                        package.setdefault(
                            "content_base64",
                            base64.b64encode(path.read_bytes()).decode("ascii"),
                        )
                if json_documents:
                    package["json_documents"] = json_documents
                output_path.write_text(
                    json.dumps(package, indent=2, sort_keys=True) + "\\n",
                    encoding="utf-8",
                )
            else:
                shutil.copy2(readable[0][1], output_path)
            return [{{**output, "path": str(output_path)}}]
        """
    ).strip()


def output_boundary_function_source(node: dict[str, Any]) -> str:
    """Return a side-effect-free validation adapter for a managed destination."""
    output = _primary_output(node, "delivery-receipt.json")
    descriptor = (
        dict(node.get("descriptor") or {})
        if isinstance(node.get("descriptor"), dict)
        else {}
    )
    destination_label = str(descriptor.get("label") or node.get("flow_id") or "destination")
    function_name = str(node["function_name"])
    return textwrap.dedent(
        f"""
        def {function_name}(inputs, output_dir, context):
            import json
            import shutil
            from pathlib import Path

            output = {output!r}
            output_path = Path(output_dir) / str(output["filename"])
            output_path.parent.mkdir(parents=True, exist_ok=True)
            if str(output.get("kind") or "") == "json" or str(output.get("format") or "") == "json":
                payload = {{
                    "status": "delivered",
                    "destination": {destination_label!r},
                    "artifacts": [
                        {{
                            "name": item.get("name"),
                            "filename": item.get("filename"),
                            "kind": item.get("kind"),
                            "format": item.get("format"),
                            "path": item.get("path"),
                        }}
                        for item in inputs
                        if isinstance(item, dict)
                    ],
                }}
                schema = output.get("schema") if isinstance(output.get("schema"), dict) else {{}}
                properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {{}}
                for key in schema.get("required") or []:
                    if key in payload:
                        continue
                    property_schema = properties.get(key) if isinstance(properties.get(key), dict) else {{}}
                    schema_type = property_schema.get("type")
                    payload[key] = (
                        [] if schema_type == "array"
                        else {{}} if schema_type == "object"
                        else False if schema_type == "boolean"
                        else 0 if schema_type in {{"integer", "number"}}
                        else ""
                    )
                output_path.write_text(
                    json.dumps(payload, indent=2, sort_keys=True) + "\\n",
                    encoding="utf-8",
                )
            else:
                source_path = next(
                    (
                        Path(str(item.get("path") or ""))
                        for item in inputs
                        if isinstance(item, dict)
                        and Path(str(item.get("path") or "")).is_file()
                    ),
                    None,
                )
                if source_path is None:
                    raise FileNotFoundError(
                        "The destination adapter did not receive a readable artifact."
                    )
                shutil.copy2(source_path, output_path)
            return [{{**output, "path": str(output_path)}}]
        """
    ).strip()


def audio_preprocessing_function_source(node: dict[str, Any]) -> str:
    """Return bounded, shape-safe DSP for an explicit preprocessing node."""
    descriptor = (
        dict(node.get("descriptor") or {})
        if isinstance(node.get("descriptor"), dict)
        else {}
    )
    parameters = (
        dict(descriptor.get("parameters") or {})
        if isinstance(descriptor.get("parameters"), dict)
        else {}
    )
    request_text = " ".join(
        str(descriptor.get(key) or "") for key in ("label", "description")
    ).lower()

    target_sample_rate = _requested_sample_rate(request_text, parameters)
    config = {
        "target_sample_rate": target_sample_rate,
        "convert_to_mono": (
            "mono" in request_text
            or bool(parameters.get("convert_to_mono"))
            or bool(parameters.get("mono"))
        ),
        "denoise": any(
            token in request_text
            for token in ("denoise", "noise reduction", "remove noise", "clean")
        )
        or bool(parameters.get("denoise")),
        "normalize": any(
            token in request_text
            for token in ("normalize", "normalise", "gain", "amplitude")
        )
        or bool(parameters.get("normalize")),
        "trim_silence": any(
            token in request_text
            for token in ("trim", "silence", "voice activity", "vad")
        )
        or bool(parameters.get("trim_silence"))
        or bool(parameters.get("vad")),
        "filter": any(
            token in request_text
            for token in ("bandpass", "band-pass", "highpass", "high-pass", "filter")
        )
        or bool(parameters.get("filter")),
        "low_cutoff_hz": float(parameters.get("low_cutoff_hz") or 80.0),
        "high_cutoff_hz": (
            float(parameters["high_cutoff_hz"])
            if parameters.get("high_cutoff_hz") is not None
            else None
        ),
    }
    output = _primary_output(node, "prepared_audio.wav")
    function_name = str(node["function_name"])
    return textwrap.dedent(
        f"""
        def {function_name}(inputs, output_dir, context):
            import math
            from pathlib import Path

            import numpy as np
            import soundfile as sf
            from scipy import signal

            config = {config!r}
            audio_extensions = {{".wav", ".wave", ".mp3", ".m4a", ".aac", ".flac", ".ogg"}}
            candidates = [
                Path(str(item.get("path") or ""))
                for item in inputs
                if isinstance(item, dict)
                and Path(str(item.get("path") or "")).suffix.lower() in audio_extensions
            ]
            input_path = next((path for path in candidates if path.is_file()), None)
            if input_path is None:
                raise FileNotFoundError(
                    "No readable audio artifact was supplied to the preprocessing node."
                )

            samples, sample_rate = sf.read(
                input_path,
                dtype="float32",
                always_2d=True,
            )
            if samples.size == 0 or not np.isfinite(samples).all():
                raise ValueError("Input audio is empty or contains non-finite samples.")
            input_duration = samples.shape[0] / float(sample_rate)

            if config["convert_to_mono"]:
                waveform = np.mean(samples, axis=1, dtype=np.float64)
            elif samples.shape[1] == 1:
                waveform = samples[:, 0].astype(np.float64, copy=False)
            else:
                raise ValueError(
                    "The requested preprocessing keeps multiple channels, but the "
                    "current WAV contract supports one output channel. Request mono "
                    "conversion or use a multi-channel output contract."
                )
            if waveform.ndim != 1:
                raise ValueError(
                    f"Audio preprocessing requires a 1-D waveform; got {{waveform.shape}}."
                )

            target_sample_rate = config["target_sample_rate"]
            if target_sample_rate and sample_rate != target_sample_rate:
                divisor = math.gcd(int(sample_rate), int(target_sample_rate))
                waveform = signal.resample_poly(
                    waveform,
                    int(target_sample_rate) // divisor,
                    int(sample_rate) // divisor,
                    axis=0,
                )
                sample_rate = int(target_sample_rate)

            if config["filter"] and waveform.size:
                nyquist = sample_rate / 2.0
                low_cutoff = float(config["low_cutoff_hz"])
                requested_high = config["high_cutoff_hz"]
                high_cutoff = (
                    float(requested_high)
                    if requested_high is not None
                    else nyquist * 0.95
                )
                if not 0.0 < low_cutoff < nyquist:
                    raise ValueError(
                        f"Invalid low cutoff {{low_cutoff}}Hz for {{sample_rate}}Hz audio."
                    )
                if low_cutoff < high_cutoff < nyquist:
                    sos = signal.butter(
                        4,
                        [low_cutoff, high_cutoff],
                        btype="bandpass",
                        fs=sample_rate,
                        output="sos",
                    )
                else:
                    sos = signal.butter(
                        4,
                        low_cutoff,
                        btype="highpass",
                        fs=sample_rate,
                        output="sos",
                    )
                waveform = (
                    signal.sosfiltfilt(sos, waveform)
                    if waveform.size > 64
                    else signal.sosfilt(sos, waveform)
                )

            if config["denoise"] and waveform.size:
                segment_size = min(1024, int(waveform.size))
                overlap = segment_size // 2
                _, _, spectrum = signal.stft(
                    waveform,
                    fs=sample_rate,
                    nperseg=segment_size,
                    noverlap=overlap,
                    boundary="zeros",
                    padded=True,
                    axis=0,
                )
                if spectrum.size:
                    noise_frames = max(
                        1,
                        min(
                            spectrum.shape[1],
                            int(math.ceil(0.5 * sample_rate / max(1, overlap))),
                        ),
                    )
                    noise_floor = np.median(
                        np.abs(spectrum[:, :noise_frames]),
                        axis=1,
                        keepdims=True,
                    )
                    magnitude = np.abs(spectrum)
                    gain = np.maximum(
                        0.0,
                        1.0 - (1.25 * noise_floor / np.maximum(magnitude, 1e-12)),
                    )
                    spectrum = spectrum * gain
                    _, waveform = signal.istft(
                        spectrum,
                        fs=sample_rate,
                        nperseg=segment_size,
                        noverlap=overlap,
                        input_onesided=True,
                        boundary=True,
                        time_axis=-1,
                        freq_axis=0,
                    )

            if config["trim_silence"] and waveform.size:
                frame_size = max(1, int(round(0.025 * sample_rate)))
                hop_size = max(1, int(round(0.010 * sample_rate)))
                frame_starts = np.arange(
                    0,
                    max(1, waveform.size - frame_size + 1),
                    hop_size,
                    dtype=np.int64,
                )
                energies = np.asarray([
                    float(np.mean(np.square(waveform[start:start + frame_size])))
                    for start in frame_starts
                ])
                threshold = max(
                    float(np.max(energies)) * 0.01 if energies.size else 0.0,
                    1e-10,
                )
                active = np.flatnonzero(energies > threshold)
                if active.size:
                    padding = int(round(0.15 * sample_rate))
                    start = max(0, int(frame_starts[active[0]]) - padding)
                    end = min(
                        waveform.size,
                        int(frame_starts[active[-1]]) + frame_size + padding,
                    )
                    waveform = waveform[start:end]

            if config["normalize"] and waveform.size:
                peak = float(np.max(np.abs(waveform)))
                if peak > 0.0:
                    waveform = waveform * ((10.0 ** (-3.0 / 20.0)) / peak)

            waveform = np.asarray(waveform, dtype=np.float32)
            if (
                waveform.ndim != 1
                or waveform.size == 0
                or not np.isfinite(waveform).all()
            ):
                raise ValueError(
                    "Audio preprocessing produced an empty, non-finite, or "
                    "multi-dimensional waveform."
                )
            output_duration = waveform.size / float(sample_rate)
            if output_duration <= 0.0 or output_duration > input_duration * 1.05:
                raise ValueError(
                    "Audio preprocessing produced an invalid output duration: "
                    f"input={{input_duration:.3f}}s output={{output_duration:.3f}}s."
                )

            output_path = Path(output_dir) / {output["filename"]!r}
            output_path.parent.mkdir(parents=True, exist_ok=True)
            sf.write(
                output_path,
                waveform,
                sample_rate,
                subtype="PCM_16",
                format="WAV",
            )
            return [{{**{output!r}, "path": str(output_path)}}]
        """
    ).strip() + "\n"


def _requested_sample_rate(text: str, parameters: dict[str, Any]) -> int | None:
    configured = (
        parameters.get("target_sample_rate")
        or parameters.get("sample_rate")
        or parameters.get("sample_rate_hz")
    )
    if configured is not None:
        value = int(configured)
        if value <= 0:
            raise ValueError("Audio target sample rate must be positive.")
        return value
    match = re.search(r"(\d+(?:\.\d+)?)\s*(k)?hz\b", text)
    if match:
        value = float(match.group(1))
        return round(value * (1000 if match.group(2) else 1))
    if any(token in text for token in ("resample", "sample rate conversion")):
        return 16000
    return None


def faster_whisper_function_source(node: dict[str, Any]) -> str:
    implementation = dict(node.get("implementation_plan") or {})
    inference = dict(implementation.get("inference_parameters") or {})
    quality = dict(implementation.get("quality_policy") or {})
    output = _primary_output(node, "transcription.json")
    function_name = str(node["function_name"])
    return textwrap.dedent(
        f"""
        def {function_name}(inputs, output_dir, context):
            import hashlib
            import json
            import math
            import os
            import time
            from pathlib import Path

            import ctranslate2
            from faster_whisper import WhisperModel

            model_id = {implementation["model_id"]!r}
            model_revision = {implementation["model_revision"]!r}
            model_variants = {dict(implementation.get("model_variants") or {})!r}
            runtime_selection = {dict(implementation.get("runtime_selection") or {})!r}
            inference_parameters = {inference!r}
            quality_policy = {quality!r}
            started_at = time.monotonic()

            def report_progress(message):
                elapsed = time.monotonic() - started_at
                print(
                    f"[inlumen:asr] {{message}} (elapsed={{elapsed:.1f}}s)",
                    flush=True,
                )

            def resolve_local_model(reviewed_model_id, reviewed_revision):
                model_root = Path(
                    os.getenv("INLUMEN_MODEL_ROOT") or "/models"
                ).resolve()
                spec_sha256 = hashlib.sha256(
                    f"{{reviewed_model_id}}@{{reviewed_revision}}".encode("utf-8")
                ).hexdigest()
                artifact_dir = model_root / "artifacts" / spec_sha256
                manifest_path = artifact_dir / "inlumen-model-manifest.json"
                verified_path = artifact_dir / "VERIFIED"
                if not manifest_path.is_file() or not verified_path.is_file():
                    raise RuntimeError(
                        "Reviewed model is not available in the verified local model "
                        f"store: {{reviewed_model_id}}@{{reviewed_revision}}. Run the "
                        "generated model-prefetch service before pipeline execution."
                    )
                manifest_bytes = manifest_path.read_bytes()
                manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
                if verified_path.read_text(encoding="utf-8").strip() != manifest_sha256:
                    raise RuntimeError(
                        f"Local model manifest integrity check failed for {{reviewed_model_id}}."
                    )
                manifest = json.loads(manifest_bytes.decode("utf-8"))
                if (
                    manifest.get("schema_version") != "inlumen.model-artifact@1"
                    or manifest.get("model_id") != reviewed_model_id
                    or manifest.get("model_revision") != reviewed_revision
                    or manifest.get("spec_sha256") != spec_sha256
                ):
                    raise RuntimeError(
                        f"Local model identity mismatch for {{reviewed_model_id}}."
                    )
                snapshot_value = manifest.get("snapshot_path")
                if not isinstance(snapshot_value, str) or not snapshot_value.strip():
                    raise RuntimeError("Local model manifest has an unsafe snapshot path.")
                relative_snapshot = Path(snapshot_value)
                if relative_snapshot.is_absolute():
                    raise RuntimeError("Local model manifest has an unsafe snapshot path.")
                snapshot = (model_root / relative_snapshot).resolve()
                try:
                    snapshot.relative_to(model_root)
                except ValueError as exc:
                    raise RuntimeError(
                        "Local model snapshot escapes the configured model root."
                    ) from exc
                if not snapshot.is_dir():
                    raise RuntimeError(
                        f"Verified local model snapshot is missing: {{snapshot}}"
                    )
                tree_sha256 = str(manifest.get("tree_sha256") or "").lower()
                if len(tree_sha256) != 64 or any(
                    character not in "0123456789abcdef" for character in tree_sha256
                ):
                    raise RuntimeError("Local model tree SHA-256 is invalid.")
                return str(snapshot), tree_sha256

            def edit_distance(reference, hypothesis):
                previous = list(range(len(hypothesis) + 1))
                for ref_index, ref_item in enumerate(reference, start=1):
                    current = [ref_index]
                    for hyp_index, hyp_item in enumerate(hypothesis, start=1):
                        current.append(min(
                            current[-1] + 1,
                            previous[hyp_index] + 1,
                            previous[hyp_index - 1] + (ref_item != hyp_item),
                        ))
                    previous = current
                return previous[-1]

            def reference_transcript_from(item):
                if not isinstance(item, dict):
                    return ""
                for candidate in (
                    item.get("reference_transcript"),
                    (item.get("metadata") or {{}}).get("reference_transcript")
                    if isinstance(item.get("metadata"), dict) else "",
                    (item.get("sample") or {{}}).get("reference_transcript")
                    if isinstance(item.get("sample"), dict) else "",
                ):
                    if isinstance(candidate, str) and candidate.strip():
                        return candidate.strip()
                return ""

            audio_extensions = {{".wav", ".wave", ".mp3", ".m4a", ".aac", ".flac", ".ogg"}}
            candidates = [
                Path(str(item.get("path") or ""))
                for item in inputs
                if isinstance(item, dict)
                and Path(str(item.get("path") or "")).suffix.lower() in audio_extensions
            ]
            audio_path = next((path for path in candidates if path.is_file()), None)
            if audio_path is None:
                raise FileNotFoundError("No readable audio artifact was supplied to the ASR node.")

            configured_device = str(os.getenv("INLUMEN_ASR_DEVICE") or {implementation.get("device", "auto")!r}).lower()
            if configured_device == "auto":
                device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
            elif configured_device in {{"cpu", "cuda"}}:
                device = configured_device
            else:
                raise ValueError(f"Unsupported INLUMEN_ASR_DEVICE: {{configured_device}}")
            requested_profile = str(
                os.getenv("INLUMEN_ASR_PROFILE")
                or runtime_selection.get("default_profile")
                or "auto"
            ).lower()
            if requested_profile == "auto":
                requested_profile = str(
                    (runtime_selection.get("auto_profile_by_device") or {{}}).get(
                        device,
                        "accuracy",
                    )
                ).lower()
            if requested_profile not in model_variants:
                raise ValueError(
                    f"Unsupported INLUMEN_ASR_PROFILE: {{requested_profile}}"
                )
            selected_variant = dict(model_variants[requested_profile])
            model_id = str(selected_variant["model_id"])
            model_revision = str(selected_variant["model_revision"])
            compute_type = str(os.getenv("INLUMEN_ASR_COMPUTE_TYPE") or ("float16" if device == "cuda" else "int8"))
            cpu_threads = max(
                1,
                int(
                    os.getenv("INLUMEN_ASR_CPU_THREADS")
                    or inference_parameters.get("cpu_threads", 2)
                ),
            )
            num_workers = max(
                1,
                int(
                    os.getenv("INLUMEN_ASR_NUM_WORKERS")
                    or inference_parameters.get("num_workers", 1)
                ),
            )

            report_progress(f"loading verified local model {{model_id}}@{{model_revision}}")
            snapshot_path, model_tree_sha256 = resolve_local_model(
                model_id,
                model_revision,
            )
            report_progress(f"verified local model ready at {{snapshot_path}}")
            model = WhisperModel(
                snapshot_path,
                device=device,
                compute_type=compute_type,
                cpu_threads=cpu_threads if device == "cpu" else 0,
                num_workers=num_workers,
            )
            report_progress(
                f"model loaded on {{device}} with {{compute_type}} compute"
            )
            segments_iterator, info = model.transcribe(
                str(audio_path),
                beam_size=int(inference_parameters.get("beam_size", 5)),
                word_timestamps=bool(inference_parameters.get("word_timestamps", True)),
                vad_filter=bool(inference_parameters.get("vad_filter", True)),
                vad_parameters=dict(inference_parameters.get("vad_parameters") or {{}}),
                condition_on_previous_text=bool(
                    inference_parameters.get("condition_on_previous_text", True)
                ),
            )
            report_progress(f"transcribing {{audio_path.name}}")
            segments = []
            for segment in segments_iterator:
                words = []
                for word in (getattr(segment, "words", None) or []):
                    words.append({{
                        "start": float(word.start),
                        "end": float(word.end),
                        "word": str(word.word),
                        "probability": float(getattr(word, "probability", 0.0)),
                    }})
                segments.append({{
                    "id": int(getattr(segment, "id", len(segments))),
                    "start": float(segment.start),
                    "end": float(segment.end),
                    "text": str(segment.text).strip(),
                    "avg_logprob": float(getattr(segment, "avg_logprob", 0.0)),
                    "no_speech_probability": float(
                        getattr(segment, "no_speech_prob", 0.0)
                    ),
                    "words": words,
                }})
                report_progress(
                    f"decoded segment {{len(segments)}} through "
                    f"{{float(segment.end):.1f}}s of audio"
                )

            transcript = " ".join(
                item["text"] for item in segments if item["text"]
            ).strip()
            report_progress(
                f"transcription complete with {{len(segments)}} segments"
            )
            average_log_probability = (
                sum(item["avg_logprob"] for item in segments) / len(segments)
                if segments else float("-inf")
            )
            max_no_speech_probability = max(
                (item["no_speech_probability"] for item in segments),
                default=1.0,
            )
            language_probability = float(
                getattr(info, "language_probability", 0.0) or 0.0
            )
            confidence_proxy = (
                max(0.0, min(1.0, math.exp(average_log_probability)))
                * max(0.0, min(1.0, language_probability))
                if math.isfinite(average_log_probability)
                else 0.0
            )
            reference_transcript = next(
                (
                    candidate
                    for item in inputs
                    if (candidate := reference_transcript_from(item))
                ),
                "",
            )
            reference_evaluation = {{"available": False}}
            if reference_transcript:
                reference_words = reference_transcript.lower().split()
                hypothesis_words = transcript.lower().split()
                reference_characters = list(" ".join(reference_words))
                hypothesis_characters = list(" ".join(hypothesis_words))
                reference_evaluation = {{
                    "available": True,
                    "word_error_rate": (
                        edit_distance(reference_words, hypothesis_words)
                        / max(1, len(reference_words))
                    ),
                    "character_error_rate": (
                        edit_distance(reference_characters, hypothesis_characters)
                        / max(1, len(reference_characters))
                    ),
                    "reference_word_count": len(reference_words),
                }}

            failures = []
            warnings = []
            if not transcript:
                failures.append("empty_transcript")
            if language_probability < float(
                quality_policy.get("min_language_probability", 0.0)
            ):
                warnings.append("low_language_probability")
            if average_log_probability < float(
                quality_policy.get("min_average_log_probability", float("-inf"))
            ):
                warnings.append("low_average_log_probability")
            if max_no_speech_probability > float(
                quality_policy.get("max_no_speech_probability", 1.0)
            ):
                warnings.append("high_no_speech_probability")
            if (
                reference_evaluation["available"]
                and reference_evaluation["word_error_rate"]
                > float(quality_policy.get("max_reference_wer", 1.0))
            ):
                warnings.append("high_reference_word_error_rate")
            if (
                reference_evaluation["available"]
                and reference_evaluation["character_error_rate"]
                > float(quality_policy.get("max_reference_cer", 1.0))
            ):
                warnings.append("high_reference_character_error_rate")
            gate_status = "fail" if failures else ("warn" if warnings else "pass")

            processing_metadata = {{
                "adapter_id": "faster-whisper",
                "adapter_version": {implementation["adapter_version"]!r},
                "model_id": model_id,
                "model_revision": model_revision,
                "model_tree_sha256": model_tree_sha256,
                "profile": requested_profile,
                "device": device,
                "compute_type": compute_type,
                "cpu_threads": cpu_threads if device == "cpu" else 0,
                "num_workers": num_workers,
                "inference_parameters": inference_parameters,
            }}
            result = {{
                "text": transcript,
                "transcript": transcript,
                "segments": segments,
                "language": str(getattr(info, "language", "") or ""),
                "language_probability": language_probability,
                "duration_seconds": float(getattr(info, "duration", 0.0) or 0.0),
                "duration_after_vad_seconds": float(
                    getattr(info, "duration_after_vad", 0.0) or 0.0
                ),
                "confidence_metrics": {{
                    "average_log_probability": (
                        average_log_probability
                        if math.isfinite(average_log_probability)
                        else None
                    ),
                    "max_no_speech_probability": max_no_speech_probability,
                    "confidence_proxy": confidence_proxy,
                    "calibrated": False,
                    "reference_evaluation": reference_evaluation,
                }},
                "quality_gate": {{
                    "status": gate_status,
                    "failures": failures,
                    "warnings": warnings,
                    "policy": quality_policy,
                }},
                "processing_metadata": processing_metadata,
                "implementation": processing_metadata,
            }}
            output_path = Path(output_dir) / {output["filename"]!r}
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(
                json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False),
                encoding="utf-8",
            )
            if failures and bool(quality_policy.get("fail_on_empty_transcript", True)):
                raise RuntimeError(
                    "ASR quality gate failed: " + ", ".join(failures)
                )
            return [{{**{output!r}, "path": str(output_path)}}]
        """
    ).strip() + "\n"


def roberta_sentiment_function_source(node: dict[str, Any]) -> str:
    implementation = dict(node.get("implementation_plan") or {})
    inference = dict(implementation.get("inference_parameters") or {})
    quality = dict(implementation.get("quality_policy") or {})
    output = _primary_output(node, "sentiment.json")
    function_name = str(node["function_name"])
    return textwrap.dedent(
        f"""
        def {function_name}(inputs, output_dir, context):
            import hashlib
            import json
            import os
            from pathlib import Path

            import torch
            from transformers import AutoModelForSequenceClassification, AutoTokenizer

            model_id = {implementation["model_id"]!r}
            model_revision = {implementation["model_revision"]!r}
            inference_parameters = {inference!r}
            quality_policy = {quality!r}

            def resolve_local_model(reviewed_model_id, reviewed_revision):
                model_root = Path(
                    os.getenv("INLUMEN_MODEL_ROOT") or "/models"
                ).resolve()
                spec_sha256 = hashlib.sha256(
                    f"{{reviewed_model_id}}@{{reviewed_revision}}".encode("utf-8")
                ).hexdigest()
                artifact_dir = model_root / "artifacts" / spec_sha256
                manifest_path = artifact_dir / "inlumen-model-manifest.json"
                verified_path = artifact_dir / "VERIFIED"
                if not manifest_path.is_file() or not verified_path.is_file():
                    raise RuntimeError(
                        "Reviewed model is not available in the verified local model "
                        f"store: {{reviewed_model_id}}@{{reviewed_revision}}. Run the "
                        "generated model-prefetch service before pipeline execution."
                    )
                manifest_bytes = manifest_path.read_bytes()
                manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
                if verified_path.read_text(encoding="utf-8").strip() != manifest_sha256:
                    raise RuntimeError(
                        f"Local model manifest integrity check failed for {{reviewed_model_id}}."
                    )
                manifest = json.loads(manifest_bytes.decode("utf-8"))
                if (
                    manifest.get("schema_version") != "inlumen.model-artifact@1"
                    or manifest.get("model_id") != reviewed_model_id
                    or manifest.get("model_revision") != reviewed_revision
                    or manifest.get("spec_sha256") != spec_sha256
                ):
                    raise RuntimeError(
                        f"Local model identity mismatch for {{reviewed_model_id}}."
                    )
                snapshot_value = manifest.get("snapshot_path")
                if not isinstance(snapshot_value, str) or not snapshot_value.strip():
                    raise RuntimeError("Local model manifest has an unsafe snapshot path.")
                relative_snapshot = Path(snapshot_value)
                if relative_snapshot.is_absolute():
                    raise RuntimeError("Local model manifest has an unsafe snapshot path.")
                snapshot = (model_root / relative_snapshot).resolve()
                try:
                    snapshot.relative_to(model_root)
                except ValueError as exc:
                    raise RuntimeError(
                        "Local model snapshot escapes the configured model root."
                    ) from exc
                if not snapshot.is_dir():
                    raise RuntimeError(
                        f"Verified local model snapshot is missing: {{snapshot}}"
                    )
                tree_sha256 = str(manifest.get("tree_sha256") or "").lower()
                if len(tree_sha256) != 64 or any(
                    character not in "0123456789abcdef" for character in tree_sha256
                ):
                    raise RuntimeError("Local model tree SHA-256 is invalid.")
                return str(snapshot), tree_sha256

            def extract_text(value):
                if isinstance(value, str):
                    return value
                if isinstance(value, dict):
                    for key in ("text", "transcript", "content"):
                        candidate = value.get(key)
                        if isinstance(candidate, str) and candidate.strip():
                            return candidate
                    for key in ("result", "data", "transcription"):
                        candidate = extract_text(value.get(key))
                        if candidate:
                            return candidate
                if isinstance(value, list):
                    return " ".join(
                        candidate
                        for item in value
                        if (candidate := extract_text(item))
                    )
                return ""

            transcript = ""
            source_path = None
            for item in inputs:
                if not isinstance(item, dict):
                    continue
                path = Path(str(item.get("path") or ""))
                if not path.is_file():
                    continue
                try:
                    content = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    continue
                transcript = extract_text(content).strip()
                if transcript:
                    source_path = path
                    break
            if not transcript:
                raise ValueError("No non-empty transcript was supplied to sentiment analysis.")

            snapshot_path, model_tree_sha256 = resolve_local_model(
                model_id,
                model_revision,
            )
            tokenizer = AutoTokenizer.from_pretrained(
                snapshot_path,
                local_files_only=True,
            )
            model = AutoModelForSequenceClassification.from_pretrained(
                snapshot_path,
                local_files_only=True,
            )
            configured_device = str(
                os.getenv("INLUMEN_SENTIMENT_DEVICE") or {implementation.get("device", "auto")!r}
            ).lower()
            device = "cuda" if configured_device == "auto" and torch.cuda.is_available() else configured_device
            if device == "auto":
                device = "cpu"
            if device not in {{"cpu", "cuda"}}:
                raise ValueError(f"Unsupported INLUMEN_SENTIMENT_DEVICE: {{device}}")
            model.to(device)
            model.eval()

            token_ids = tokenizer(
                transcript,
                add_special_tokens=False,
            )["input_ids"]
            requested_max = int(inference_parameters.get("max_length", 512))
            model_max = int(getattr(tokenizer, "model_max_length", requested_max))
            max_length = max(8, min(requested_max, model_max, 512))
            window_size = max_length - 2
            overlap = max(
                0,
                min(int(inference_parameters.get("overlap_tokens", 64)), window_size - 1),
            )
            step_size = max(1, window_size - overlap)
            weighted_scores = None
            total_weight = 0
            chunks = []
            for start in range(0, max(1, len(token_ids)), step_size):
                chunk_ids = token_ids[start : start + window_size]
                if not chunk_ids:
                    break
                chunk_text = tokenizer.decode(
                    chunk_ids,
                    skip_special_tokens=True,
                )
                encoded = tokenizer(
                    chunk_text,
                    return_tensors="pt",
                    truncation=True,
                    max_length=max_length,
                )
                encoded = {{key: value.to(device) for key, value in encoded.items()}}
                with torch.inference_mode():
                    probabilities = torch.softmax(model(**encoded).logits[0], dim=-1)
                values = probabilities.detach().cpu().tolist()
                weight = len(chunk_ids)
                weighted_scores = (
                    [value * weight for value in values]
                    if weighted_scores is None
                    else [
                        current + value * weight
                        for current, value in zip(weighted_scores, values)
                    ]
                )
                total_weight += weight
                chunks.append({{
                    "start_token": start,
                    "token_count": weight,
                    "scores": {{
                        str(model.config.id2label.get(index, f"LABEL_{{index}}")).lower(): float(value)
                        for index, value in enumerate(values)
                    }},
                }})
                if start + window_size >= len(token_ids):
                    break

            if weighted_scores is None or total_weight <= 0:
                raise RuntimeError("Sentiment model produced no prediction windows.")
            aggregate_scores = {{
                str(model.config.id2label.get(index, f"LABEL_{{index}}")).lower(): float(
                    value / total_weight
                )
                for index, value in enumerate(weighted_scores)
            }}
            label = max(aggregate_scores, key=aggregate_scores.get)
            top_probability = float(aggregate_scores[label])
            min_probability = float(
                quality_policy.get("min_top_class_probability", 0.0)
            )
            quality_status = "pass" if top_probability >= min_probability else "warn"
            processing_metadata = {{
                "adapter_id": "transformers-roberta-sentiment",
                "adapter_version": {implementation["adapter_version"]!r},
                "model_id": model_id,
                "model_revision": model_revision,
                "model_tree_sha256": model_tree_sha256,
                "device": device,
                "inference_parameters": inference_parameters,
                "source_transcript_path": str(source_path) if source_path else "",
                "chunk_count": len(chunks),
                "aggregation": "token-count-weighted-mean",
            }}
            result = {{
                "transcript": transcript,
                "sentiment_label": label,
                "confidence": top_probability,
                "sentiment_scores": aggregate_scores,
                "processing_metadata": processing_metadata,
                # Compatibility aliases for consumers of the adapter's earlier
                # uncontracted payload. Canonical consumers should use the fields
                # above, which are guaranteed by the node data contract.
                "label": label,
                "score": top_probability,
                "scores": aggregate_scores,
                "chunks": chunks,
                "source_transcript": str(source_path) if source_path else "",
                "quality_gate": {{
                    "status": quality_status,
                    "warnings": (
                        [] if quality_status == "pass" else ["low_top_class_probability"]
                    ),
                    "policy": quality_policy,
                }},
                "implementation": processing_metadata,
            }}
            output_path = Path(output_dir) / {output["filename"]!r}
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(
                json.dumps(result, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            return [{{**{output!r}, "path": str(output_path)}}]
        """
    ).strip() + "\n"


def _primary_output(node: dict[str, Any], fallback_filename: str) -> dict[str, Any]:
    outputs = node.get("outputs")
    candidate = (
        dict(outputs[0])
        if isinstance(outputs, list) and outputs and isinstance(outputs[0], dict)
        else {}
    )
    candidate.setdefault("name", fallback_filename.rsplit(".", 1)[0])
    candidate.setdefault("kind", "json")
    candidate.setdefault("format", "json")
    candidate["filename"] = str(
        candidate.get("filename") or f"{candidate['name']}.json"
    )
    return candidate


def _remove_unused_imports(body: list[ast.stmt]) -> list[ast.stmt]:
    module = ast.Module(body=body, type_ignores=[])
    loaded_names = {
        item.id
        for item in ast.walk(module)
        if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Load)
    }
    filtered: list[ast.stmt] = []
    for statement in body:
        if isinstance(statement, ast.Import):
            aliases = [
                alias
                for alias in statement.names
                if (alias.asname or alias.name.split(".", 1)[0]) in loaded_names
            ]
            if aliases:
                statement.names = aliases
                filtered.append(statement)
        elif isinstance(statement, ast.ImportFrom):
            if statement.module == "__future__":
                filtered.append(statement)
                continue
            aliases = [
                alias
                for alias in statement.names
                if alias.name == "*" or (alias.asname or alias.name) in loaded_names
            ]
            if aliases:
                statement.names = aliases
                filtered.append(statement)
        else:
            filtered.append(statement)
    return filtered
