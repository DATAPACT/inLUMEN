# inlumen-agents

inLUMEN's pipeline design and code generation agents as one Python package, used
by the inLUMEN backend, the codegen service and `inlumen-bench`. See
[ADR 0005](../../docs/adr/0005-design-and-codegen-libraries.md).

| Module | Contents | Status |
| --- | --- | --- |
| `inlumen.contracts` | Port types, Parallel Map scopes, the artifact runtime, the Task package contract | Done |
| `inlumen.design` | The pipeline design agent | Planned |
| `inlumen.codegen` | The code generator | Planned |

## Install

In this repository (editable, for development):
```sh
uv pip install -e packages/agents
```
From another project, pinned to a release tag:
```sh
pip install "inlumen-agents @ git+https://github.com/DATAPACT/inLUMEN@<tag>#subdirectory=packages/agents"
```

## Tests
```sh
cd packages/agents && uv run --extra dev pytest
```
