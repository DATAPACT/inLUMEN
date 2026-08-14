import unittest

from artifact_content import decode_artifact_content, encode_artifact_bytes


class ArtifactContentTransportTest(unittest.TestCase):
    def test_text_content_remains_plain_utf8(self):
        original = b"name,value\nalpha,1\n"

        encoded = encode_artifact_bytes(
            original,
            filename="sample.csv",
            content_type="text/csv",
        )

        self.assertEqual("utf-8", encoded["content_encoding"])
        self.assertEqual(original.decode("utf-8"), encoded["content"])
        self.assertEqual(original, decode_artifact_content(encoded))

    def test_binary_content_uses_lossless_base64(self):
        original = b"RIFF\xff\x00\x80WAVEfmt \x00data"

        encoded = encode_artifact_bytes(
            original,
            filename="sample.wav",
            content_type="audio/wav",
        )

        self.assertEqual("base64", encoded["content_encoding"])
        self.assertEqual(len(original), encoded["size_bytes"])
        self.assertTrue(encoded["sha256"].startswith("sha256:"))
        self.assertEqual(original, decode_artifact_content(encoded))

if __name__ == "__main__":
    unittest.main()
