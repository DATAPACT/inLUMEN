const buildDeploymentBundleRequest = <DockerfilePayload>(
  dockerfileJson: DockerfilePayload,
  target: "argo" | "dagster",
) => ({
  dockerfile_json: dockerfileJson,
  targets: { argo: target === "argo", dagster: target === "dagster" },
  validation_mode: "fast",
  validate_bundle: true,
  validation: {
    enabled: true,
    mode: "fast",
    materialize: false,
    validate_argo: target === "argo",
    validate_dagster: target === "dagster",
    argo_lint: false,
    argo_dry_run: false,
  },
});

export const buildDagsterBundleRequest = <DockerfilePayload>(
  dockerfileJson: DockerfilePayload,
) => buildDeploymentBundleRequest(dockerfileJson, "dagster");

export const buildArgoBundleRequest = <DockerfilePayload>(
  dockerfileJson: DockerfilePayload,
) => buildDeploymentBundleRequest(dockerfileJson, "argo");
