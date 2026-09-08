#!/usr/bin/env python3
"""Generate standalone service copies and browser contracts from canonical sources."""
import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COPIES = {
    'shared/request_diagnostics.py': ['runner/app/request_diagnostics.py', 'codegen/app/request_diagnostics.py'],
    'shared/leases.py': ['runner/app/leases.py', 'codegen/app/leases.py'],
    'backend/node_definitions/manifests/core.json': ['frontend/src/features/nodes/registry/core.generated.json'],
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
    if args.check and stale:
        raise SystemExit('Run python scripts/sync_shared.py; stale generated files: ' + ', '.join(stale))


if __name__ == '__main__':
    main()
