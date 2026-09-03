import React, { useState, useEffect, useRef, useCallback } from 'react';
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { cn } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import {
  Eye,
  EyeOff,
  GitBranch,
  Loader2,
  Plus,
  Upload,
  Wand2,
  X,
} from 'lucide-react';
import { toast } from "sonner";
import { FilePreviewDialog, PreviewType } from '@/components/properties/FilePreviewDialog';
import { ChatbotConfig } from '@/services/chatbotService';
import {
  normalizeType,
  getStepTypeLabel,
  pickBackendUpdatableProps,
  StepType,
  normalizeNodePorts,
  normalizeSecretParamKeys,
  isSensitiveParameterName,
  withoutSensitiveParameterValues,
  getNodeFileBucket,
  getNodeFileName,
  getNodeFileRole,
  isBrowserFile,
  typeHasContent,
  typeHasFiles,
  isImagePreviewName,
  isTextPreviewName,
  isTextPreviewFile,
  NodeFileReference,
  NodeFileRole,
  GeneratedArtifact,
  NodePorts,
} from '@/features/nodes/nodeSchema';
import {
  nodeSupportsInputFiles,
  taskImplementationMigrationError,
  taskImplementationStatus,
} from '@/features/nodes/propertyPanelPolicy';
import {
  defaultParametersForTemplate,
  defaultTemplateForType,
  findTemplateForType,
  templateOptionsForType,
} from '@/features/nodes/templateCatalog';
import {
  readNodeFile,
  removeNodeFile,
  generateNodeScript,
  deleteNodeSecret,
  fetchConfiguredNodeSecrets,
  storeNodeSecret,
  updateNodeTextFile,
  updateNodePropertiesInBackend,
  uploadNodeFile,
} from '@/features/nodes/nodePersistence';
import { Node } from 'reactflow';
import type { ValidationIssue } from '@/features/flow/flowValidation';
import {
  publicPortsForSubpipeline,
  type SubpipelineDefinition,
  type SubpipelineInterface,
  type SubpipelineReference,
} from '@/features/flow/subpipeline';
import {
  attachReusablePipelineVersion,
  fetchReusablePipelines,
  previewReusablePipelineAttachment,
  type ReusablePipelineAttachment,
  type ReusablePipelineSummary,
  type ReusablePipelineVersion,
} from '@/features/flow/subpipelinePersistence';

type NodeParamMap = Record<string, unknown>;
const FLOW_PARAMETER_KEYS = new Set(["expression", "max_concurrency", "failure_policy"]);
const USER_PARAMETER_NAME_PATTERN = /^[A-Za-z_][A-Za-z0-9_.-]*$/;
const USER_CODE_FILE_PATTERN = /^(?:main\.py|requirements\.txt)$/;

export type PropertyNodeData = {
  label?: string;
  description?: string;
  type?: StepType | string;
  content?: string;
  files?: NodeFileReference[];
  has_files?: string;
  param?: NodeParamMap;
  secret_params?: string[];
  ports?: Partial<NodePorts>;
  template_label?: string;
  template?: { id: string; name: string; version?: number };
  definition_id?: string;
  definition_version?: number;
  implementation?: Record<string, unknown>;
  configuration_status?: "unconfigured" | "valid" | "invalid";
  generated_artifact?: GeneratedArtifact;
  validation_issues?: ValidationIssue[];
  connected_ports?: { inputs?: string[]; outputs?: string[] };
  subpipeline?: {
    version?: number;
    expanded?: boolean;
    reference?: SubpipelineReference;
    interface?: SubpipelineInterface;
    resolved_graph?: { nodes: unknown[]; edges: unknown[] };
    graph?: { nodes: unknown[]; edges: unknown[] }; // v1 compatibility only
    resolution_error?: string;
  };
  [key: string]: unknown;
};

const normalizeFileReferences = (value: unknown): NodeFileReference[] => {
  if (!Array.isArray(value)) return [];
  return value.filter((file): file is NodeFileReference => {
    return getNodeFileName(file as NodeFileReference).length > 0;
  });
};

const uploadedFileReference = (
  nodeId: string,
  fileName: string,
  role: NodeFileRole,
): NodeFileReference => ({
  filename: fileName,
  bucket: `files-step-id-${nodeId}`.toLowerCase(),
  role,
});

const withNodeFileRole = (
  file: NodeFileReference,
  role: NodeFileRole,
): NodeFileReference => {
  if (typeof file === "string" || isBrowserFile(file)) {
    return { filename: getNodeFileName(file), role };
  }
  return { ...file, role };
};

const InspectorSection = ({
  id,
  title,
  description,
  status,
  children,
}: {
  id?: string;
  title: string;
  description?: string;
  status?: "error" | "warning";
  children: React.ReactNode;
}) => (
  <section
    id={id}
    className={cn(
      "scroll-mt-4 space-y-3 rounded-lg border bg-muted/10 p-3",
      status === "error"
        ? "border-destructive/60 bg-destructive/5"
        : status === "warning"
          ? "border-amber-500/60 bg-amber-500/5"
          : "border-border",
    )}
  >
    <div>
      <h3 className="text-sm font-semibold">{title}</h3>
      {description && (
        <p className="mt-1 text-xs text-muted-foreground">{description}</p>
      )}
    </div>
    {children}
  </section>
);

interface PropertiesPanelProps {
  selectedNode: Node<PropertyNodeData> | null;
  onNodeUpdate: (
    id: string,
    data: PropertyNodeData,
    options?: { remapSubpipeline?: boolean },
  ) => void;
  onRemoveNode?: (nodeId: string) => void;
  onGenerateCode?: (nodeId: string) => void;
  activeChatbotConfig?: ChatbotConfig | null;
  className?: string;
}

export function PropertiesPanel({
  selectedNode,
  onNodeUpdate,
  onRemoveNode,
  onGenerateCode,
  activeChatbotConfig,
  className,
}: PropertiesPanelProps) {
  const nodeType: StepType = normalizeType(selectedNode?.data?.type ?? selectedNode?.type);
  const canManageFiles = typeHasFiles(nodeType);
  // Code is a Task concern. Sources and Destinations are connectivity boundaries
  // whose adapters are resolved when the runnable bundle is assembled.
  const canManageImplementation = nodeType === "task";
  const canGenerateScript = canManageImplementation;

  const [label, setLabel] = useState('');
  const [description, setDescription] = useState('');

  // Sources own run input files; Tasks own a minimal Python implementation.
  const [files, setFiles] = useState<NodeFileReference[]>([]);
  const codeFileInputRef = useRef<HTMLInputElement>(null);
  const dataFileInputRef = useRef<HTMLInputElement>(null);

  // Parameters belong to the node inspector, never to separate graph nodes.
  const [param, setParam] = useState<NodeParamMap>({});
  const [secretParamKeys, setSecretParamKeys] = useState<string[]>([]);
  const [secretDrafts, setSecretDrafts] = useState<Record<string, string>>({});
  const [configuredSecretParams, setConfiguredSecretParams] = useState<Set<string>>(
    () => new Set(),
  );
  const [newParameterName, setNewParameterName] = useState("");
  const [revealedSecretParams, setRevealedSecretParams] = useState<Set<string>>(
    () => new Set(),
  );
  const [ports, setPorts] = useState<NodePorts>(() => normalizeNodePorts(undefined, nodeType));
  const [reusablePipelines, setReusablePipelines] = useState<ReusablePipelineSummary[]>([]);
  const [selectedReusableVersion, setSelectedReusableVersion] = useState("");
  const [isLoadingReusablePipelines, setIsLoadingReusablePipelines] = useState(false);
  const [pendingAttachment, setPendingAttachment] = useState<ReusablePipelineAttachment | null>(null);
  const [isAttachmentReviewOpen, setIsAttachmentReviewOpen] = useState(false);
  const [isAttachingReusablePipeline, setIsAttachingReusablePipeline] = useState(false);
  const [attachmentInputMapping, setAttachmentInputMapping] = useState<Record<string, string>>({});
  const [attachmentOutputMapping, setAttachmentOutputMapping] = useState<Record<string, string>>({});
  const currentTemplate = String(
    selectedNode?.data?.template?.name
      || selectedNode?.data?.template_label
      || defaultTemplateForType(nodeType),
  );
  const canManageInputFiles = nodeSupportsInputFiles(nodeType, currentTemplate);
  const editableParamEntries: Array<[string, unknown]> = Object.entries(param).filter(([key]) => (
    key !== "model_plan" && !(nodeType === "flow" && FLOW_PARAMETER_KEYS.has(key))
  ));
  const templateOptions = templateOptionsForType(nodeType, currentTemplate);
  const showTemplateSelector = templateOptions.length > 1;
  const templateGroups = Array.from(new Set(templateOptions.map((option) => option.category)))
    .map((category) => ({
      category,
      options: templateOptions.filter((option) => option.category === category),
    }));
  const connectorTemplateGroups = templateGroups
    .map((group) => ({
      ...group,
      options: group.options.filter((option) => [
        "Custom",
        "File",
        "Folder",
        "Database",
        "Object Storage",
        "REST API",
        "Kafka",
      ].includes(option.value)),
    }))
    .filter((group) => group.options.length > 0);
  const indexedCodeFiles = files
    .map((file, index) => ({ file, index }))
    .filter(({ file }) => getNodeFileRole(file) === "code");
  const indexedDataFiles = files
    .map((file, index) => ({ file, index }))
    .filter(({ file }) => getNodeFileRole(file) === "data");
  const activeNodeIdRef = useRef<string | null>(selectedNode?.id ?? null);
  const locallyProducedNodeDataRef = useRef(new WeakSet<object>());
  const onNodeUpdateRef = useRef(onNodeUpdate);

  useEffect(() => {
    onNodeUpdateRef.current = onNodeUpdate;
  }, [onNodeUpdate]);

  // preview/edit file dialog state
  const [previewFile, setPreviewFile] = useState<File | null>(null);
  const [previewFileName, setPreviewFileName] = useState('');
  const [previewContent, setPreviewContent] = useState('');
  const [previewType, setPreviewType] = useState<PreviewType>('text');
  const [isPreviewLoading, setIsPreviewLoading] = useState(false);
  const [canEditPreview, setCanEditPreview] = useState(false);
  const [isEditing, setIsEditing] = useState(false);
  const [editedContent, setEditedContent] = useState('');
  const [previewFileIndex, setPreviewFileIndex] = useState<number>(-1);
  const [isGeneratingScript, setIsGeneratingScript] = useState(false);

  // Debounce backend updates to avoid POST per keystroke
  const backendDebounceRef = useRef<number | null>(null);
  const debouncedUpdatePropertyToBackend = useCallback((nodeId: string, properties: Record<string, unknown>) => {
    if (backendDebounceRef.current) window.clearTimeout(backendDebounceRef.current);
    backendDebounceRef.current = window.setTimeout(() => {
      updateNodePropertiesInBackend(nodeId, properties);
    }, 300);
  }, []);

  useEffect(() => {
    return () => {
      if (backendDebounceRef.current) window.clearTimeout(backendDebounceRef.current);
    };
  }, []);

  // Enforce type-specific rules before persisting into node.data
  const pushNodeUpdate = (patch: Partial<PropertyNodeData>) => {
    if (!selectedNode) return;

    const next: PropertyNodeData = { ...selectedNode.data, ...patch, type: nodeType };

    // Content is boundary metadata only for sources and destinations.
    if (!typeHasContent(nodeType)) {
      delete next.content;
    } else {
      if (next.content == null) next.content = "";
    }

    // Files are supported on every runtime node; has_files is internal/derived.
    if (!typeHasFiles(nodeType)) {
      delete next.files;
      delete next.has_files;
    } else {
      const filesArr = normalizeFileReferences(next.files);
      next.files = filesArr;
      next.has_files = filesArr.length > 0 ? "yes" : "no"; // internal
    }

    next.param = next.param && typeof next.param === "object" && !Array.isArray(next.param)
      ? next.param
      : {};
    next.secret_params = normalizeSecretParamKeys(next.secret_params, next.param);
    next.param = withoutSensitiveParameterValues(next.param, next.secret_params);
    next.ports = normalizeNodePorts(next.ports, nodeType);

    // 1) update local reactflow node
    locallyProducedNodeDataRef.current.add(next);
    onNodeUpdate(selectedNode.id, next);

    // 2) update backend state (only allowed props)
    const backendProps = pickBackendUpdatableProps(selectedNode.id, next, nodeType);
    debouncedUpdatePropertyToBackend(selectedNode.id, backendProps);
  };

  useEffect(() => {
    const nextNodeId = selectedNode?.id ?? null;
    const nodeChanged = activeNodeIdRef.current !== nextNodeId;
    if (nodeChanged) {
      setRevealedSecretParams(new Set());
      setSecretDrafts({});
      setConfiguredSecretParams(new Set());
      setNewParameterName("");
      activeNodeIdRef.current = nextNodeId;
    }
    if (
      !nodeChanged
      && selectedNode
      && locallyProducedNodeDataRef.current.has(selectedNode.data)
    ) {
      return;
    }
    if (selectedNode) {
      setLabel(selectedNode.data.label || '');
      setDescription(selectedNode.data.description || '');

      // Runtime artifacts can be attached to any structural component.
      setFiles(typeHasFiles(nodeType) ? normalizeFileReferences(selectedNode.data.files) : []);

      const p = selectedNode.data.param;
      const nextParam = (p && typeof p === "object" && !Array.isArray(p))
        ? p as NodeParamMap
        : {};
      const nextSecrets = normalizeSecretParamKeys(selectedNode.data.secret_params, nextParam);
      const legacySecretDrafts = Object.fromEntries(nextSecrets
        .filter((key) => String(nextParam[key] ?? "").length > 0)
        .map((key) => [key, String(nextParam[key])]));
      const sanitizedParam = withoutSensitiveParameterValues(nextParam, nextSecrets);
      setParam(sanitizedParam);
      setSecretParamKeys(nextSecrets);
      setSecretDrafts(legacySecretDrafts);
      if (Object.keys(legacySecretDrafts).length > 0) {
        const sanitizedData = {
          ...selectedNode.data,
          param: sanitizedParam,
          secret_params: nextSecrets,
        };
        onNodeUpdateRef.current(selectedNode.id, sanitizedData);
        void updateNodePropertiesInBackend(
          selectedNode.id,
          pickBackendUpdatableProps(selectedNode.id, sanitizedData, nodeType),
        );
        void Promise.all(Object.entries(legacySecretDrafts).map(async ([key, value]) => {
          await storeNodeSecret(selectedNode.id, key, value);
          setConfiguredSecretParams((current) => new Set([...current, key]));
        })).catch((error) => {
          toast.error("Could not move a sensitive value to secure storage", {
            description: error instanceof Error ? error.message : "Unknown error",
          });
        });
      }
      setPorts(normalizeNodePorts(selectedNode.data.ports, nodeType));
    } else {
      setLabel('');
      setDescription('');
      setFiles([]);
      setParam({});
      setSecretParamKeys([]);
      setSecretDrafts({});
      setConfiguredSecretParams(new Set());
      setPorts(normalizeNodePorts(undefined, nodeType));
    }

    // reset preview dialog
    setPreviewFile(null);
    setPreviewFileName('');
    setPreviewContent('');
    setPreviewType('text');
    setIsPreviewLoading(false);
    setCanEditPreview(false);
    setIsEditing(false);
    setEditedContent('');
    setPreviewFileIndex(-1);
  }, [selectedNode, nodeType]);

  useEffect(() => {
    if (!selectedNode?.id) return;
    let cancelled = false;
    void fetchConfiguredNodeSecrets(selectedNode.id)
      .then((names) => {
        if (!cancelled) {
          setConfiguredSecretParams((current) => new Set([...current, ...names]));
        }
      })
      .catch(() => {
        if (!cancelled) setConfiguredSecretParams(new Set());
      });
    return () => { cancelled = true; };
  }, [selectedNode?.id]);

  const selectedSubpipelinePipelineUid = selectedNode?.data.subpipeline?.reference?.pipeline_uid || "";
  const selectedSubpipelineVersionUid = selectedNode?.data.subpipeline?.reference?.version_uid || "";

  useEffect(() => {
    if (nodeType !== "subpipeline" || !selectedNode?.id) return;
    let cancelled = false;
    setIsLoadingReusablePipelines(true);
    void fetchReusablePipelines()
      .then((pipelines) => {
        if (cancelled) return;
        setReusablePipelines(pipelines);
        setSelectedReusableVersion(
          selectedSubpipelinePipelineUid && selectedSubpipelineVersionUid
            ? `${selectedSubpipelinePipelineUid}::${selectedSubpipelineVersionUid}`
            : "",
        );
      })
      .catch((error) => {
        if (!cancelled) toast.error("Could not load reusable pipelines", {
          description: error instanceof Error ? error.message : "Unknown error",
        });
      })
      .finally(() => {
        if (!cancelled) setIsLoadingReusablePipelines(false);
      });
    return () => { cancelled = true; };
  }, [
    nodeType,
    selectedNode?.id,
    selectedSubpipelinePipelineUid,
    selectedSubpipelineVersionUid,
  ]);

  const handleLabelChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    setLabel(e.target.value);
    pushNodeUpdate({ label: e.target.value });
  };

  const handleDescriptionChange = (
    e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>,
  ) => {
    setDescription(e.target.value);
    pushNodeUpdate({ description: e.target.value });
  };

  const handleTemplateChange = (templateLabel: string) => {
    const template = findTemplateForType(nodeType, templateLabel);
    const nextPorts = normalizeNodePorts(template?.ports, nodeType);
    const connected = selectedNode?.data.connected_ports || {};
    const removesConnectedPort = (direction: keyof NodePorts) =>
      (connected[direction] || []).some((portId) =>
        !nextPorts[direction].some((port) => port.id === portId),
      );
    if (removesConnectedPort("inputs") || removesConnectedPort("outputs")) {
      toast.error("Template change would break connections", {
        description: "Disconnect or remap the affected connections before choosing this template.",
      });
      return;
    }
    const templateDefaults = defaultParametersForTemplate(nodeType, templateLabel);
    const nextParam = nodeType === "flow"
      ? {
          ...Object.fromEntries(Object.entries(param).filter(([key]) => !FLOW_PARAMETER_KEYS.has(key))),
          ...templateDefaults,
        }
      : { ...templateDefaults, ...param };
    const nextSecretParams = Array.from(new Set([
      ...secretParamKeys,
      ...(template?.configurationFields || [])
        .filter((field) => field.secret)
        .map((field) => field.name),
    ])).filter((key) => key in nextParam);
    const replaceGenericFlowLabel = nodeType === "flow" && ["Flow", "Condition", "Parallel Map"].includes(label);
    const nextLabel = replaceGenericFlowLabel ? (template?.label || templateLabel) : label;
    setPorts(nextPorts);
    setParam(nextParam);
    setSecretParamKeys(nextSecretParams);
    if (nextLabel !== label) setLabel(nextLabel);
    pushNodeUpdate({
      template_label: templateLabel,
      template: {
        id: template?.id || `custom.${templateLabel.toLowerCase().replace(/[^a-z0-9]+/g, '-')}`,
        name: templateLabel,
      },
      ports: nextPorts,
      param: nextParam,
      secret_params: nextSecretParams,
      ...(nodeType === "flow" ? { label: nextLabel } : {}),
    });
  };

  const setFlowParameter = (key: string, value: unknown) => {
    const next = { ...param, [key]: value };
    setParam(next);
    pushNodeUpdate({ param: next, secret_params: secretParamKeys });
  };

  const applyReusablePipelineVersion = (
    version: ReusablePipelineVersion,
    options: { persist?: boolean } = {},
  ) => {
    const stableDefinition: SubpipelineDefinition = {
      version: 2,
      reference: version.reference,
      interface: version.interface,
      resolved_graph: version.graph,
      expanded: false,
    };
    const nextPorts = publicPortsForSubpipeline(stableDefinition);
    setPorts(nextPorts);
    setSelectedReusableVersion(`${version.reference.pipeline_uid}::${version.reference.version_uid}`);
    if (options.persist !== false) {
      pushNodeUpdate({ ports: nextPorts, subpipeline: stableDefinition });
    } else if (selectedNode) {
      onNodeUpdate(selectedNode.id, {
        ...selectedNode.data,
        ports: nextPorts,
        subpipeline: stableDefinition,
      }, { remapSubpipeline: false });
    }
  };

  const refreshReusablePipelineCatalog = async () => {
    const pipelines = await fetchReusablePipelines();
    setReusablePipelines(pipelines);
    return pipelines;
  };

  const handleRefreshReusablePipelineCatalog = async () => {
    try {
      setIsLoadingReusablePipelines(true);
      await refreshReusablePipelineCatalog();
    } catch (error) {
      toast.error("Could not refresh reusable pipelines", {
        description: error instanceof Error ? error.message : "Unknown error",
      });
    } finally {
      setIsLoadingReusablePipelines(false);
    }
  };

  const attachSelectedReusablePipeline = async () => {
    const [pipelineUid, versionUid] = selectedReusableVersion.split("::");
    if (!selectedNode || !pipelineUid || !versionUid) return;
    try {
      setIsLoadingReusablePipelines(true);
      const preview = await previewReusablePipelineAttachment({
        flowId: selectedNode.id,
        pipelineUid,
        versionUid,
      });
      if (preview.compatibility.conflicts.length === 0) {
        const attached = await attachReusablePipelineVersion({
          flowId: selectedNode.id,
          pipelineUid,
          versionUid,
          inputMapping: preview.compatibility.input_mapping,
          outputMapping: preview.compatibility.output_mapping,
        });
        applyReusablePipelineVersion(attached, { persist: false });
        toast.success("Reusable pipeline attached", {
          description: `${attached.reference.pipeline_name} · ${attached.reference.version_name}`,
        });
        return;
      }
      setPendingAttachment(preview);
      setAttachmentInputMapping(preview.compatibility.input_mapping);
      setAttachmentOutputMapping(preview.compatibility.output_mapping);
      setIsAttachmentReviewOpen(true);
    } catch (error) {
      toast.error("Could not attach reusable pipeline version", {
        description: error instanceof Error ? error.message : "Unknown error",
      });
    } finally {
      setIsLoadingReusablePipelines(false);
    }
  };

  const confirmReusablePipelineAttachment = async () => {
    if (!selectedNode || !pendingAttachment) return;
    try {
      setIsAttachingReusablePipeline(true);
      const attached = await attachReusablePipelineVersion({
        flowId: selectedNode.id,
        pipelineUid: pendingAttachment.reference.pipeline_uid,
        versionUid: pendingAttachment.reference.version_uid,
        inputMapping: attachmentInputMapping,
        outputMapping: attachmentOutputMapping,
      });
      applyReusablePipelineVersion(attached, { persist: false });
      setIsAttachmentReviewOpen(false);
      setPendingAttachment(null);
      toast.success("Subpipeline version updated", {
        description: `${attached.reference.pipeline_name} · ${attached.reference.version_name}`,
      });
    } catch (error) {
      toast.error("Could not update Subpipeline version", {
        description: error instanceof Error ? error.message : "Unknown error",
      });
    } finally {
      setIsAttachingReusablePipeline(false);
    }
  };

  // Upload newly added files through the backend. Same filename replaces older entry.
  const handleFileUpload = async (
    e: React.ChangeEvent<HTMLInputElement>,
    role: NodeFileRole,
  ) => {
    if (!canManageFiles) return;
    if (!selectedNode) return;
    const picked = e.target.files ? Array.from(e.target.files) : [];
    if (picked.length === 0) return;
    if (role === "code") {
      const invalid = picked.find((file) => !USER_CODE_FILE_PATTERN.test(file.name));
      if (invalid) {
        toast.error(`Could not upload ${invalid.name}`, {
          description: "Upload main.py and, only when needed, requirements.txt.",
        });
        e.target.value = "";
        return;
      }
      const incorrectlyCasedEntrypoint = picked.find((file) => (
        file.name.toLowerCase() === "main.py" && file.name !== "main.py"
      ));
      if (incorrectlyCasedEntrypoint) {
        toast.error("Rename the entrypoint to main.py", {
          description: "The Python entrypoint filename is case-sensitive.",
        });
        e.target.value = "";
        return;
      }
    }
    const existing = files;
    // Map filename -> index in existing array
    const nameToIndex = new Map<string, number>();
    existing.forEach((f, idx) => {
      const fileName = getNodeFileName(f);
      if (fileName) nameToIndex.set(fileName, idx);
    });
    const uploadedFiles = [...existing];
    let latestGeneratedArtifact: GeneratedArtifact | undefined;
    let changedCount = 0;
    for (const f of picked) {
      try {
        const uploadResult = await uploadNodeFile(selectedNode.id, f, role);
        if (uploadResult?.generated_artifact) {
          latestGeneratedArtifact = uploadResult.generated_artifact as GeneratedArtifact;
        }
        const uploadedRef = uploadedFileReference(selectedNode.id, f.name, role);
        const idx = nameToIndex.get(f.name);
        if (idx != null) {
          uploadedFiles[idx] = uploadedRef;
        } else {
          uploadedFiles.push(uploadedRef);
          nameToIndex.set(f.name, uploadedFiles.length - 1);
        }
        changedCount += 1;
      } catch (err) {
        console.warn(`[PropertiesPanel.tsx] Upload failed for ${f.name} on node ${selectedNode.id}:`, err);
        toast.error(`Could not upload ${f.name}`, {
          description: err instanceof Error ? err.message : "The file was rejected.",
        });
      }
    }
    if (changedCount > 0) {
      setFiles(uploadedFiles);
      pushNodeUpdate({
        files: uploadedFiles,
        ...(latestGeneratedArtifact
          ? { generated_artifact: latestGeneratedArtifact }
          : {}),
      });
    }
    e.target.value = "";
  };

  const removeFile = async (index: number) => {
    if (!selectedNode) return;
    const fileToRemove = files[index];
    if (!fileToRemove) return;
    try {
      const removeResult = await removeNodeFile(selectedNode.id, fileToRemove);
      const updatedFiles = files.filter((_, i) => i !== index);
      setFiles(updatedFiles);
      pushNodeUpdate({
        files: updatedFiles,
        ...(removeResult?.generated_artifact
          ? { generated_artifact: removeResult.generated_artifact as GeneratedArtifact }
          : {}),
      });
    } catch (err) {
      console.warn("[PropertiesPanel.tsx] File removal failed; keeping frontend state unchanged:", err);
    }
  };

  const viewFile = async (file: NodeFileReference, index: number) => {
    if (!selectedNode) return;
    const fileName = getNodeFileName(file);
    setPreviewFileName(fileName);
    setPreviewFileIndex(index);
    setIsEditing(false);
    setCanEditPreview(false);
    setIsPreviewLoading(false);

    if (isBrowserFile(file)) {
      setPreviewFile(file);
      setCanEditPreview(isTextPreviewFile(file));
      if (file.type.startsWith('image/')) {
        setPreviewType('image');
        setPreviewContent(URL.createObjectURL(file));
        setEditedContent('');
      } else if (isTextPreviewFile(file)) {
        setPreviewType('text');
        try {
          const c = await file.text();
          setPreviewContent(c);
          setEditedContent(c);
        } catch {
          setPreviewContent('Error reading file content');
          setEditedContent('');
        }
      } else {
        setPreviewType('binary');
        setPreviewContent(`Preview is not available for ${fileName}.`);
        setEditedContent('');
      }
      return;
    }

    setPreviewFile(null);
    setIsPreviewLoading(true);
    setPreviewContent('');
    try {
      const response = await readNodeFile(selectedNode.id, file);
      if (isImagePreviewName(fileName)) {
        const blob = await response.blob();
        setPreviewType('image');
        setPreviewContent(URL.createObjectURL(blob));
        setEditedContent('');
      } else if (isTextPreviewName(fileName)) {
        const text = await response.text();
        setPreviewType('text');
        setPreviewContent(text);
        setEditedContent(text);
        setCanEditPreview(true);
      } else {
        setPreviewType('binary');
        setPreviewContent(`Preview is not available for ${fileName}.`);
        setEditedContent('');
      }
    } catch (err) {
      console.warn("[PropertiesPanel.tsx] Failed to load file preview:", err);
      setPreviewType('binary');
      setPreviewContent(`Preview is not available for ${fileName}.`);
      setEditedContent('');
    } finally {
      setIsPreviewLoading(false);
    }
  };

  // Save edited text file through the backend
  const saveFileChanges = async () => {
    if (!selectedNode) return;
    if (previewFileIndex === -1 || previewType !== 'text') return;

    const currentFile = files[previewFileIndex];
    if (!currentFile) return;
    const currentFileName = getNodeFileName(currentFile);
    const currentFileRole = getNodeFileRole(currentFile);
    if (!currentFileName) return;
    const fileType = previewFile?.type || "text/plain";
    const newFile = new File([new Blob([editedContent], { type: fileType })], currentFileName, {
      type: fileType,
      lastModified: Date.now(),
    });

    try {
      const updatedFiles = [...files];
      let updatedGeneratedArtifact: GeneratedArtifact | undefined;
      if (isBrowserFile(currentFile)) {
        const uploadResult = await uploadNodeFile(selectedNode.id, newFile, currentFileRole);
        if (uploadResult?.generated_artifact) {
          updatedGeneratedArtifact = uploadResult.generated_artifact as GeneratedArtifact;
        }
        updatedFiles[previewFileIndex] = uploadedFileReference(
          selectedNode.id,
          currentFileName,
          currentFileRole,
        );
        setPreviewFile(newFile);
      } else {
        const updateResult = await updateNodeTextFile(selectedNode.id, currentFile, editedContent);
        if (updateResult?.generated_artifact) {
          updatedGeneratedArtifact = updateResult.generated_artifact as GeneratedArtifact;
        }
        updatedFiles[previewFileIndex] = {
          filename: currentFileName,
          bucket: getNodeFileBucket(currentFile, selectedNode.id),
          role: currentFileRole,
        };
      }

      setFiles(updatedFiles);
      pushNodeUpdate({
        files: updatedFiles,
        ...(updatedGeneratedArtifact
          ? { generated_artifact: updatedGeneratedArtifact }
          : {}),
      });
      setPreviewContent(editedContent);
      setIsEditing(false);
    } catch (err) {
      console.warn("[PropertiesPanel.tsx] File update failed; keeping frontend state unchanged:", err);
    }
  };

  const handleGenerateScript = async () => {
    if (!selectedNode || !canGenerateScript || isGeneratingScript) return;
    if (onGenerateCode) {
      onGenerateCode(selectedNode.id);
      return;
    }
    setIsGeneratingScript(true);
    try {
      const result = await generateNodeScript(selectedNode.id, activeChatbotConfig);
      const generatedArtifact = result?.generated_artifact as GeneratedArtifact | undefined;
      const generatedFiles = Array.isArray(result?.files)
        ? normalizeFileReferences(result.files).map((file) => withNodeFileRole(file, "code"))
        : [];
      const mergedFiles = [...files];
      const indexByName = new Map<string, number>();
      mergedFiles.forEach((file, index) => {
        const fileName = getNodeFileName(file);
        if (fileName) indexByName.set(fileName, index);
      });
      generatedFiles.forEach((file) => {
        const fileName = getNodeFileName(file);
        if (!fileName) return;
        const existingIndex = indexByName.get(fileName);
        if (existingIndex == null) {
          indexByName.set(fileName, mergedFiles.length);
          mergedFiles.push(file);
        } else {
          mergedFiles[existingIndex] = file;
        }
      });
      setFiles(mergedFiles);
      pushNodeUpdate({
        files: mergedFiles,
        ...(generatedArtifact ? { generated_artifact: generatedArtifact } : {}),
      });
      const status = generatedArtifact?.validation_report?.status || "generated";
      toast("Script generated", {
        description: `Runtime bundle ${status}.`,
      });
    } catch (error) {
      toast("Script generation failed", {
        description: error instanceof Error ? error.message : "Unknown error",
      });
    } finally {
      setIsGeneratingScript(false);
    }
  };

  const setParamValue = (key: string, value: string) => {
    if (secretParamKeys.includes(key)) {
      setSecretDrafts((current) => ({ ...current, [key]: value }));
      return;
    }
    const next = { ...param, [key]: value };
    setParam(next);
    pushNodeUpdate({
      param: next,
      secret_params: secretParamKeys,
    });
  };

  const commitSecretValue = async (key: string) => {
    if (!selectedNode?.id) return;
    const value = secretDrafts[key] || "";
    if (!value) return;
    try {
      await storeNodeSecret(selectedNode.id, key, value);
      setConfiguredSecretParams((current) => new Set([...current, key]));
      setSecretDrafts((current) => ({ ...current, [key]: "" }));
      setRevealedSecretParams((current) => {
        const next = new Set(current);
        next.delete(key);
        return next;
      });
      toast.success(`${key} stored securely`);
    } catch (error) {
      toast.error(`Could not store ${key}`, {
        description: error instanceof Error ? error.message : "Unknown error",
      });
    }
  };

  const setParameterSensitive = async (key: string, sensitive: boolean) => {
    const currentValue = String(param[key] ?? "");
    if (sensitive) {
      const nextSecrets = Array.from(new Set([...secretParamKeys, key]));
      const next = { ...param, [key]: "" };
      setParam(next);
      setSecretParamKeys(nextSecrets);
      if (currentValue) setSecretDrafts((current) => ({ ...current, [key]: currentValue }));
      pushNodeUpdate({ param: next, secret_params: nextSecrets });
      if (currentValue && selectedNode?.id) {
        try {
          await storeNodeSecret(selectedNode.id, key, currentValue);
          setConfiguredSecretParams((current) => new Set([...current, key]));
          setSecretDrafts((current) => ({ ...current, [key]: "" }));
        } catch (error) {
          toast.error(`Could not store ${key}`, {
            description: error instanceof Error ? error.message : "Unknown error",
          });
        }
      }
      return;
    }

    const restoredValue = secretDrafts[key] || "";
    const nextSecrets = secretParamKeys.filter((name) => name !== key);
    const next = { ...param, [key]: restoredValue };
    setParam(next);
    setSecretParamKeys(nextSecrets);
    setConfiguredSecretParams((current) => {
      const updated = new Set(current);
      updated.delete(key);
      return updated;
    });
    setSecretDrafts((current) => {
      const updated = { ...current };
      delete updated[key];
      return updated;
    });
    pushNodeUpdate({ param: next, secret_params: nextSecrets });
    if (selectedNode?.id) {
      void deleteNodeSecret(selectedNode.id, key).catch(() => undefined);
    }
  };

  const addParameter = () => {
    const name = newParameterName.trim();
    if (!name) return;
    if (!USER_PARAMETER_NAME_PATTERN.test(name)) {
      toast.error("Use a simple parameter name", {
        description: "Start with a letter or underscore; then use letters, numbers, dots, dashes, or underscores.",
      });
      return;
    }
    if (name in param) {
      toast.error(`Parameter ${name} already exists.`);
      return;
    }
    const next = { ...param, [name]: "" };
    const nextSecrets = isSensitiveParameterName(name)
      ? Array.from(new Set([...secretParamKeys, name]))
      : secretParamKeys;
    setParam(next);
    setSecretParamKeys(nextSecrets);
    setNewParameterName("");
    pushNodeUpdate({ param: next, secret_params: nextSecrets });
  };

  const removeParameter = (key: string) => {
    const wasSecret = secretParamKeys.includes(key);
    const next = { ...param };
    delete next[key];
    const nextSecrets = secretParamKeys.filter((name) => name !== key);
    setParam(next);
    setSecretParamKeys(nextSecrets);
    setRevealedSecretParams((current) => {
      const updated = new Set(current);
      updated.delete(key);
      return updated;
    });
    pushNodeUpdate({ param: next, secret_params: nextSecrets });
    if (wasSecret && selectedNode?.id) {
      void deleteNodeSecret(selectedNode.id, key).catch(() => undefined);
    }
  };

  const toggleSecretVisibility = (key: string) => {
    setRevealedSecretParams((current) => {
      const next = new Set(current);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const renderFileArea = (
    role: NodeFileRole,
    indexedFiles: Array<{ file: NodeFileReference; index: number }>,
    inputRef: React.RefObject<HTMLInputElement>,
  ) => {
    const attachmentLabel = role === "code"
      ? "Python Code"
      : "Input";
    return (
    <div className="rounded-lg border border-dashed border-border p-3">
      <input
        ref={inputRef}
        type="file"
        multiple
        accept={role === "code" ? ".py,.pyi,.txt,.json,.toml,.yaml,.yml,.sql,.sh" : undefined}
        onChange={(event) => { void handleFileUpload(event, role); }}
        className="hidden"
      />
      <Button
        type="button"
        variant="outline"
        size="sm"
        onClick={() => inputRef.current?.click()}
        className="w-full"
      >
        <Upload className="mr-2 h-4 w-4" />
        {role === "code" ? "Upload your Python code" : `Upload ${attachmentLabel} Files`}
      </Button>

      {indexedFiles.length === 0 ? (
        <p className="mt-3 text-center text-xs text-muted-foreground">
          No {attachmentLabel.toLowerCase()} files attached.
        </p>
      ) : (
        <div className="mt-3 space-y-2">
          {indexedFiles.map(({ file, index }) => (
            <div key={`${getNodeFileName(file)}-${index}`} className="flex items-center justify-between rounded bg-muted/50 p-2">
              <span className="min-w-0 flex-1 truncate text-xs">{getNodeFileName(file)}</span>
              <div className="flex items-center gap-1">
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  onClick={() => { void viewFile(file, index); }}
                  aria-label={`Preview ${getNodeFileName(file)}`}
                >
                  <Eye className="h-3 w-3" />
                </Button>
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  onClick={() => { void removeFile(index); }}
                  aria-label={`Remove ${getNodeFileName(file)}`}
                >
                  <X className="h-3 w-3" />
                </Button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
    );
  };

  const validationReport = selectedNode?.data.generated_artifact?.validation_report;
  const runtimeEnvironment = Array.isArray(
    selectedNode?.data.generated_artifact?.runtime_environment,
  )
    ? selectedNode.data.generated_artifact.runtime_environment.filter((item) => (
        item && typeof item === "object" && String(item.name || "").trim()
      ))
    : [];
  const designValidationIssues = selectedNode?.data.validation_issues || [];
  const visibleDesignValidationIssues = designValidationIssues.filter((issue) => (
    issue.category !== "ports" && issue.category !== "implementation"
  ));
  const categoryStatus = (category: ValidationIssue['category']) => {
    const categoryIssues = designValidationIssues.filter((issue) => issue.category === category);
    if (categoryIssues.some((issue) => issue.severity === 'error')) return 'invalid';
    if (categoryIssues.length > 0) return 'warning';
    return 'valid';
  };
  const sectionStatus = (category: ValidationIssue['category']): "error" | "warning" | undefined => {
    const status = categoryStatus(category);
    return status === "invalid" ? "error" : status === "warning" ? "warning" : undefined;
  };
  const implementationMigrationError = taskImplementationMigrationError(selectedNode?.data.implementation);
  const implementationStatus = taskImplementationStatus({
    implementation: selectedNode?.data.implementation,
    artifact: selectedNode?.data.generated_artifact,
    hasPythonPackage: indexedCodeFiles.some(({ file }) => /(^|\/)main\.py$/i.test(getNodeFileName(file))),
    isGenerating: isGeneratingScript,
    hasImplementationErrors: designValidationIssues.some((issue) => (
      issue.category === "implementation" && issue.severity === "error"
    )),
  });
  const implementationStatusLabel = {
    missing: "Not added",
    generating: "Generating",
    current: "Current",
    stale: "Stale",
    invalid: "Invalid",
  }[implementationStatus];
  const packageEntry = indexedCodeFiles.find(({ file }) => /(^|\/)main\.py$/i.test(getNodeFileName(file)))
    || indexedCodeFiles[0];
  const hasValidationMessages = visibleDesignValidationIssues.length > 0
    || Boolean(validationReport?.errors?.length)
    || Boolean(validationReport?.warnings?.length);
  const focusIssueSection = (issue: ValidationIssue) => {
    const section = ["unknown-edge-port", "missing-edge-port"].includes(issue.code)
      ? "validation"
      : issue.category;
    window.setTimeout(() => {
      document.getElementById(`inspector-${section}`)?.scrollIntoView({
        behavior: "smooth",
        block: "start",
      });
    });
  };
  const renderParametersSection = ({
    title = "Runtime parameters",
    description = "Values you configure manually for the attached code. The pipeline assistant never fills this section.",
  }: {
    title?: string;
    description?: string;
  } = {}) => (
    <InspectorSection
      id="inspector-configuration"
      title={title}
      description={description}
    >
      <div className="space-y-3">
      {editableParamEntries.map(([key, value]) => {
        const isSecret = secretParamKeys.includes(key);
        const isRevealed = revealedSecretParams.has(key);
        return (
          <div key={key} className="space-y-1.5 rounded-md border bg-background/50 p-2">
            <div className="flex items-center justify-between gap-2">
              <Label htmlFor={`parameter-${key}`} className="min-w-0 truncate font-mono text-xs">
                {key}
              </Label>
              <Button
                type="button"
                variant="ghost"
                size="icon"
                className="h-7 w-7 shrink-0"
                onClick={() => removeParameter(key)}
                aria-label={`Remove parameter ${key}`}
              >
                <X className="h-3.5 w-3.5" />
              </Button>
            </div>
            <div className="flex items-center justify-between gap-3">
              <label className="flex cursor-pointer items-center gap-2 text-xs text-muted-foreground">
                <Checkbox
                  checked={isSecret}
                  onCheckedChange={(checked) => {
                    void setParameterSensitive(key, checked === true);
                  }}
                  aria-label={`Mark ${key} as sensitive`}
                />
                Sensitive
              </label>
              {isSecret && configuredSecretParams.has(key) && (
                <Badge variant="outline" className="text-[10px]">Stored securely</Badge>
              )}
            </div>
            <div className="relative">
              <Input
                id={`parameter-${key}`}
                type={isSecret && !isRevealed ? "password" : "text"}
                value={isSecret
                  ? (secretDrafts[key] || "")
                  : (typeof value === "string" ? value : JSON.stringify(value ?? ""))}
                onChange={(event) => setParamValue(key, event.target.value)}
                onBlur={() => { if (isSecret) void commitSecretValue(key); }}
                placeholder={isSecret
                  ? (configuredSecretParams.has(key) ? "Stored securely — enter to replace" : "Enter sensitive value")
                  : "Value"}
                className={isSecret ? "pr-9" : undefined}
              />
              {isSecret && (
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  className="absolute right-0 top-0 h-9 w-9"
                  onClick={() => toggleSecretVisibility(key)}
                  aria-label={isRevealed ? `Hide ${key}` : `Show ${key}`}
                >
                  {isRevealed ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                </Button>
              )}
            </div>
          </div>
        );
      })}
        {editableParamEntries.length === 0 && (
          <p className="text-xs text-muted-foreground">No parameters added.</p>
        )}
        <div className="flex gap-2">
          <Input
            aria-label="New parameter name"
            value={newParameterName}
            onChange={(event) => setNewParameterName(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") {
                event.preventDefault();
                addParameter();
              }
            }}
            placeholder="Parameter name"
          />
          <Button
            type="button"
            variant="outline"
            onClick={addParameter}
            disabled={!newParameterName.trim()}
          >
            <Plus className="mr-1.5 h-4 w-4" />
            Add
          </Button>
        </div>
      </div>
    </InspectorSection>
  );

  const renderInputFilesSection = () => (
    <InspectorSection
      title="Input Files"
      description="Upload the files this Source provides to the pipeline. These files are used when the pipeline runs."
    >
      {renderFileArea("data", indexedDataFiles, dataFileInputRef)}
    </InspectorSection>
  );

  return (
    <div className={cn("w-full border-l border-border bg-card text-card-foreground flex flex-col h-full", className)}>
      <div className="p-4 border-b border-border">
        <div className="flex items-center justify-between">
          <div>
            <h2 className="text-lg font-semibold">Properties</h2>
            <p className="text-xs text-muted-foreground mt-1">
              {selectedNode ? `${getStepTypeLabel(nodeType)} details` : "Select a node to edit"}
            </p>
          </div>
        </div>
      </div>

      {selectedNode ? (
        <div className="p-4 flex-1 overflow-y-auto">
          <div className="space-y-4">
            {hasValidationMessages && <InspectorSection
              id="inspector-validation"
              title="Validation"
              description="Review anything that still needs attention before running the pipeline."
              status={visibleDesignValidationIssues.some((issue) => issue.severity === "error")
                ? "error"
                : visibleDesignValidationIssues.length > 0
                  ? "warning"
                  : undefined}
            >
              {visibleDesignValidationIssues.length === 0 ? (
                <p className="text-xs text-[hsl(var(--success-foreground))]">No design-time issues on this component.</p>
              ) : (
                <div className="space-y-1.5">
                  {visibleDesignValidationIssues.map((issue, index) => (
                    <button
                      key={`${issue.code}-${index}`}
                      type="button"
                      onClick={() => focusIssueSection(issue)}
                      className={cn(
                        "w-full rounded border p-2 text-left text-xs transition-colors hover:bg-muted/60",
                        issue.severity === "error"
                          ? "border-destructive/30 text-destructive"
                          : "border-amber-500/30 text-amber-500",
                      )}
                    >
                      <span className="font-medium capitalize">{issue.category}:</span> {issue.message}
                    </button>
                  ))}
                </div>
              )}
              {Array.isArray(validationReport?.errors) && validationReport.errors.length > 0 && (
                <div className="rounded border border-destructive/30 bg-destructive/10 p-2 text-xs text-destructive">
                  {validationReport.errors.join(" ")}
                </div>
              )}
              {Array.isArray(validationReport?.warnings) && validationReport.warnings.length > 0 && (
                <div className="rounded border border-amber-500/30 bg-amber-500/10 p-2 text-xs text-amber-500">
                  {validationReport.warnings.join(" ")}
                </div>
              )}
            </InspectorSection>}

            <InspectorSection
              id="inspector-graph"
              title="Details"
              description="Describe what this component should do."
              status={sectionStatus("graph")}
            >
              <div className="space-y-2">
                <Label htmlFor="node-label" className="text-sm">Name</Label>
                <Input
                  id="node-label"
                  value={label}
                  onChange={handleLabelChange}
                  placeholder="Enter component label"
                />
              </div>

              <div className="space-y-2">
                <Label htmlFor="node-description" className="text-sm">Description</Label>
                <Textarea
                  id="node-description"
                  value={description}
                  onChange={handleDescriptionChange}
                  placeholder={nodeType === "source"
                    ? "Describe the incoming data and any important assumptions"
                    : "Describe this pipeline component"}
                  className="min-h-20 resize-y"
                />
              </div>
            </InspectorSection>

            {canManageInputFiles && renderInputFilesSection()}

            {["source", "destination"].includes(nodeType) && (
              <InspectorSection
                id="inspector-connection"
                title="Connection"
                description="No setup is needed for most pipelines."
                status={sectionStatus("configuration")}
              >
                <details className="rounded-md border border-border bg-muted/10">
                  <summary className="cursor-pointer px-3 py-2 text-xs font-medium">
                    Advanced settings
                  </summary>
                  <div className="space-y-3 border-t border-border p-3">
                    <div className="space-y-2">
                      <Label htmlFor="connector-type">Connector type</Label>
                      <select
                        id="connector-type"
                        className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm"
                        value={currentTemplate}
                        onChange={(event) => handleTemplateChange(event.target.value)}
                      >
                        {connectorTemplateGroups.map((group) => (
                          <optgroup key={group.category} label={group.category}>
                            {group.options.map((option) => (
                              <option key={option.value} value={option.value}>{option.label}</option>
                            ))}
                          </optgroup>
                        ))}
                      </select>
                    </div>
                    {renderParametersSection({
                      title: "Connection settings",
                      description: "Only change these settings if this step needs a specific external connection. Sign-in details are kept private.",
                    })}
                  </div>
                </details>
              </InspectorSection>
            )}

            {nodeType === "flow" && (
              <InspectorSection
                id="inspector-flow"
                title="Flow behavior"
                description="Flow components control scheduling and routing; they do not transform the value themselves."
                status={sectionStatus("configuration")}
              >
                {showTemplateSelector && (
                  <div className="space-y-2">
                    <Label htmlFor="flow-behavior">Behavior</Label>
                    <select
                      id="flow-behavior"
                      className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm"
                      value={currentTemplate}
                      onChange={(event) => handleTemplateChange(event.target.value)}
                    >
                      {templateGroups.map((group) => (
                        <optgroup key={group.category} label={group.category}>
                          {group.options.map((option) => (
                            <option key={option.value} value={option.value}>{option.label}</option>
                          ))}
                        </optgroup>
                      ))}
                    </select>
                  </div>
                )}
                {currentTemplate === "Condition" && (
                  <div className="space-y-3">
                    <div className="flex gap-2 rounded-md border border-purple-500/25 bg-purple-500/10 p-3 text-xs text-muted-foreground">
                      <GitBranch className="mt-0.5 h-4 w-4 shrink-0 text-purple-400" />
                      <p>The incoming value is forwarded through exactly one branch. Connect <strong className="text-foreground">when_true</strong>; connect <strong className="text-foreground">when_false</strong> when the rejected path needs handling.</p>
                    </div>
                    <div className="space-y-2">
                      <Label htmlFor="flow-condition-expression">Expression</Label>
                      <Textarea
                        id="flow-condition-expression"
                        value={String(param.expression ?? "")}
                        onChange={(event) => setFlowParameter("expression", event.target.value)}
                        placeholder={'value.status == "valid"'}
                        className="min-h-20 font-mono text-xs"
                      />
                      <p className="text-[11px] text-muted-foreground">
                        Use <code className="rounded bg-muted px-1 py-0.5">value</code> for the incoming value, for example <code className="rounded bg-muted px-1 py-0.5">value.score &gt;= 0.8</code>.
                      </p>
                    </div>
                  </div>
                )}

                {currentTemplate === "Parallel Map" && (
                  <div className="space-y-3">
                    <div className="rounded-md border border-purple-500/25 bg-purple-500/10 p-3 text-xs text-muted-foreground">
                      The <strong className="text-foreground">items</strong> input must be an array. The downstream branch connected to <strong className="text-foreground">item</strong> is scheduled once for each array item.
                    </div>
                    <div className="grid grid-cols-2 gap-3">
                      <div className="space-y-2">
                        <Label htmlFor="flow-max-concurrency">Maximum concurrency</Label>
                        <Input
                          id="flow-max-concurrency"
                          type="number"
                          min={1}
                          step={1}
                          value={String(param.max_concurrency ?? "")}
                          onChange={(event) => setFlowParameter(
                            "max_concurrency",
                            event.target.value === "" ? "" : Number(event.target.value),
                          )}
                        />
                      </div>
                      <div className="space-y-2">
                        <Label htmlFor="flow-failure-policy">If one item fails</Label>
                        <select
                          id="flow-failure-policy"
                          className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm"
                          value={String(param.failure_policy || "stop")}
                          onChange={(event) => setFlowParameter("failure_policy", event.target.value)}
                        >
                          <option value="stop">Stop remaining items</option>
                          <option value="continue">Continue other items</option>
                        </select>
                      </div>
                    </div>
                  </div>
                )}

                {currentTemplate === "Flow" && (
                  <div className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-xs text-destructive">
                    This compatibility template has no behavior. Select Condition or Parallel Map above.
                  </div>
                )}
              </InspectorSection>
            )}

            {canManageImplementation && renderParametersSection()}

            {canManageImplementation && runtimeEnvironment.length > 0 && (
              <InspectorSection
                id="inspector-runtime-environment"
                title="Environment variables detected"
                description="Read-only requirements discovered from the attached Python script. This does not create parameters or store values."
                status={runtimeEnvironment.some((item) => item.required) ? "warning" : undefined}
              >
                <div className="space-y-2">
                  {runtimeEnvironment.map((item) => (
                    <div
                      key={String(item.name)}
                      className="flex items-start justify-between gap-3 rounded-md border border-amber-500/25 bg-amber-500/5 p-2"
                    >
                      <div className="min-w-0">
                        <p className="truncate font-mono text-xs font-medium">{item.name}</p>
                        <p className="mt-1 text-[11px] text-muted-foreground">
                          {item.description || "Referenced by main.py at runtime."}
                        </p>
                      </div>
                      <div className="flex shrink-0 gap-1">
                        <Badge variant="outline" className="text-[10px]">
                          {item.required ? "Required" : "Optional"}
                        </Badge>
                        {item.secret && (
                          <Badge variant="outline" className="text-[10px]">Sensitive</Badge>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              </InspectorSection>
            )}

            {canManageImplementation && (
              <InspectorSection
                id="inspector-implementation"
                title="Implementation"
                description="Generate the code with AI or upload main.py with an optional requirements.txt file."
                status={implementationStatus === "invalid" ? "error" : sectionStatus("implementation")}
              >
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <Badge variant="outline">{implementationStatusLabel}</Badge>
                  <div className="flex items-center gap-2">
                    {packageEntry && (
                      <Button
                        type="button"
                        variant="outline"
                        size="sm"
                        onClick={() => { void viewFile(packageEntry.file, packageEntry.index); }}
                      >
                        View code
                      </Button>
                    )}
                    <Button
                      type="button"
                      size="sm"
                      onClick={() => { void handleGenerateScript(); }}
                      disabled={isGeneratingScript}
                    >
                      {isGeneratingScript
                        ? <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                        : <Wand2 className="mr-2 h-4 w-4" />}
                      Generate with AI
                    </Button>
                  </div>
                </div>
                {implementationMigrationError && (
                  <p role="alert" className="rounded-md border border-destructive/40 bg-destructive/10 p-2 text-xs text-destructive">
                    {implementationMigrationError} The original metadata has been preserved.
                  </p>
                )}
                {renderFileArea("code", indexedCodeFiles, codeFileInputRef)}
                <p className="text-[11px] text-muted-foreground">
                  The only required file is <code className="rounded bg-muted px-1">main.py</code>.
                  Add <code className="rounded bg-muted px-1">requirements.txt</code> only when third-party packages are needed.
                </p>
                <div className="rounded-md border border-border bg-muted/30 p-3 text-xs text-muted-foreground">
                  <p className="font-medium text-foreground">Task runtime contract</p>
                  <p className="mt-1">Your code runs with a flat standard workspace. Upstream files are placed directly in <code className="rounded bg-background px-1">PIPELINE_INPUT_DIR</code>; write every result directly to <code className="rounded bg-background px-1">PIPELINE_OUTPUT_DIR</code>. Port names never create implicit subdirectories.</p>
                </div>
              </InspectorSection>
            )}

            {nodeType === "subpipeline" && (
              <>
                <InspectorSection
                  title="Referenced Pipeline"
                  description="This component invokes a separately saved, versioned pipeline."
                >
                  <div className="rounded-md border border-cyan-400/20 bg-cyan-500/5 p-3 text-xs">
                    <div className="font-medium text-cyan-600 dark:text-cyan-300">
                      {selectedNode.data.subpipeline?.reference?.pipeline_name || 'No reusable pipeline attached'}
                    </div>
                    <div className="mt-1 text-muted-foreground">
                      {selectedNode.data.subpipeline?.reference?.version_name
                        ? `${selectedNode.data.subpipeline.reference.version_name} · `
                        : ''}
                      {ports.inputs.length} input{ports.inputs.length === 1 ? '' : 's'} · {ports.outputs.length} output{ports.outputs.length === 1 ? '' : 's'}
                    </div>
                    {selectedNode.data.subpipeline?.resolution_error && (
                      <div className="mt-2 text-red-500">{selectedNode.data.subpipeline.resolution_error}</div>
                    )}
                  </div>
                  <div className="space-y-2">
                    <Label htmlFor="reusable-pipeline-version">Saved pipeline version</Label>
                    <select
                      id="reusable-pipeline-version"
                      className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm"
                      value={selectedReusableVersion}
                      disabled={isLoadingReusablePipelines}
                      onChange={(event) => setSelectedReusableVersion(event.target.value)}
                    >
                      <option value="">Select a reusable pipeline…</option>
                      {reusablePipelines.flatMap((pipeline) => pipeline.versions.map((version) => (
                        <option key={`${pipeline.uid}::${version.uid}`} value={`${pipeline.uid}::${version.uid}`}>
                          {pipeline.name} · {version.name}
                        </option>
                      )))}
                    </select>
                    <Button
                      type="button"
                      size="sm"
                      disabled={!selectedReusableVersion || isLoadingReusablePipelines}
                      onClick={() => { void attachSelectedReusablePipeline(); }}
                    >
                      {isLoadingReusablePipelines ? "Attaching…" : "Use selected version"}
                    </Button>
                  </div>
                  <div className="rounded-md border border-dashed p-3 text-xs text-muted-foreground">
                    Create and update reusable pipelines from <strong className="text-foreground">Library → Reusable pipelines → Manage</strong> using the normal main canvas.
                  </div>
                  {selectedNode.data.subpipeline?.graph && !selectedNode.data.subpipeline?.reference && (
                    <p role="alert" className="rounded-md border border-amber-500/30 bg-amber-500/10 p-3 text-xs text-amber-700 dark:text-amber-300">
                      This component contains a legacy embedded pipeline. Rebuild it on the main canvas, save it for reuse, then select the saved version here. The embedded metadata remains preserved.
                    </p>
                  )}
                  <div className="flex justify-end">
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      onClick={() => { void handleRefreshReusablePipelineCatalog(); }}
                    >
                      Refresh library
                    </Button>
                  </div>
                </InspectorSection>
              </>
            )}

          </div>
        </div>
      ) : (
        <div className="flex-1 flex items-center justify-center p-6 text-center">
          <div>
            <p className="text-muted-foreground">No node selected</p>
            <p className="text-xs text-muted-foreground mt-2">
              Click on a node in the canvas to edit its properties
            </p>
          </div>
        </div>
      )}

      <AlertDialog open={isAttachmentReviewOpen} onOpenChange={setIsAttachmentReviewOpen}>
        <AlertDialogContent className="max-w-2xl">
          <AlertDialogHeader>
            <AlertDialogTitle>Review Subpipeline version change</AlertDialogTitle>
            <AlertDialogDescription>
              The reusable pipeline version and every affected connection will be updated together.
            </AlertDialogDescription>
          </AlertDialogHeader>
          {pendingAttachment && (
            <div className="max-h-[55vh] space-y-4 overflow-y-auto text-sm">
              <div className="rounded-md border p-3">
                <div className="font-medium">
                  {pendingAttachment.reference.pipeline_name} · {pendingAttachment.reference.version_name}
                </div>
                <div className="mt-1 text-xs text-muted-foreground">
                  Available connections: {pendingAttachment.interface.inputs.length} input{pendingAttachment.interface.inputs.length === 1 ? "" : "s"}
                  {" · "}{pendingAttachment.interface.outputs.length} output{pendingAttachment.interface.outputs.length === 1 ? "" : "s"}
                </div>
              </div>
              {pendingAttachment.compatibility.conflicts.length === 0 ? (
                <div className="rounded-md border border-emerald-500/30 bg-emerald-500/5 p-3 text-emerald-700 dark:text-emerald-300">
                  Connected ports have an unambiguous compatible mapping. No connections will be lost.
                </div>
              ) : (
                <div className="space-y-3">
                  <p className="text-xs text-amber-600 dark:text-amber-300">
                    Choose where each existing connection should move. Only type-compatible ports are available.
                  </p>
                  {pendingAttachment.compatibility.conflicts.map((conflict) => {
                    const mapping = conflict.direction === "inputs" ? attachmentInputMapping : attachmentOutputMapping;
                    const setMapping = conflict.direction === "inputs" ? setAttachmentInputMapping : setAttachmentOutputMapping;
                    return (
                      <div key={`${conflict.direction}-${conflict.port}`} className="space-y-1.5 rounded-md border p-3">
                        <Label htmlFor={`mapping-${conflict.direction}-${conflict.port}`}>
                          Existing {conflict.direction === "inputs" ? "input" : "output"} “{conflict.port}”
                        </Label>
                        <select
                          id={`mapping-${conflict.direction}-${conflict.port}`}
                          value={mapping[conflict.port] || ""}
                          onChange={(event) => setMapping((current) => ({ ...current, [conflict.port]: event.target.value }))}
                          className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm"
                        >
                          <option value="">Select a compatible target…</option>
                          {conflict.candidates.map((candidate) => {
                            const id = typeof candidate === "string" ? candidate : candidate.id;
                            const text = typeof candidate === "string"
                              ? candidate
                              : `${candidate.name || candidate.id} · ${candidate.type}`;
                            return <option key={id} value={id}>{text}</option>;
                          })}
                        </select>
                        <p className="text-xs text-muted-foreground">{conflict.reason}</p>
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          )}
          <AlertDialogFooter>
            <AlertDialogCancel disabled={isAttachingReusablePipeline}>Cancel</AlertDialogCancel>
            <AlertDialogAction
              disabled={
                isAttachingReusablePipeline
                || !pendingAttachment
                || pendingAttachment.compatibility.conflicts.some((conflict) => {
                  const mapping = conflict.direction === "inputs" ? attachmentInputMapping : attachmentOutputMapping;
                  return !mapping[conflict.port];
                })
              }
              onClick={(event) => {
                event.preventDefault();
                void confirmReusablePipelineAttachment();
              }}
            >
              {isAttachingReusablePipeline ? "Updating version…" : "Use this version"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <FilePreviewDialog
        open={Boolean(previewFileName)}
        fileName={previewFileName}
        previewContent={previewContent}
        previewType={previewType}
        isLoading={isPreviewLoading}
        canEdit={canEditPreview || previewType === 'text'}
        isEditing={isEditing}
        editedContent={editedContent}
        onClose={() => {
          setPreviewFile(null);
          setPreviewFileName('');
          setCanEditPreview(false);
          setIsPreviewLoading(false);
        }}
        onStartEditing={() => setIsEditing(true)}
        onCancelEditing={() => {
          setIsEditing(false);
          setEditedContent(previewContent);
        }}
        onEditedContentChange={setEditedContent}
        onSaveChanges={saveFileChanges}
      />
    </div>
  );
}
