# Design from a deployment artifact

**Import** on the canvas accepts three kinds of file. A Project JSON file is
read as before. The other two are turned into a design by the backend, without
a model:

| File | What becomes the design |
|---|---|
| inLUMEN deployment bundle (`.zip`), or its `bundle-manifest.json` | The steps, port names and types, and connections listed in the manifest |
| Argo Workflows file (`.yaml`, `.yml`, or JSON) of any origin | One step per task of the entrypoint, one connection per dependency |

The import replaces the canvas, like a Project JSON import. Nothing in the
file is executed, and only the bundle manifest is read from a ZIP.

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

## API

`POST /api/pipeline/design-from-artifact` with a multipart `file`. The answer
holds `kind` (`inlumen-bundle` or `argo-workflow`), `project` (Project JSON),
`notes`, `step_count` and `connection_count`. A file that is neither kind is
answered with status 422 and the reason. The endpoint stores nothing.

Limits: 50 MiB for a ZIP, 5 MiB for a manifest or workflow file, 500 steps.

## Not covered

Building a design from a ZIP of code files, from a Dagster project, or from
other workflow formats is not implemented. See issue #98.
