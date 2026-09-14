import copy
import io
import json
import stat
import unittest
import zipfile
from unittest.mock import patch

from project_package import build_package, parse_package


def sample():
    return {'pipeline': {'name': 'Portable', 'last_run': 'private history'}, 'settings': {'engine': 'dagster'},
            'nodes': [{'id': 'task', 'position': {'x': 20, 'y': 30}, 'data': {
                'type': 'task', 'label': 'Task', 'param': {'API_KEY': 'hidden', 'CUSTOM': 'hidden-too', 'DEVICE': 'cpu'},
                'secret_params': ['CUSTOM'], 'param_json': '{"API_KEY":"hidden"}',
                'generated_artifact': {'validation_report': 'private history'},
                'files': [{'filename': 'main.py', 'bucket': 'old-workspace', 'role': 'code'}],
            }}], 'edges': []}


class ProjectPackageTests(unittest.TestCase):
    def test_round_trip_and_secret_exclusion(self):
        graph = sample()
        before = copy.deepcopy(graph)
        archive = build_package(graph, lambda *_: b'\x00\xffcode\n')
        manifest, blobs, preview = parse_package(archive)
        self.assertEqual(graph, before)
        self.assertEqual(list(blobs.values()), [b'\x00\xffcode\n'])
        self.assertEqual(preview['secret_parameters'], 2)
        self.assertEqual(manifest['graph']['nodes'][0]['data']['param'], {'API_KEY': '', 'CUSTOM': '', 'DEVICE': 'cpu'})
        encoded = json.dumps(manifest)
        for private in ('hidden', 'old-workspace', 'private history', 'param_json'):
            self.assertNotIn(private, encoded)
        self.assertEqual(manifest['graph']['nodes'][0]['position'], {'x': 20, 'y': 30})

    def test_reusable_definitions_are_deduplicated_and_required(self):
        graph = sample()
        dependency = sample()
        graph['nodes'] = [{'id': str(i), 'data': {'type': 'subpipeline', 'subpipeline': {
            'reference': {'pipeline_uid': 'external', 'pipeline_name': 'Reusable'}, 'resolved_graph': dependency,
        }}} for i in range(2)]
        manifest, _, preview = parse_package(build_package(graph, lambda *_: b'code'))
        self.assertEqual(preview['definitions'], 1)
        self.assertEqual(len(manifest['graph']['nodes']), 2)
        graph['nodes'][0]['data']['subpipeline'].pop('resolved_graph')
        with self.assertRaisesRegex(ValueError, 'Resolve every'):
            build_package(graph, lambda *_: b'code')

    def rewrite(self, change):
        payload = build_package(sample(), lambda *_: b'code')
        with zipfile.ZipFile(io.BytesIO(payload)) as source:
            entries = {name: source.read(name) for name in source.namelist()}
        change(entries)
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w') as archive:
            for name, content in entries.items():
                archive.writestr(name, content)
        return output.getvalue()

    def test_tampered_missing_and_unexpected_files_are_rejected(self):
        def tamper(entries):
            entries[next(name for name in entries if name.startswith('files/'))] = b'tampered'
        def missing(entries):
            entries.pop(next(name for name in entries if name.startswith('files/')))
        for change in (tamper, missing, lambda entries: entries.update({'../escape': b'bad'}),
                       lambda entries: entries.update({'extra.txt': b'bad'})):
            with self.subTest(change=change), self.assertRaises(ValueError):
                parse_package(self.rewrite(change))

    def test_invalid_graph_and_foreign_storage_pointers_are_not_imported(self):
        def change(entries):
            manifest = json.loads(entries['manifest.json'])
            data = manifest['graph']['nodes'][0]['data']
            data['file_buckets'] = [{'filename': 'steal', 'bucket': 'foreign'}]
            data['files'][0]['snapshot_bucket'] = 'foreign'
            entries['manifest.json'] = json.dumps(manifest).encode()
        manifest, _, _ = parse_package(self.rewrite(change))
        self.assertNotIn('foreign', json.dumps(manifest))
        graph = sample()
        graph['edges'] = [{'source': 'task', 'target': 'missing'}]
        with self.assertRaises(ValueError):
            build_package(graph, lambda *_: b'code')

    def test_limits_and_symlink(self):
        archive = build_package(sample(), lambda *_: b'code')
        with patch('project_package.MAX_BYTES', len(archive) - 1), self.assertRaises(ValueError):
            parse_package(archive)
        with patch('project_package.MAX_ENTRIES', 1), self.assertRaises(ValueError):
            parse_package(archive)
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w') as archive:
            info = zipfile.ZipInfo('manifest.json')
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, 'target')
        with self.assertRaises(ValueError):
            parse_package(output.getvalue())

    def test_legacy_attachment_without_a_role_round_trips(self):
        graph = sample()
        graph['nodes'][0]['data']['files'][0]['role'] = None
        manifest, _, _ = parse_package(build_package(graph, lambda *_: b'code'))
        self.assertEqual(manifest['graph']['nodes'][0]['data']['files'][0]['role'], '')

    def test_nested_reusable_pipeline_is_rejected(self):
        graph = sample()
        graph['nodes'][0]['data'].update(type='subpipeline', subpipeline={
            'reference': {'pipeline_uid': 'outer'}, 'resolved_graph': {'nodes': [
                {'id': 'nested', 'data': {'type': 'subpipeline'}}], 'edges': []},
        })
        with self.assertRaisesRegex(ValueError, 'Nested'):
            build_package(graph, lambda *_: b'code')


if __name__ == '__main__':
    unittest.main()
