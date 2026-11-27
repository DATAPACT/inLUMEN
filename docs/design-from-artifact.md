# Design from a deployment artifact or from code

**Import** on the canvas accepts four kinds of file. A Project JSON file is
read as before. The others are turned into a design by the backend:

| File | What becomes the design | Model |
|---|---|---|
| inLUMEN deployment bundle (`.zip`), or its `bundle-manifest.json` | The steps, port names and types, and connections listed in the manifest | not used |
| Argo Workflows file (`.yaml`, `.yml`, or JSON) of any origin, also inside a ZIP | One step per task of the entrypoint, one connection per dependency | not used |
| ZIP of code (for example a Python script with its README) | The steps that the model reads from the code | used, after you agree |

An inLUMEN bundle is the ZIP that inLUMEN's own export produces; it holds a
`bundle-manifest.json`. Any other ZIP with code files is a ZIP of code.

The import replaces the canvas, like a Project JSON import. Nothing in the
file is executed.

## inLUMEN bundle

The manifest (`inlumen.deployment-bundle@…`) is read from the top level of the
ZIP, or from one folder below it. Step types, templates (for example Parallel
Map and Collect), ports and connections come back as they were exported.

Not in the manifest, and therefore not restored: step descriptions,
parameters, code and data files. The code is in the bundle under `nodes/`;
attach it afterwards with **Upload code ZIP**.

## Argo Workflows

Read: `Workflow`, `WorkflowTemplate`, `ClusterWorkflowTemplate` and
`CronWorkflow`. The entrypoint template gives the steps:

- **`dag`**: one step per task. A connection is added for each entry of
  `dependencies`, each task named in `depends`, and each reference of the form
  `{{tasks.NAME.outputs…}}` in the task's arguments.
- **`steps`**: one step per entry. Every step of a group is connected to every
  step of the group before it.
- A task without a predecessor becomes a **Source**, a task without a
  successor a **Destination**, every other task a **Task**.
- An artifact or parameter argument that refers to another task names the
  input port. A task with several predecessors gets one input port for each.
- The step description says what the task runs (image and command), taken
  from its template.

Kept as text in the description, not as steps of their own:

- conditions (`when`) are not turned into Condition steps;
- loops (`withItems`, `withParam`, `withSequence`) are not turned into Parallel
  Map and Collect;
- a nested `dag` or `steps` template, and a template from another file
  (`templateRef`), stay one step.

Not read: port types (every port has the type "any"), parameters, exit
handlers, and the code inside the images. When a file holds several workflow
documents, the first is imported. A `Workflow` that only refers to a
`WorkflowTemplate` cannot be imported; import the template file.

After the import, a message lists what the file did not say. If the resulting
design does not pass validation, the message says why.

## ZIP of code

A ZIP without a bundle manifest and without an Argo workflow file is read as
code. A design cannot be read from code by rule, so the design agent infers
it with the model that is selected in the chat.

Before anything is sent, a dialog names the model and lists the files that it
would read, with what is sent of each. Only after **Send and build design**
are they sent:

- **Read whole** (up to 60,000 characters each, 200,000 in total, 60 files):
  `.py`, `.r`, `.sql`, `.sh`, `.md`, `.rst`, `.txt`, `.toml`, `.cfg`, `.ini`,
  `.yaml`, `.yml`, `.json`, and files named `Dockerfile`, `Makefile`, `README`.
- **First line only**: `.csv` and `.tsv` files, for the column names.
- **Never read**, only listed by name: every other file, hidden files and
  folders (names that start with a dot, such as `.env`), and the folders
  `__pycache__`, `node_modules`, `site-packages`, `dist`, `build`.

The agent is asked for one Source per input dataset, one Task per processing
stage that the code or its documentation names, and one Destination per
output. If its first design does not pass validation, it gets one more turn.

The finished design is laid out from left to right in the order of its
connections, each step in a row of its own, and the view is fitted to it. A
short message reports the number of steps and connections; **Details** opens
the model's summary of how the code maps to the steps.

What to expect:

- The design is the model's reading of the code. Check each step against the
  code. Two imports of the same ZIP can give different designs, for example
  one Task or three for the same function.
- No code or data is attached to the steps.
- The first line of a table is sent as it is. If a table has no header row,
  that line is data.
- Do not import code that you may not share with the provider of the selected
  model.

## API

`POST /api/pipeline/design-from-artifact` with a multipart `file`. The answer
holds `kind` (`inlumen-bundle`, `argo-workflow` or `code-zip`), `project`
(Project JSON), `notes`, `step_count` and `connection_count`. A file of no
supported kind is answered with status 422 and the reason. The endpoint
stores nothing.

For a ZIP of code the first answer holds no design: `needs_model` is true and
`files_to_send` and `files_not_sent` list the files. Send the request again
with the form fields `consent=true` and `llm_config` (JSON) to build the
design.

Limits: 50 MiB for a ZIP, 5 MiB for a manifest or workflow file, 500 steps,
4096 ZIP entries.

## Not covered

Dagster projects and other workflow formats are not read by rule; inside a
ZIP they are treated as code. Attaching the code of a ZIP to the steps is not
implemented. See issue #98.
