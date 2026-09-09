import { describe, expect, it } from "vitest";

import { validateGraph } from "@/features/flow/flowValidation";
import {
  conversationUnderstandingSubpipeline,
  deriveSubpipelineInterface,
  publicPortsForSubpipeline,
  remapSubpipelineParentEdges,
} from "@/features/flow/subpipeline";

describe("Subpipeline contracts", () => {
  it("allows one level in the main graph but rejects reuse of a graph containing a subpipeline", () => {
    const child = conversationUnderstandingSubpipeline();
    const node = { id: "reuse", position: { x: 0, y: 0 }, data: {
      type: "subpipeline", ports: publicPortsForSubpipeline(child),
      subpipeline: { reference: { pipeline_uid: "p", version_uid: "v" }, interface: child.interface, resolved_graph: child.graph },
    } };
    const depthIssues = (reusable: boolean) => validateGraph([node], [], { mode: "draft", reusable }).issues
      .filter((issue) => issue.code === "subpipeline-depth-exceeded");
    expect(depthIssues(false)).toHaveLength(0);
    expect(depthIssues(true)).toHaveLength(1);
    node.data.subpipeline.resolved_graph = { nodes: [node], edges: [], updated_at: null };
    expect(depthIssues(false)).toHaveLength(1);
  });
  it("builds a valid standalone Conversation Understanding pipeline", () => {
    const definition = conversationUnderstandingSubpipeline();

    expect(definition.graph.nodes).toHaveLength(6);
    expect(definition.graph.edges).toHaveLength(6);
    expect(definition.interface).toMatchObject({
      inputs: [{ id: "audio", type: "Audio", internal: { node: "conversation-input", port: "audio" } }],
      outputs: [{
        id: "conversation_analysis",
        type: "Object",
        internal: { node: "conversation-output", port: "conversation_analysis" },
      }],
    });
    const designValidation = validateGraph(
      definition.graph.nodes,
      definition.graph.edges,
      { mode: "draft" },
    );
    expect(designValidation.valid).toBe(true);
    [
      "transcription",
      "pii-redaction",
      "sentiment-analysis",
      "conversation-summary",
    ].forEach((nodeId) => {
      expect(designValidation.byNode[nodeId]).toEqual(expect.arrayContaining([
        expect.objectContaining({ code: "missing-code", severity: "warning" }),
      ]));
    });
  });

  it("derives public ports from nested boundaries and removes internal mappings", () => {
    const definition = conversationUnderstandingSubpipeline();
    const contract = deriveSubpipelineInterface(definition.graph);
    const publicPorts = publicPortsForSubpipeline({ interface: contract });

    expect(publicPorts.inputs).toEqual([
      expect.objectContaining({ id: "audio", name: "audio", type: "Audio" }),
    ]);
    expect(publicPorts.outputs).toEqual([
      expect.objectContaining({ id: "conversation_analysis", type: "Object" }),
    ]);
    expect(publicPorts.inputs[0]).not.toHaveProperty("internal");
  });

  it("remaps existing parent connections when the public contract becomes semantic", () => {
    const definition = conversationUnderstandingSubpipeline();
    const edges = remapSubpipelineParentEdges(
      "conversation",
      [
        { id: "incoming", source: "source", target: "conversation", sourceHandle: "data", targetHandle: "input" },
        { id: "outgoing", source: "conversation", target: "condition", sourceHandle: "output", targetHandle: "value" },
      ],
      {
        inputs: [{ id: "input", name: "input", type: "any", required: true, description: "" }],
        outputs: [{ id: "output", name: "output", type: "any", required: true, description: "" }],
      },
      publicPortsForSubpipeline(definition),
    );

    expect(edges[0].targetHandle).toBe("audio");
    expect(edges[1].sourceHandle).toBe("conversation_analysis");
  });
});
