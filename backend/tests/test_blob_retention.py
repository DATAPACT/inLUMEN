import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
from prune_workspace_blobs import referenced_objects


class BlobRetentionTests(unittest.TestCase):
    def test_protects_files_and_references_in_nested_saved_versions(self):
        refs = referenced_objects({'filename': 'main.py', 'bucket': 'node',
            'graph_json': '{"nodes":[{"data":{"file_buckets":[{"snapshot_bucket":"versions","snapshot_object":"old/main.py"}]}}]}'})
        self.assertEqual(refs, {('node', 'main.py'), ('versions', 'old/main.py')})

    def test_unreadable_saved_json_blocks_cleanup(self):
        with self.assertRaises(ValueError):
            referenced_objects({'graph_json': '{broken'})
