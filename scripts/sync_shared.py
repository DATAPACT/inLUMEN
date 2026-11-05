#!/usr/bin/env python3
"""Generate service copies and browser contracts from canonical sources.

The agents' contracts live in the inlumen-agents package
(packages/agents/src/inlumen/contracts) and are installed, not copied; only
the browser copies and the public contract files are generated from them.
"""
import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COPIES = {
    'packages/agents/src/inlumen/contracts/task_package_spec.json': ['frontend/src/features/flow/taskPackageGuide.generated.json'],
    'shared/run_artifact_names.py': ['backend/run_artifact_names.py', 'codegen/app/run_artifact_names.py'],
    'shared/request_diagnostics.py': ['runner/app/request_diagnostics.py', 'codegen/app/request_diagnostics.py'],
    'packages/agents/src/inlumen/contracts/port_types.json': ['frontend/src/features/flow/portTypes.generated.json'],
    'shared/leases.py': ['runner/app/leases.py', 'codegen/app/leases.py'],
    'backend/node_definitions/manifests/core.json': ['frontend/src/features/nodes/registry/core.generated.json'],
    'backend/plugins/plugin-manifest.schema.json': ['contracts/plugin-manifest-v1/schema.json'],
    'backend/plugins/manifests/jsi-wikifier.plugin.json': ['contracts/plugin-manifest-v1/example.json'],
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    stale = []
    for source, targets in COPIES.items():
        expected = (ROOT / source).read_bytes()
        for target in targets:
            path = ROOT / target
            if not path.exists() or path.read_bytes() != expected:
                stale.append(target)
                if not args.check:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(expected)
    spec = json.loads((ROOT / 'packages/agents/src/inlumen/contracts/task_package_spec.json').read_text())
    generated = {
        'contracts/task-package-v1/schema.json': json.dumps(spec['schema'], indent=2) + '\n',
        'contracts/task-package-v1/example.json': json.dumps(spec['example'], indent=2) + '\n',
        'contracts/task-package-v1/README.md': '# Task package v1\n\n' + spec['guide'],
    }
    for target, expected in generated.items():
        path = ROOT / target
        if not path.exists() or path.read_text() != expected:
            stale.append(target)
            if not args.check:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(expected)
    if args.check and stale:
        raise SystemExit('Run python scripts/sync_shared.py; stale generated files: ' + ', '.join(stale))


if __name__ == '__main__':
    main()
