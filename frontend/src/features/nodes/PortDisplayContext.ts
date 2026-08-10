import { createContext } from "react";
import type { ValidationIssue } from "@/features/flow/flowValidation";

export type PortDisplayState = {
  advanced: boolean;
  validationByNode: Record<string, ValidationIssue[]>;
};

export const PortDisplayContext = createContext<PortDisplayState>({
  advanced: false,
  validationByNode: {},
});
