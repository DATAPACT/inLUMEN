import { describe, expect, it } from "vitest";

import {
  buildArgoBundleRequest,
  buildDagsterBundleRequest,
} from "@/features/deployment/deploymentBundleRequest";

describe("deployment bundle request", () => {
  it("keeps Dagster and Argo export requests separate", () => {
    const dockerfileJson = { dockerfiles: [] };

    const dagster = buildDagsterBundleRequest(dockerfileJson);
    const argo = buildArgoBundleRequest(dockerfileJson);

    expect(dagster.dockerfile_json).toBe(dockerfileJson);
    expect(dagster.targets).toEqual({ argo: false, dagster: true });
    expect(dagster.validation.validate_dagster).toBe(true);
    expect(argo.targets).toEqual({ argo: true, dagster: false });
    expect(argo.validation.validate_argo).toBe(true);
  });
});
