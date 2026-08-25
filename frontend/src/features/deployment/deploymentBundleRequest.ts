export const buildDeploymentBundleRequest = <DockerfilePayload>(
  dockerfileJson: DockerfilePayload,
) => ({
  dockerfile_json: dockerfileJson,
  targets: { argo: true, dagster: true },
  validation_mode: "fast",
  validate_bundle: true,
  validation: {
    enabled: true,
    mode: "fast",
    materialize: false,
    validate_argo: false,
    validate_dagster: false,
    argo_lint: false,
    argo_dry_run: false,
  },
});
