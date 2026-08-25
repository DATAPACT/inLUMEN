import { describe, expect, it } from "vitest";

import { buildDeploymentBundleRequest } from "@/features/deployment/deploymentBundleRequest";

describe("deployment bundle request", () => {
  it("includes the Argo workflow export in runnable bundles", () => {
    const dockerfileJson = { dockerfiles: [] };

    const request = buildDeploymentBundleRequest(dockerfileJson);

    expect(request.dockerfile_json).toBe(dockerfileJson);
    expect(request.targets).toEqual({ argo: true, dagster: true });
  });
});
