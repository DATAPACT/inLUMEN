# Plugins

Plugins let a pipeline step use a third-party tool that **already runs as a
deployed service with a REST API**, for example the
[JSI Wikifier](https://wikifier.org/info.html) or SINTEF's
[Moose](https://github.com/DATAPACT/moose) DPV annotator (both built in). There is no plugin store or
catalog: when a tool owner tells us how their API works (endpoint, parameters,
how to authenticate), we write a small JSON definition for it. inLUMEN never
hosts or runs the tool's code; a plugin step runs a small platform-owned adapter
(standard library only) that calls the tool's endpoint.

## Adding a tool

1. Get from the tool owner: the base URL, the operation(s) to call, the request
   format, the parameters, and how API keys are passed. Get a key for testing.
2. Write `<owner>-<tool>.plugin.json` (format below). Start from
   [`jsi-wikifier.plugin.json`](../backend/plugins/manifests/jsi-wikifier.plugin.json).
3. Make it available, in one of two ways:
   - **Built-in (preferred):** commit it to `backend/plugins/manifests/`. It is
     reviewed like code, ships with every deployment, and is loaded on start
     (`INLUMEN_PLUGIN_DIR` points elsewhere if needed). The test suite validates
     every file in that folder.
   - **Added in the UI:** an application administrator pastes or uploads the file
     in **Lab → Plugins → Manage → Add plugin**. Useful for trying a tool or for a
     tool specific to one installation. A UI definition with the same id overrides
     the built-in one; removing it falls back to the built-in file.
4. Users drag the plugin from the **Plugins** palette group (or pick it in the
   **Plugin** section of a step), fill in its parameters and secret, and run.

## How it runs

- The step stores `template_label = "plugin:<id>"`.
- Secrets (API keys) are entered in the inspector, stored encrypted in the
  node-secret store and never written to the graph or deployment bundles; the
  backend forces every declared secret to be treated as sensitive.
- On run or export the step gets the plugin adapter. Its `main.py` embeds a
  secret-free spec (endpoint, operation, parameter declarations and non-secret
  values). Secrets arrive at run time as `INLUMEN_PARAM_<NAME>` (Argo
  `secretKeyRef`, Dagster `secret_environment`), like any sensitive parameter.
- The Pipeline Designer agent sees available plugins in
  `list_pipeline_components` (name, type, description). It reads a plugin's notes
  with `get_plugin_instructions` only when one matches the request, then uses
  `plugin:<id>` as the step template. It never sets parameters or secrets.

## Manifest (`inlumen.plugin@1`)

Schema: [`contracts/plugin-manifest-v1/schema.json`](../contracts/plugin-manifest-v1/schema.json).
Examples: [`backend/plugins/manifests/jsi-wikifier.plugin.json`](../backend/plugins/manifests/jsi-wikifier.plugin.json) (real tool) and
[`examples/plugins/text-anonymizer.plugin.json`](../examples/plugins/text-anonymizer.plugin.json) (local mock tool).

| Field | Meaning |
| --- | --- |
| `id`, `version`, `name`, `description`, `publisher` | Identity. `id` is dotted lowercase (`<owner>.<tool>`, e.g. `jsi.wikifier`); `version` is semver. |
| `step_type` | `source`, `task` or `destination`. |
| `endpoint.base_url` | Where the deployed tool is reachable from inLUMEN runtimes. `health_path` is used by **Check endpoint**. |
| `operations[]` | `id`, `method`, `path` (may contain `{param}` placeholders), `request.body`, `response`, `timeout_seconds`. The first operation is the default. |
| `parameters[]` | User-configured values: `name`, `type`, `required`, `default`, `enum`, `location` (`query`, `header`, `path`, `body`), optional `operations` filter. `api_name` is the name on the wire when it differs (a header such as `X-LLM-Endpoint`, or a dotted path such as `llm.model` that nests a JSON body field). |
| `auth` | `none`, `api_key` (sent in a `header`, `query_parameter` or `form_field`) or `bearer`; `secret` names the secret that holds the credential. |
| `secrets[]` | Declared credentials (`name`, `label`, `description`, `required`, optional `header` to send it as an extra header when a tool needs several keys). Values are never part of a manifest. |
| `ports` | At most one input and one output port (defaults: `data` / `input` → `output`). |
| `instructions` | Guidance for the Pipeline Designer agent: when (not) to use the tool. |

`request.body` decides what is sent:

- `none` – no body (default for `GET`/`DELETE`).
- `parameters` – a JSON object of the `body` parameters (default for sources); with `input_field`, the text of each input file is put in that (dotted) field, one request per file (used by Moose: `input_field: "text"`).
- `form` – `application/x-www-form-urlencoded` `body` parameters; with `input_field`, the text of each input file is sent in that field, one request per file (used by Wikifier: `input_field: "text"`).

  The text of an input file is its UTF-8 content, except for a JSON document:
  then its `transcript`, `text`, `content` or `body` field is sent (the first
  that is a non-empty string), so a Speech-to-Text result sends the transcript
  rather than its segments and metadata. JSON without such a field fails before
  the tool is called.
- `input` – the raw bytes of each input file, one request per file (default for tasks/destinations).
- `multipart` – each input file as a `multipart/form-data` upload (`file_field`, default `file`) plus `body` parameters as form fields.

`response` decides what a Source or Task publishes: `format` (default `json`;
JSON responses are validated), `filename` (default `response.<format>`), and
`representation`. `file` requires exactly one call (one input file); `directory`
(Tasks only) writes one result per input file into the named directory.
Destinations write a `delivery-receipt.json` instead.
`async` handles tools that queue a job: the first response must contain a job id
(`job_id_field`); the adapter polls `poll_path` (e.g. `/jobs/{job_id}`) with the
same credentials every `interval_seconds` until `status_field` is in
`done_values` (it then publishes `result_field`) or `failed_values` (the step
fails with `error_field`), giving up after `timeout_seconds`.

`response.error_fields` lists top-level JSON fields that mean failure even with
HTTP 200 (Wikifier answers a bad key with `{"status": "user-key-not-found"}`).

### Passing the input on (`response.merge`)

An annotation tool answers with annotations only, so in a linear chain such as
*Speech-to-Text → Wikifier → Sentiment Analysis* the last step would get
annotations and no transcript. An operation with `response.merge`
(`{"key": "wikifier", "filename": "annotated.json"}`; requires a JSON response
and `request.input_field`) gives the step an **Output** setting, the step
parameter `output_mode`:

- `annotations` – **Annotations only** (default): publish the tool's response,
  as without `merge`.
- `input_with_annotations` – **Input with annotations**: publish the input with
  the response added under `key`, in `merge.filename` (default
  `annotated.json`). A JSON object input keeps all its fields (a transcript's
  `transcript`, `text`, segments and metadata), so later steps still find the
  text; plain text becomes `{"text": <input>, "<key>": <response>}`.

```json
{"transcript": "Rome is the capital of Italy.", "segments": [...],
 "wikifier": {"annotations": [...], "words": [...], "spaces": [...]}}
```

The step's declared output (name, filename, `json` kind and format) follows the
chosen mode. Both built-in plugins support it: Wikifier under `wikifier`, Moose
under `dpv`. Steps keep explicit connections; a step that needs only the
annotations reads `wikifier` (or `dpv`) from the enriched document.

Step parameters reserved by the adapter: `operation`, `timeout`,
`output_filename`, `output_mode` (and `base_url`, which is reserved but ignored: the endpoint is
always the one in the plugin definition). The adapter
never follows HTTP redirects, so credentials cannot be forwarded to another host.
Optional secrets are added to a step only when the user enters them.

## API

| Endpoint | Who | Purpose |
| --- | --- | --- |
| `GET /api/plugins` | any user | Available plugins (built-in and UI-added, with `source`) and `can_manage`. |
| `GET /api/plugins/{id}` | any user | One plugin with its definition. |
| `POST /api/plugins/{id}/check` | editors | Probe `base_url + health_path`; never sends credentials. |
| `POST /api/admin/plugins` | application admin | Add or update: `{"manifest": {...}}`. |
| `DELETE /api/admin/plugins/{id}` | application admin | Remove a UI-added plugin (built-in files return 409). Steps using a missing plugin fail validation with `plugin-not-installed`. |

## Try it

**Wikifier (real service).** Register for a free user key at
<https://wikifier.org/register.html>. Build *Source (upload a .txt file) → JSI
Wikifier → Destination*, enter the key in the Wikifier step, and run. Note the
public service is occasionally down.

**Offline example.** `docker compose --profile plugins-demo up -d mock-tool`
starts a local "Text Anonymizer" (API key `demo-key`). Add
`examples/plugins/text-anonymizer.plugin.json` in **Add plugin** and use it like
any other tool. Without Docker run `python examples/plugins/mock_tool_server.py`
and change the definition's `base_url` to `http://localhost:8099/tools/text-anonymizer`.

## Built-in plugins

| Plugin | What it does | Keys |
| --- | --- | --- |
| JSI Wikifier (`jsi.wikifier`) | Links concepts in text to Wikipedia/Wikidata. | Wikifier user key ([register](https://wikifier.org/register.html)). |
| Moose DPV Annotation (`datapact.moose-dpv`) | Annotates text with W3C DPV concepts (`dpv`) or DPV personal-data categories (`dpv_pd`); asynchronous. | Moose API key (from SINTEF) and your LLM provider key (not needed for Ollama). Set the LLM provider and model on the step. |

## Limits and next steps

- One input and one output port per plugin.
- No automatic retries; a failed call fails the step with the tool's HTTP status
  and (truncated) error body.
- OpenAPI import is not implemented; `endpoint.openapi_url` is informational.
