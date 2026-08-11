import { describe, expect, it } from "vitest";

import { validateGraph } from "@/features/flow/flowValidation";
import {
  conversationUnderstandingSubpipeline,
  deriveSubpipelineInterface,
  publicPortsForSubpipeline,
  remapSubpipelineParentEdges,
} from "@/features/flow/subpipeline";

describe("Subpipeline contracts", () => {
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
    expect(validateGraph(definition.graph.nodes, definition.graph.edges).valid).toBe(true);
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
