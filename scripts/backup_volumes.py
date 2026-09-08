#!/usr/bin/env python3
"""Consistent offline backup and isolated restore of a named Compose project.

Backup stops only containers with the exact project label and restarts those
previously running. Restore creates NEW volumes and refuses existing targets.
Keep encryption keys separately; archives contain application data.
"""
import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path

IMAGE = 'python:3.11-slim@sha256:9534e5a8e315485d4061ed659af0fd78a284c015f9b73661b41d6bab25604534'


def command(*args):
    return subprocess.check_output(['docker', *args], text=True).strip()


def digest(path):
    with path.open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def verify(directory):
    manifest = json.loads((directory / 'manifest.json').read_text())
    for item in manifest['volumes']:
        filename = item['archive']
        if not re.fullmatch(r'volume-\d+\.tar\.gz', filename) or digest(directory / filename) != item['sha256']:
            raise ValueError('Backup checksum or archive path is invalid')
    return manifest


def backup(project, directory):
    directory.mkdir(parents=True, exist_ok=False)
    ids = command('ps', '-a', '-q', '--filter', f'label=com.docker.compose.project={project}').split()
    if not ids: raise ValueError('No containers in the requested project')
    containers = json.loads(command('inspect', *ids))
    running = [container['Id'] for container in containers if container['State']['Running']]
    volumes = sorted({mount['Name'] for container in containers for mount in container['Mounts'] if mount['Type'] == 'volume'})
    manifest = {'project': project, 'images': [container['Image'] for container in containers], 'volumes': []}
    try:
        if running: command('stop', *running)
        for index, volume in enumerate(volumes):
            archive = f'volume-{index}.tar.gz'
            command('run', '--rm', '--network', 'none', '-v', f'{volume}:/data:ro', '-v', f'{directory}:/backup', IMAGE,
                    'python', '-c', f'import tarfile; t=tarfile.open("/backup/{archive}","w:gz"); t.add("/data",arcname="."); t.close()')
            manifest['volumes'].append({'name': volume, 'archive': archive, 'sha256': digest(directory / archive)})
        (directory / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
        verify(directory)
    finally:
        if running: command('start', *running)


def restore(directory, prefix):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,100}', prefix):
        raise ValueError('Restore prefix must be a valid Docker volume name')
    manifest = verify(directory)
    existing = set(command('volume', 'ls', '-q').splitlines())
    targets = [f'{prefix}-{index}' for index, _ in enumerate(manifest['volumes'])]
    if existing.intersection(targets): raise ValueError('Restore refuses to overwrite an existing volume')
    for target, item in zip(targets, manifest['volumes']):
        command('volume', 'create', target)
        command('run', '--rm', '--network', 'none', '-v', f'{target}:/data', '-v', f'{directory}:/backup:ro', IMAGE,
                'python', '-c', f'import tarfile\ndef safe(member, path):\n result=tarfile.data_filter(member,path)\n if result is not None: result.uid=member.uid; result.gid=member.gid\n return result\nwith tarfile.open("/backup/{item["archive"]}") as t: t.extractall("/data",filter=safe)')
    print(json.dumps(dict(zip((item['name'] for item in manifest['volumes']), targets)), indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['backup', 'verify', 'restore'])
    parser.add_argument('directory', type=lambda value: Path(value).expanduser().resolve())
    parser.add_argument('--project')
    parser.add_argument('--restore-prefix')
    args = parser.parse_args()
    if args.action == 'backup':
        if not args.project: parser.error('--project is required')
        backup(args.project, args.directory)
    elif args.action == 'restore':
        if not args.restore_prefix: parser.error('--restore-prefix is required')
        restore(args.directory, args.restore_prefix)
    else: verify(args.directory)
