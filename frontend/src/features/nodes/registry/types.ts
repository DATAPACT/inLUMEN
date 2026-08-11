import { z } from "zod";
import type { NodePorts, StepType } from "@/features/nodes/nodeSchema";

export const nodePaletteMetadataSchema = z.object({
  label: z.string().min(1),
  description: z.string(),
  icon: z.string().min(1),
  color: z.string().min(1),
  order: z.number().int().default(0),
});

export const nodeEditorDescriptorSchema = z.object({
  kind: z.string().min(1),
  catalog: z.string().min(1).nullable().optional(),
});

export const nodeRuntimeDescriptorSchema = z.object({
  generator: z.string().min(1),
  template: z.string().min(1).nullable().optional(),
  base_image: z.string().min(1).nullable().optional(),
});

export const nodeDefinitionSchema = z.object({
  id: z.string().min(1),
  version: z.number().int().positive(),
  base_type: z.enum([
    "source",
    "task",
    "destination",
    "flow",
    "subpipeline",
  ]),
  family: z.string().min(1),
  operation: z.string().min(1).nullable().optional(),
  enabled: z.boolean().default(true),
  palette: nodePaletteMetadataSchema,
  editor: nodeEditorDescriptorSchema,
  runtime: nodeRuntimeDescriptorSchema,
  default_implementation: z.record(z.unknown()).default({}),
});

export const nodeDefinitionResponseSchema = z.object({
  schema_version: z.literal(1),
  definitions: z.array(nodeDefinitionSchema),
});

export type NodePaletteMetadata = z.infer<typeof nodePaletteMetadataSchema>;
export type NodeEditorDescriptor = z.infer<typeof nodeEditorDescriptorSchema>;
export type NodeRuntimeDescriptor = z.infer<typeof nodeRuntimeDescriptorSchema>;
export type NodeDefinition = z.infer<typeof nodeDefinitionSchema>;

export type NodeInstanceImplementation = Record<string, unknown>;

export type NodeDefinitionData = {
  label: string;
  description: string;
  type: StepType;
  definition_id: string;
  definition_version: number;
  implementation: NodeInstanceImplementation;
  template_label: string;
  template: { id: string; name: string; version?: number };
  ports: NodePorts;
  param: Record<string, unknown>;
  configuration_status?: "unconfigured" | "valid" | "invalid";
};
