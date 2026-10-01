"""Bounded ZIP inspection and graph-aware Task package validation (no execution)."""
import hashlib
import io
import json
import re
import stat
import zipfile
from pathlib import PurePosixPath
from urllib.parse import quote, unquote

from artifact_runtime import ArtifactContractError, validate_connection_contract
from task_package_contract import safe_path, validate_package, resolve_requirements

MAX_ARCHIVE = 50 * 1024 * 1024
MAX_EXPANDED = 200 * 1024 * 1024
MAX_ENTRIES = 4096
PLATFORM_FILES = {'node-manifest.json', 'validation-report.json'}


def task_package_folder(node_id, label):
    """Human-readable label plus an opaque identity, never a sequence number."""
    name = re.sub(r'[^a-zA-Z0-9 ]+', '-', str(label or ''))
    name = re.sub(r' +', ' ', name).strip(' -')[:64].rstrip(' -') or 'Task'
    # Escape hyphens in IDs so even IDs containing '--' round-trip safely.
    identity = quote(str(node_id), safe='').replace('-', '%2D')
    return f'nodes/{name}--{identity}'


def read_packages(data):
    if len(data) >= MAX_ARCHIVE:
        raise ArtifactContractError('ZIP must be smaller than 50 MB')
    files, total = {}, 0
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if len(archive.infolist()) > MAX_ENTRIES:
                raise ArtifactContractError('ZIP contains more than 4096 entries')
            seen = set()
            for entry in archive.infolist():
                name = entry.filename.rstrip('/')
                safe_path(name)
                if name in seen:
                    raise ArtifactContractError(f'Duplicate ZIP entry: {name}')
                seen.add(name)
                mode = entry.external_attr >> 16
                if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode))):
                    raise ArtifactContractError(f'Unsafe ZIP link or special file: {name}')
                if entry.flag_bits & 1:
                    raise ArtifactContractError('Encrypted ZIP entries are unsupported')
                if entry.is_dir():
                    continue
                total += entry.file_size
                if total > MAX_EXPANDED:
                    raise ArtifactContractError('Expanded ZIP exceeds 200 MB')
                with archive.open(entry) as stream:
                    body = stream.read(MAX_EXPANDED - sum(map(len, files.values())) + 1)
                if len(body) != entry.file_size or len(body) > MAX_EXPANDED:
                    raise ArtifactContractError(f'Invalid ZIP size for {name}')
                files[name] = body
    except (zipfile.BadZipFile, RuntimeError, OSError) as exc:
        raise ArtifactContractError(f'Invalid ZIP: {exc}') from exc
    roots = sorted({str(PurePosixPath(name).parent) for name in files if PurePosixPath(name).name == 'main.py'})
    # A helper directory with a main.py is part of its parent package.
    roots = [root for root in roots if not any(other == '.' or root.startswith(other + '/') for other in roots if other != root)]
    if not roots:
        raise ArtifactContractError('ZIP contains no Task package with main.py')
    result = {}
    for root in roots:
        prefix = '' if root == '.' else root + '/'
        content = {name[len(prefix):]: body for name, body in files.items() if name.startswith(prefix)}
        if any(PurePosixPath(name).name in PLATFORM_FILES or PurePosixPath(name).name.lower().startswith('dockerfile') for name in content):
            raise ArtifactContractError(f'{root}: contains platform-generated files; export a portable Task package')
        result[root] = content
    return result


def inspect_packages(data, graph, mappings=None):
    packages = read_packages(data)
    nodes = {str(n['id']): n for n in graph.get('nodes', []) if str(n.get('data', {}).get('type', '')).lower() == 'task'}
    mappings = mappings or {}
    if not isinstance(mappings, dict) or set(mappings) - set(packages):
        raise ArtifactContractError('Mappings must identify folders in this ZIP')
    checked, errors, used = [], [], set()
    for folder, files in packages.items():
        item = {'folder': folder, 'files': list(files), 'node_id': None, 'warnings': [], 'errors': []}
        # Mapping is independent of code validity: an invalid manifest must not
        # erase a known target or make the user map nodes/2 again.
        name = PurePosixPath(folder).name
        ident = unquote(name.rsplit('--', 1)[-1])
        normalized = lambda value: re.sub(r'[^a-z0-9]+', ' ', str(value).lower()).strip()
        explicit_identity = '--' in name
        matches = [ident] if ident in nodes else [] if explicit_identity else [key for key, node in nodes.items() if normalized(node['data'].get('label')) == normalized(name)]
        if not matches and not explicit_identity and folder not in mappings:
            tokens = set(normalized(name).split())
            candidates = sorted(((len(tokens & set(normalized(node['data'].get('label')).split())) / max(len(tokens), 1), key) for key, node in nodes.items()), reverse=True)
            if candidates and candidates[0][0] >= .5 and (len(candidates) == 1 or candidates[0][0] > candidates[1][0]):
                matches = [candidates[0][1]]
                item['warnings'].append('Suggested Task based on folder name; verify this match before importing.')
        node_id = str(mappings[folder]) if folder in mappings else matches[0] if len(matches) == 1 else None
        if node_id not in nodes:
            item['errors'].append({'task': folder, 'filename': 'package', 'field': 'mapping', 'message': 'Choose the target Task for this package'})
        else:
            item['node_id'] = node_id
            item['label'] = nodes[node_id]['data'].get('label', node_id)
            item['replaces_code'] = bool(nodes[node_id]['data'].get('file_buckets') or nodes[node_id]['data'].get('files'))
            if node_id in used:
                item['errors'].append({'task': folder, 'filename': 'package', 'field': 'mapping', 'message': 'Multiple packages map to the same Task'})
            used.add(node_id)
        try:
            valid = validate_package(files)
            item.update(data_contract=valid['data_contract'], manifest=valid['metadata'], requirements=valid['requirements'])
            item['warnings'].extend(valid['warnings'])
        except (ArtifactContractError, UnicodeError) as exc:
            for issue in getattr(exc, 'errors', [exc]):
                item['errors'].append({'task': folder, 'filename': 'inlumen.task.json', 'message': str(issue), **getattr(issue, 'details', {})})
        checked.append(item)
    contracts = {str(n['id']): (n.get('data', {}).get('generated_artifact') or {}).get('data_contract', {}) for n in graph.get('nodes', [])}
    for node in graph.get('nodes', []):
        source_data = node.get('data', {})
        if source_data.get('type') not in {'source', 'input'} or contracts.get(str(node['id']), {}).get('outputs'):
            continue
        files = source_data.get('file_buckets', source_data.get('files', [])) or []
        files = [file for file in files if not isinstance(file, dict) or file.get('role') != 'code']
        if len(files) == 1:
            filename = files[0] if isinstance(files[0], str) else files[0].get('filename', '')
            if filename:
                contracts[str(node['id'])] = {'outputs': [{'name': 'data', 'filename': filename, 'representation': 'file', 'format': PurePosixPath(filename).suffix.lstrip('.').lower()}]}
    contracts.update({item['node_id']: item.get('data_contract', {}) for item in checked if item['node_id']})
    incoming = {}
    for edge in graph.get('edges', []):
        target, source = str(edge['target']), str(edge['source'])
        port = edge.get('targetHandle') or 'input'
        incoming.setdefault(target, []).append((source, port))
    affected = set(used) | {str(e['target']) for e in graph.get('edges', []) if str(e['source']) in used}
    graph_checks = checked + [{'node_id': key, 'folder': nodes[key]['data'].get('label', key), 'data_contract': contracts.get(key, {}), 'errors': []} for key in affected - used if key in nodes]
    for item in graph_checks:
        if not item['node_id'] or item['errors']:
            continue
        contract = item['data_contract']
        occupied = {}
        try:
            for requirement in [*contract.get('input_requirements', []), *contract.get('inputs', [])]:
                if not any(port == requirement.get('target_port', 'input') for _, port in incoming.get(item['node_id'], [])):
                    raise ArtifactContractError(f"Input port {requirement.get('target_port', 'input')} has no connection")
            for source, port in incoming.get(item['node_id'], []):
                output = contracts.get(source, {}).get('outputs', [])
                if not output:
                    continue  # Managed Source declarations are resolved at run/export preparation.
                declaration = output[0]
                filename = declaration['filename']
                if any(filename == name or filename.startswith(name + '/') or name.startswith(filename + '/') for name in occupied):
                    raise ArtifactContractError(f'Input filename collision at {filename}: producers {occupied} and {source}')
                occupied[filename] = source
                for requirement in [*contract.get('input_requirements', []), *contract.get('inputs', [])]:
                    if requirement.get('target_port', 'input') == port:
                        validate_connection_contract(declaration, requirement, connection=f'{source}->{item["node_id"]}:{port}')
        except ArtifactContractError as exc:
            item['errors'].append({'task': item['folder'], 'filename': 'inlumen.task.json', 'message': str(exc), **getattr(exc, 'details', {})})
    errors = [error for item in graph_checks for error in item['errors']]
    try:
        # Shared runtimes install the union; reject known cross-package conflicts.
        all_dependencies = [dep for item in checked for dep in item.get('requirements', [])]
        resolve_requirements({'requirements.txt': '\n'.join(all_dependencies)})
    except ArtifactContractError as exc:
        errors.append({'task': 'pipeline', 'filename': 'requirements.txt', 'message': str(exc)})

    return {'digest': hashlib.sha256(data).hexdigest(), 'packages': checked, 'errors': errors, 'valid': not errors}, packages
