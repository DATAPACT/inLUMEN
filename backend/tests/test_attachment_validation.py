import io
import sys
import unittest
import wave
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from attachment_validation import (  # noqa: E402
    attachment_input_errors,
    read_attachment_probe,
)


class AttachmentValidationTests(unittest.TestCase):
    def test_rejects_external_ai_placeholder_wav(self):
        content = (
            b"RIFF0000WAVEfmt 0000FAKEPCM\n"
            b"SAMPLE_A\nThis is a fake wav file for demos.\n"
        )

        errors = attachment_input_errors(
            "input.wav",
            content,
            size_bytes=len(content),
        )

        self.assertTrue(any("placeholder data" in error for error in errors))
        self.assertTrue(any("not a valid WAV" in error for error in errors))

    def test_accepts_real_pcm_wav(self):
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(8000)
            wav_file.writeframes(b"\x00\x00" * 80)
        content = buffer.getvalue()

        self.assertEqual(
            [],
            attachment_input_errors(
                "recording.wav",
                content,
                size_bytes=len(content),
            ),
        )

    def test_upload_probe_restores_stream_position(self):
        stream = io.BytesIO(b"real input bytes")
        stream.seek(3)

        probe, size_bytes = read_attachment_probe(stream)

        self.assertEqual(b"real input bytes", probe)
        self.assertEqual(16, size_bytes)
        self.assertEqual(3, stream.tell())


if __name__ == "__main__":
    unittest.main()
