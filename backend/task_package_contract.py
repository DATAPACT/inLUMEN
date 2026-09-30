"""Public Task package v1: shared authoring, validation and normalization."""
import ast
import json
import posixpath
import shlex
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit

from jsonschema import Draft202012Validator
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import parse_wheel_filename

try:
    from .artifact_runtime import CONTRACT_ID, ArtifactContractError, validate_declaration
except ImportError:
    from artifact_runtime import CONTRACT_ID, ArtifactContractError, validate_declaration

TASK_METADATA_FILENAME = 'inlumen.task.json'
# This data file also generates the public schema, documentation and browser help.
_SPEC = json.loads(__import__('pathlib').Path(__file__).with_name('task_package_spec.json').read_text())
TASK_SCHEMA, EXAMPLE, AUTHORING_GUIDE = _SPEC['schema'], _SPEC['example'], _SPEC['guide']


class PackageValidationError(ArtifactContractError):
    def __init__(self, *, filename, field, supplied, expected, message, hint=None):
        self.details = dict(filename=filename, field=field, supplied=supplied, expected=expected, example=EXAMPLE)
        if hint:
            self.details['hint'] = hint
        super().__init__(f"{filename} · {field}: {message}")


class PackageValidationErrors(PackageValidationError):
    """Preserve every schema issue so authors can repair a package in one pass."""
    def __init__(self, errors):
        self.errors = errors
        self.details = errors[0].details
        ArtifactContractError.__init__(self, '\n'.join(map(str, errors)))


def manifest_errors(metadata):
    errors = []
    for error in sorted(Draft202012Validator(TASK_SCHEMA).iter_errors(metadata), key=lambda e: str(list(e.path))):
        field = '/'.join(map(str, error.path)) or '$'
        message, hint = error.message, None
        if field.endswith('/model_revision'):
            message = f'Model revision {error.instance!r} must be an immutable commit hash (7–64 hexadecimal characters).'
            hint = ('Resolve the model repository revision to a real commit SHA, then use that same SHA in the manifest and model-loading code. '
                    'For Hugging Face: HfApi().model_info(model_id, revision="main").sha. Do not invent a hash or remove a required model declaration.')
        elif field.endswith('/kind') and error.validator == 'enum':
            hint = 'For JSON output use "kind": "json", or omit optional kind. Allowed values: ' + ', '.join(error.validator_value) + '.'
        errors.append(PackageValidationError(filename=TASK_METADATA_FILENAME, field=field,
            supplied=error.instance, expected=error.validator_value, message=message, hint=hint))
    return errors


def safe_path(value):
    if not isinstance(value, str) or not value or '\\' in value or any(ord(c) < 32 for c in value):
        raise ArtifactContractError(f'Unsafe package-relative path: {value!r}')
    parts = value.split('/')
    if any(p in {'', '.', '..'} for p in parts) or ':' in parts[0] or any(c in value for c in '*?[]'):
        raise ArtifactContractError(f'Unsafe package-relative path: {value!r}')
    return value


def _content_schema(spec):
    if 'schema' in spec:
        if not isinstance(spec['schema'], dict):
            raise ArtifactContractError('Artifact content schema must be an object')
        try:
            Draft202012Validator.check_schema(spec['schema'])
        except Exception as exc:
            raise ArtifactContractError(f'Invalid artifact content schema: {exc.message}') from exc
        # Schemas stay portable and validation must never fetch arbitrary URLs.
        def refs(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key in {'$ref', '$dynamicRef'} and not str(item).startswith('#'):
                        raise ArtifactContractError('Content schemas support only local fragment references')
                    refs(item)
            elif isinstance(value, list):
                for item in value:
                    refs(item)
        refs(spec['schema'])


def task_data_contract(metadata):
    if not isinstance(metadata, dict):
        raise ArtifactContractError(f'{TASK_METADATA_FILENAME} must contain an object')
    if 'data_contract' in metadata:
        if 'version' in metadata or 'inputs' in metadata or (isinstance(metadata.get('output'), dict) and ('path' in metadata['output'] or 'type' in metadata['output'])):
            raise ArtifactContractError('Conflicting public and internal artifact declarations')
        allowed = {'data_contract', 'execution', 'input', 'output', 'capabilities', 'dependencies', 'models', 'resources', 'secrets', 'side_effects', 'schema_version'}
        if set(metadata) - allowed:
            raise ArtifactContractError(f'Unknown legacy metadata fields: {sorted(set(metadata)-allowed)}')
        contract = metadata['data_contract']
        if not isinstance(contract, dict) or contract.get('contract_id') != CONTRACT_ID or contract.get('version', '2') != '2':
            raise ArtifactContractError('Legacy data_contract must declare inlumen.generic-node@2, version 2')
        if not isinstance(contract.get('inputs'), list) or not isinstance(contract.get('outputs'), list) or len(contract['outputs']) != 1:
            raise ArtifactContractError('Legacy data_contract requires inputs and exactly one output artifact')
        def legacy(item):
            result = validate_declaration(item)
            if 'representation' not in item:
                raise ArtifactContractError('Artifact requires representation')
            safe_path(result['filename'])
            _content_schema(result)
            if 'members' in result:
                if not isinstance(result['members'], list):
                    raise ArtifactContractError('Artifact members must be a list')
                result['members'] = [legacy(member) for member in result['members']]
            return result
        return {**contract, 'version': '2', 'inputs': [legacy(x) for x in contract['inputs']], 'outputs': [legacy(x) for x in contract['outputs']]}
    errors = manifest_errors(metadata)
    if errors:
        raise PackageValidationErrors(errors)
    out = metadata['output']
    safe_path(out['path'])
    _content_schema(out)
    output = {'name': out.get('name', 'output'), 'filename': out['path'], 'representation': out['type'],
              'kind': out.get('kind', 'directory' if out['type'] == 'directory' else 'binary')}
    fmt = out.get('format') or ('json' if out['type'] == 'file' and out['path'].lower().endswith('.json') else None)
    if 'schema' in out and (out['type'] != 'file' or fmt != 'json'):
        raise ArtifactContractError('output/schema requires a JSON file output; set output/format to json')
    if fmt:
        output.update(format=fmt, kind=out.get('kind', 'json' if fmt == 'json' else output['kind']))
    if 'schema' in out:
        output['schema'] = out['schema']
    if 'members' in out:
        if out['type'] != 'directory':
            raise ArtifactContractError('output/members requires a directory bundle')
        output['members'] = [task_data_contract({'version': 1, 'output': member})['outputs'][0] for member in out['members']]
    inputs = []
    seen = set()
    for item in metadata.get('inputs', []):
        if item['port'] in seen:
            raise ArtifactContractError(f'Duplicate input requirement for port {item["port"]}')
        seen.add(item['port'])
        _content_schema(item)
        requirement = {'name': item['port'], 'target_port': item['port']}
        for source, target in [('type', 'representation'), ('format', 'format'), ('schema', 'schema')]:
            if source in item:
                requirement[target] = item[source]
        inputs.append(requirement)
    return {'contract_id': CONTRACT_ID, 'version': '2', 'inputs': [], 'input_requirements': inputs, 'outputs': [output]}


def parse_task_metadata(content):
    try:
        metadata = json.loads(content)
    except (ValueError, UnicodeError) as exc:
        raise ArtifactContractError(f'{TASK_METADATA_FILENAME} must contain valid UTF-8 JSON') from exc
    task_data_contract(metadata)
    return metadata


def public_manifest(contract, *, models=None):
    """Serialize the internal declaration without exporting runtime identities."""
    if len(contract.get('outputs', [])) != 1:
        raise ArtifactContractError('Task must declare exactly one output artifact')
    out = contract['outputs'][0]
    result = {'version': 1, 'output': {'type': out.get('representation', 'file'), 'path': out['filename']}}
    for key in ('name', 'kind', 'format', 'schema'):
        if out.get(key):
            result['output'][key] = out[key]
    if out.get('members'):
        result['output']['members'] = [public_manifest({'outputs': [member]})['output'] for member in out['members']]
    if models:
        result['models'] = models
    requirements = {}
    for item in [*contract.get('inputs', []), *contract.get('input_requirements', [])]:
        port = item.get('target_port')
        if port:
            value = {'port': port}
            for source, target in [('representation', 'type'), ('format', 'format'), ('schema', 'schema')]:
                if item.get(source):
                    value[target] = item[source]
            if port in requirements and requirements[port] != value:
                raise ArtifactContractError(f'Conflicting input declarations for port {port}')
            requirements[port] = value
    if requirements:
        result['inputs'] = list(requirements.values())
    task_data_contract(result)
    return result


def resolve_requirements(files, *, with_constraints=False):
    """Flatten package-local includes and preserve standard PEP 508 semantics."""
    dependencies, constraints = [], []
    def read(filename, constraint=False, parents=()):
        safe_path(filename)
        if filename in parents:
            raise ArtifactContractError(f'Cyclic requirements include: {filename}')
        if filename not in files:
            raise ArtifactContractError(f'Missing requirements include: {filename}')
        content = files[filename]
        if isinstance(content, bytes):
            content = content.decode('utf-8')
        for number, raw in enumerate(content.replace('\\\n', '').splitlines(), 1):
            line = raw.strip().split(' #', 1)[0].strip()
            if not line or line.startswith('#'):
                continue
            include = None
            for prefix, is_constraint in [('--requirement ', False), ('--requirement=', False), ('-r ', False), ('--constraint ', True), ('--constraint=', True), ('-c ', True), ('-r', False), ('-c', True)]:
                if line.startswith(prefix):
                    include = (line[len(prefix):].strip(), is_constraint or constraint)
                    break
            if include:
                parts = shlex.split(include[0])
                if len(parts) != 1 or parts[0].startswith('/'):
                    raise ArtifactContractError(f'Invalid package-local include: {include[0]}')
                path = posixpath.normpath((PurePosixPath(filename).parent / parts[0]).as_posix())
                read(path, include[1], (*parents, filename))
                continue
            try:
                if line.startswith(('https://', 'http://')):
                    wheel = PurePosixPath(unquote(urlsplit(line).path)).name
                    name, _, _, _ = parse_wheel_filename(wheel)
                    line = f'{name} @ {line}'
                req = Requirement(line)
                if req.url and urlsplit(req.url).scheme not in {'https', 'http', 'git+https'}:
                    raise ValueError('unsupported or nonportable dependency URL')
            except (ValueError, InvalidRequirement) as exc:
                raise ArtifactContractError(f'{filename}:{number}: unsupported dependency {line!r}: {exc}') from exc
            (constraints if constraint else dependencies).append(str(req))
    if 'requirements.txt' in files:
        read('requirements.txt')
    for constraint in constraints:
        con = Requirement(constraint)
        if con.url or con.extras:
            raise ArtifactContractError('Constraints must specify package versions, without extras or URLs')
    # Detect directly contradictory exact pins without running or importing code.
    pins = {}
    for dep in [*dependencies, *constraints]:
        req = Requirement(dep)
        if req.marker:
            continue
        key = req.name.lower().replace('_', '-')
        if req.url:
            previous = pins.setdefault(key + '@', req.url)
            if previous != req.url:
                raise ArtifactContractError(f'Conflicting dependency URLs for {req.name}')
        for spec in req.specifier:
            if spec.operator == '==' and '*' not in spec.version:
                pins.setdefault(key, set()).add(spec.version)
    for name, values in pins.items():
        if not name.endswith('@') and len(values) > 1:
            raise ArtifactContractError(f'Conflicting dependency versions for {name}: {sorted(values)}')
    dependencies = list(dict.fromkeys(dependencies))
    return (dependencies, list(dict.fromkeys(constraints))) if with_constraints else dependencies


def validate_package(files):
    for name in files:
        safe_path(name)
    for required in ('main.py', TASK_METADATA_FILENAME):
        if required not in files:
            raise ArtifactContractError(f'Missing {required}; {AUTHORING_GUIDE.splitlines()[0]}')
    metadata = parse_task_metadata(files[TASK_METADATA_FILENAME])
    for name, content in files.items():
        if name.endswith('.py'):
            try:
                ast.parse(content, filename=name, feature_version=(3, 11))
            except (SyntaxError, ValueError) as exc:
                raise ArtifactContractError(f'{name}: invalid Python syntax: {exc}') from exc
    dependencies, constraints = resolve_requirements(files, with_constraints=True)
    return {'metadata': metadata, 'data_contract': task_data_contract(metadata),
            'requirements': dependencies, 'constraints': constraints,
            'warnings': ([] if metadata.get('output', {}).get('schema') or metadata.get('data_contract', {}).get('outputs', [{}])[0].get('schema') else ['Output content schema not supplied; structural validation unavailable.']) + ['Model availability and execution have not been tested.']}
