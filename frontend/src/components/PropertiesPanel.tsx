import React, { useState, useEffect, useRef, useCallback } from 'react';
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import {
  Eye,
  EyeOff,
  Loader2,
  LockKeyhole,
  PlusCircle,
  Unlock,
  Upload,
  Wand2,
  X,
} from 'lucide-react';
import { toast } from "sonner";
import { FilePreviewDialog, PreviewType } from '@/components/properties/FilePreviewDialog';
import { getTypeColor, getTypeIcon } from '@/components/properties/nodeAppearance';
import { ChatbotConfig } from '@/services/chatbotService';
import {
  normalizeType,
  getStepTypeLabel,
  pickBackendUpdatableProps,
  StepType,
  IMPLEMENTATION_KIND_OPTIONS,
  normalizeImplementationKind,
  normalizeNodePorts,
  normalizeSecretParamKeys,
  isSensitiveParameterName,
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
  NodePort,
  NodePorts,
} from '@/features/nodes/nodeSchema';
import {
  defaultTemplateForType,
  findTemplateForType,
  templateOptionsForType,
} from '@/features/nodes/templateCatalog';
import {
  readNodeFile,
  removeNodeFile,
  generateNodeScript,
  updateNodeTextFile,
  updateNodePropertiesInBackend,
  uploadNodeFile,
} from '@/features/nodes/nodePersistence';
import { Node } from 'reactflow';
import type { ValidationIssue } from '@/features/flow/flowValidation';

type NodeParamMap = Record<string, unknown>;
type DraftKeyMap = Record<string, string>;

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
  source_config?: Record<string, unknown>;
  subpipeline?: {
    expanded?: boolean;
    graph?: { nodes: unknown[]; edges: unknown[] };
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
  onNodeUpdate: (id: string, data: PropertyNodeData) => void;
  onRemoveNode?: (nodeId: string) => void;
  activeChatbotConfig?: ChatbotConfig | null;
  isAdvancedMode?: boolean;
  className?: string;
}

const PORT_TYPE_OPTIONS = [
  "any",
  "Text",
  "Number",
  "Boolean",
  "Object",
  "Object[]",
  "Dataset",
  "File",
  "File[]",
  "Image",
  "Image[]",
  "Audio",
  "Message",
  "Message[]",
  "Document",
  "Document[]",
  "Vector",
  "Vector[]",
  "FeatureSet",
  "Model",
  "Prediction[]",
] as const;

export function PropertiesPanel({
  selectedNode,
  onNodeUpdate,
  onRemoveNode,
  activeChatbotConfig,
  isAdvancedMode = false,
  className,
}: PropertiesPanelProps) {
  const nodeType: StepType = normalizeType(selectedNode?.data?.type ?? selectedNode?.type);
  const canManageFiles = typeHasFiles(nodeType);
  const canGenerateScript = nodeType === "task";

  const [label, setLabel] = useState('');
  const [description, setDescription] = useState('');

  // Source/destination boundary content.
  const [content, setContent] = useState('');

  // Every runtime node can carry its script, requirements, and input files.
  const [files, setFiles] = useState<NodeFileReference[]>([]);
  const codeFileInputRef = useRef<HTMLInputElement>(null);
  const dataFileInputRef = useRef<HTMLInputElement>(null);

  // Parameters belong to the node inspector, never to separate graph nodes.
  const [param, setParam] = useState<NodeParamMap>({});
  const [secretParamKeys, setSecretParamKeys] = useState<string[]>([]);
  const [revealedSecretParams, setRevealedSecretParams] = useState<Set<string>>(
    () => new Set(),
  );
  const [draftKeys, setDraftKeys] = useState<DraftKeyMap>({});
  const editableParamEntries = Object.entries(param).filter(([key]) => key !== "model_plan");
  const [ports, setPorts] = useState<NodePorts>(() => normalizeNodePorts(undefined, nodeType));
  const [portContractUnlocked, setPortContractUnlocked] = useState(false);
  const [customPortTypeKeys, setCustomPortTypeKeys] = useState<Set<string>>(() => new Set());
  const [templateSearch, setTemplateSearch] = useState("");
  const [implementationKind, setImplementationKind] = useState(() =>
    normalizeImplementationKind(selectedNode?.data?.implementation?.kind),
  );
  const [implementationLanguage, setImplementationLanguage] = useState('python');
  const [implementationDependencies, setImplementationDependencies] = useState('');
  const [implementationEntrypoint, setImplementationEntrypoint] = useState('');
  const [implementationReference, setImplementationReference] = useState('');
  const [sourceConfigDraft, setSourceConfigDraft] = useState('{}');
  const [subpipelineGraphDraft, setSubpipelineGraphDraft] = useState('{\n  "nodes": [],\n  "edges": []\n}');
  const currentTemplate = String(
    selectedNode?.data?.template?.name
      || selectedNode?.data?.template_label
      || defaultTemplateForType(nodeType),
  );
  const templateOptions = templateOptionsForType(nodeType, currentTemplate);
  const filteredTemplateOptions = templateOptions.filter((option) => {
    const query = templateSearch.trim().toLowerCase();
    return !query
      || option.value === currentTemplate
      || option.label.toLowerCase().includes(query)
      || option.category.toLowerCase().includes(query);
  });
  const templateGroups = Array.from(new Set(filteredTemplateOptions.map((option) => option.category)))
    .map((category) => ({
      category,
      options: filteredTemplateOptions.filter((option) => option.category === category),
    }));
  const indexedCodeFiles = files
    .map((file, index) => ({ file, index }))
    .filter(({ file }) => getNodeFileRole(file) === "code");
  const indexedDataFiles = files
    .map((file, index) => ({ file, index }))
    .filter(({ file }) => getNodeFileRole(file) === "data");
  const activeNodeIdRef = useRef<string | null>(selectedNode?.id ?? null);

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
    next.ports = normalizeNodePorts(next.ports, nodeType);

    // 1) update local reactflow node
    onNodeUpdate(selectedNode.id, next);

    // 2) update backend state (only allowed props)
    const backendProps = pickBackendUpdatableProps(selectedNode.id, next, nodeType);
    debouncedUpdatePropertyToBackend(selectedNode.id, backendProps);
  };

  useEffect(() => {
    const nextNodeId = selectedNode?.id ?? null;
    if (activeNodeIdRef.current !== nextNodeId) {
      setRevealedSecretParams(new Set());
      setPortContractUnlocked(false);
      setCustomPortTypeKeys(new Set());
      setTemplateSearch("");
      activeNodeIdRef.current = nextNodeId;
    }
    if (selectedNode) {
      setLabel(selectedNode.data.label || '');
      setDescription(selectedNode.data.description || '');

      // content only for source/destination boundaries
      setContent(typeHasContent(nodeType) ? (selectedNode.data.content || '') : '');

      // Runtime artifacts can be attached to any structural component.
      setFiles(typeHasFiles(nodeType) ? normalizeFileReferences(selectedNode.data.files) : []);

      const p = selectedNode.data.param;
      const nextParam = (p && typeof p === "object" && !Array.isArray(p))
        ? p as NodeParamMap
        : {};
      setParam(nextParam);
      setSecretParamKeys(normalizeSecretParamKeys(selectedNode.data.secret_params, nextParam));
      setPorts(normalizeNodePorts(selectedNode.data.ports, nodeType));
      setImplementationKind(normalizeImplementationKind(selectedNode.data.implementation?.kind));
      setImplementationLanguage(String(selectedNode.data.implementation?.language || 'python'));
      setImplementationDependencies(
        Array.isArray(selectedNode.data.implementation?.dependencies)
          ? selectedNode.data.implementation.dependencies.map(String).join(', ')
          : '',
      );
      setImplementationEntrypoint(String(selectedNode.data.implementation?.entrypoint || ''));
      setImplementationReference(String(
        selectedNode.data.implementation?.image
          || selectedNode.data.implementation?.repository
          || selectedNode.data.implementation?.endpoint
          || '',
      ));
      setSourceConfigDraft(JSON.stringify(selectedNode.data.source_config || {}, null, 2));
      setSubpipelineGraphDraft(JSON.stringify(
        selectedNode.data.subpipeline?.graph || { nodes: [], edges: [] },
        null,
        2,
      ));

    } else {
      setLabel('');
      setDescription('');
      setContent('');
      setFiles([]);
      setParam({});
      setSecretParamKeys([]);
      setPorts(normalizeNodePorts(undefined, nodeType));
      setImplementationKind("python");
      setImplementationLanguage("python");
      setImplementationDependencies("");
      setImplementationEntrypoint("");
      setImplementationReference("");
      setSourceConfigDraft("{}");
      setSubpipelineGraphDraft('{\n  "nodes": [],\n  "edges": []\n}');
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

  const handleLabelChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    setLabel(e.target.value);
    pushNodeUpdate({ label: e.target.value });
  };

  const handleDescriptionChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    setDescription(e.target.value);
    pushNodeUpdate({ description: e.target.value });
  };

  const handleContentChange = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    setContent(e.target.value);
    pushNodeUpdate({ content: e.target.value });
  };

  const handleImplementationKindChange = (kind: string) => {
    const normalized = normalizeImplementationKind(kind);
    setImplementationKind(normalized);
    pushNodeUpdate({
      implementation: {
        ...(selectedNode?.data.implementation || {}),
        kind: normalized,
      },
    });
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
        description: "Disconnect or remap connected ports before choosing a template with a different contract.",
      });
      return;
    }
    setPorts(nextPorts);
    setPortContractUnlocked(false);
    setCustomPortTypeKeys(new Set());
    pushNodeUpdate({
      template_label: templateLabel,
      template: {
        id: template?.id || `custom.${templateLabel.toLowerCase().replace(/[^a-z0-9]+/g, '-')}`,
        name: templateLabel,
      },
      ports: nextPorts,
    });
  };

  const updateImplementation = (patch: Record<string, unknown>) => {
    pushNodeUpdate({
      implementation: {
        ...(selectedNode?.data.implementation || {}),
        ...patch,
      },
    });
  };

  const persistSourceConfiguration = () => {
    try {
      const parsed = JSON.parse(sourceConfigDraft);
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
        throw new Error('Source configuration must be a JSON object.');
      }
      pushNodeUpdate({ source_config: parsed as Record<string, unknown> });
    } catch (error) {
      toast.error('Invalid source configuration', {
        description: error instanceof Error ? error.message : 'Enter a JSON object.',
      });
    }
  };

  const persistSubpipelineGraph = () => {
    try {
      const parsed = JSON.parse(subpipelineGraphDraft) as { nodes?: unknown; edges?: unknown };
      if (!Array.isArray(parsed?.nodes) || !Array.isArray(parsed?.edges)) {
        throw new Error('Nested graph JSON must contain nodes and edges arrays.');
      }
      pushNodeUpdate({
        subpipeline: {
          ...(selectedNode?.data.subpipeline || {}),
          graph: { nodes: parsed.nodes, edges: parsed.edges },
        },
      });
    } catch (error) {
      toast.error('Invalid nested graph', {
        description: error instanceof Error ? error.message : 'Enter a nested graph object.',
      });
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
    const existing = files;
    // Map filename -> index in existing array
    const nameToIndex = new Map<string, number>();
    existing.forEach((f, idx) => {
      const fileName = getNodeFileName(f);
      if (fileName) nameToIndex.set(fileName, idx);
    });
    const uploadedFiles = [...existing];
    let changedCount = 0;
    for (const f of picked) {
      try {
        await uploadNodeFile(selectedNode.id, f, role);
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
      pushNodeUpdate({ files: uploadedFiles });
    }
    e.target.value = "";
  };

  const removeFile = async (index: number) => {
    if (!selectedNode) return;
    const fileToRemove = files[index];
    if (!fileToRemove) return;
    try {
      await removeNodeFile(selectedNode.id, fileToRemove);
      const updatedFiles = files.filter((_, i) => i !== index);
      setFiles(updatedFiles);
      pushNodeUpdate({ files: updatedFiles });
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
      if (isBrowserFile(currentFile)) {
        await uploadNodeFile(selectedNode.id, newFile, currentFileRole);
        updatedFiles[previewFileIndex] = uploadedFileReference(
          selectedNode.id,
          currentFileName,
          currentFileRole,
        );
        setPreviewFile(newFile);
      } else {
        await updateNodeTextFile(selectedNode.id, currentFile, editedContent);
        updatedFiles[previewFileIndex] = {
          filename: currentFileName,
          bucket: getNodeFileBucket(currentFile, selectedNode.id),
          role: currentFileRole,
        };
      }

      setFiles(updatedFiles);
      pushNodeUpdate({ files: updatedFiles });
      setPreviewContent(editedContent);
      setIsEditing(false);
    } catch (err) {
      console.warn("[PropertiesPanel.tsx] File update failed; keeping frontend state unchanged:", err);
    }
  };

  const handleGenerateScript = async () => {
    if (!selectedNode || !canGenerateScript || isGeneratingScript) return;
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

  // Static node parameter helpers.
  const addParamRow = () => {
    let i = 1;
    let key = `key_${i}`;
    while (param[key] != null) {
      i += 1;
      key = `key_${i}`;
    }
    const next = { ...param, [key]: "" };
    setParam(next);
    pushNodeUpdate({ param: next, secret_params: secretParamKeys });
  };

  const renameParamKey = (oldKey: string, newKeyRaw: string) => {
    const newKey = newKeyRaw.trim();
    if (!newKey || newKey === oldKey) return;
    const next: NodeParamMap = {};
    Object.entries(param).forEach(([k, v]) => {
      if (k === oldKey) next[newKey] = v ?? "";
      else next[k] = v ?? "";
    });
    setParam(next);
    const renamedSecrets = secretParamKeys
      .map((key) => key === oldKey ? newKey : key)
      .filter((key) => key in next);
    if (isSensitiveParameterName(newKey) && !renamedSecrets.includes(newKey)) {
      renamedSecrets.push(newKey);
    }
    setSecretParamKeys(renamedSecrets);
    setRevealedSecretParams((current) => {
      const nextRevealed = new Set(current);
      if (nextRevealed.delete(oldKey)) nextRevealed.add(newKey);
      return nextRevealed;
    });
    pushNodeUpdate({ param: next, secret_params: renamedSecrets });
    setDraftKeys((prev) => {
      const copy = { ...prev };
      delete copy[oldKey];
      return copy;
    });
  };

  const setParamValue = (key: string, value: string) => {
    const next = { ...param, [key]: value };
    setParam(next);
    pushNodeUpdate({ param: next, secret_params: secretParamKeys });
  };

  const removeParamKey = (key: string) => {
    const next = { ...param };
    delete next[key];
    setParam(next);
    const nextSecrets = secretParamKeys.filter((secretKey) => secretKey !== key);
    setSecretParamKeys(nextSecrets);
    setRevealedSecretParams((current) => {
      const nextRevealed = new Set(current);
      nextRevealed.delete(key);
      return nextRevealed;
    });
    pushNodeUpdate({ param: next, secret_params: nextSecrets });
    setDraftKeys((prev) => {
      const copy = { ...prev };
      delete copy[key];
      return copy;
    });
  };

  const toggleSecretParam = (key: string) => {
    const isSecret = secretParamKeys.includes(key);
    const nextSecrets = isSecret
      ? secretParamKeys.filter((secretKey) => secretKey !== key)
      : [...secretParamKeys, key];
    setSecretParamKeys(nextSecrets);
    if (isSecret) {
      setRevealedSecretParams((current) => {
        const nextRevealed = new Set(current);
        nextRevealed.delete(key);
        return nextRevealed;
      });
    }
    pushNodeUpdate({ secret_params: nextSecrets });
  };

  const toggleSecretVisibility = (key: string) => {
    setRevealedSecretParams((current) => {
      const next = new Set(current);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const pushPorts = (next: NodePorts) => {
    const normalized = normalizeNodePorts(next, nodeType);
    setPorts(normalized);
    pushNodeUpdate({ ports: normalized });
  };

  const addPort = (direction: keyof NodePorts) => {
    const existing = ports[direction];
    const stem = direction === "inputs" ? "input" : "output";
    let index = existing.length + 1;
    let id = `${stem}-${index}`;
    const ids = new Set(existing.map((port) => port.id));
    while (ids.has(id)) {
      index += 1;
      id = `${stem}-${index}`;
    }
    pushPorts({
      ...ports,
      [direction]: [...existing, {
        id,
        name: id,
        type: "any",
        required: true,
        description: "",
      }],
    });
  };

  const updatePort = (
    direction: keyof NodePorts,
    portId: string,
    patch: Partial<NodePort>,
  ) => {
    pushPorts({
      ...ports,
      [direction]: ports[direction].map((port) =>
        port.id === portId ? { ...port, ...patch } : port
      ),
    });
  };

  const removePort = (direction: keyof NodePorts, portId: string) => {
    pushPorts({
      ...ports,
      [direction]: ports[direction].filter((port) => port.id !== portId),
    });
  };

  const renderFileArea = (
    role: NodeFileRole,
    indexedFiles: Array<{ file: NodeFileReference; index: number }>,
    inputRef: React.RefObject<HTMLInputElement>,
  ) => (
    <div className="rounded-lg border border-dashed border-border p-3">
      <input
        ref={inputRef}
        type="file"
        multiple
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
        Upload {role === "code" ? "Runtime Package" : "Test Fixture"} Files
      </Button>

      {indexedFiles.length === 0 ? (
        <p className="mt-3 text-center text-xs text-muted-foreground">
          No {role === "code" ? "runtime package" : "test fixture"} files attached.
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

  const validationReport = selectedNode?.data.generated_artifact?.validation_report;
  const configurationStatus = selectedNode?.data.configuration_status || "unconfigured";
  const designValidationIssues = selectedNode?.data.validation_issues || [];
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
  const connectedPorts = selectedNode?.data.connected_ports || {};
  const isPortConnected = (direction: keyof NodePorts, portId: string) =>
    (connectedPorts[direction] || []).includes(portId);
  const canCustomizePorts = isAdvancedMode && portContractUnlocked;
  const focusIssueSection = (issue: ValidationIssue) => {
    const section = ["unknown-edge-port", "missing-edge-port"].includes(issue.code)
      ? "ports"
      : issue.category;
    document.getElementById(`inspector-${section}`)?.scrollIntoView({
      behavior: "smooth",
      block: "start",
    });
  };
  const renderPortDirection = (direction: keyof NodePorts) => {
    const directionLabel = direction === "inputs" ? "Inputs" : "Outputs";
    const structurallyBlocked = direction === "inputs"
      ? nodeType === "source"
      : nodeType === "destination";
    return (
      <div className="space-y-2">
        <div className="flex items-center justify-between">
          <Label className="text-xs uppercase tracking-wide text-muted-foreground">{directionLabel}</Label>
          {!structurallyBlocked && canCustomizePorts && (
            <Button type="button" variant="outline" size="sm" onClick={() => addPort(direction)}>
              <PlusCircle className="mr-1 h-3.5 w-3.5" /> Add
            </Button>
          )}
        </div>
        {ports[direction].length === 0 && (
          <p className="text-xs text-muted-foreground">
            {structurallyBlocked
              ? `${nodeType === "source" ? "Sources" : "Destinations"} cannot define ${direction}.`
              : `No ${direction} defined.`}
          </p>
        )}
        {ports[direction].map((port) => {
          const portKey = `${direction}:${port.id}`;
          const connected = isPortConnected(direction, port.id);
          const customType = customPortTypeKeys.has(portKey)
            || !PORT_TYPE_OPTIONS.includes(port.type as typeof PORT_TYPE_OPTIONS[number]);
          return (
            <div key={port.id} className="space-y-2 rounded-md border border-border/70 p-2">
              <div className="flex items-center justify-between gap-2">
                <span className="text-xs font-medium">{port.name}</span>
                {connected && <Badge variant="outline" className="text-[10px]">Connected</Badge>}
              </div>
              <div className="grid grid-cols-[1fr_1fr_auto] gap-2">
                <Input
                  value={port.name}
                  disabled={!canCustomizePorts || connected}
                  onChange={(event) => updatePort(direction, port.id, { name: event.target.value })}
                  placeholder="name"
                  title={connected ? "Disconnect or remap this port before renaming it." : undefined}
                />
                <select
                  className="h-9 min-w-0 rounded-md border border-input bg-background px-2 text-sm disabled:cursor-not-allowed disabled:opacity-50"
                  value={customType ? "__custom__" : port.type}
                  disabled={!canCustomizePorts || connected}
                  onChange={(event) => {
                    if (event.target.value === "__custom__") {
                      setCustomPortTypeKeys((current) => new Set(current).add(portKey));
                      if (!customType) updatePort(direction, port.id, { type: "CustomType" });
                      return;
                    }
                    setCustomPortTypeKeys((current) => {
                      const next = new Set(current);
                      next.delete(portKey);
                      return next;
                    });
                    updatePort(direction, port.id, { type: event.target.value });
                  }}
                  title={connected ? "Disconnect or remap this port before changing its type." : undefined}
                >
                  {PORT_TYPE_OPTIONS.map((type) => <option key={type} value={type}>{type}</option>)}
                  <option value="__custom__">Custom type…</option>
                </select>
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  disabled={!canCustomizePorts || connected}
                  onClick={() => removePort(direction, port.id)}
                  aria-label={`Remove ${direction === "inputs" ? "input" : "output"} ${port.name}`}
                  title={connected ? "Disconnect or remap this port before deleting it." : undefined}
                >
                  <X className="h-4 w-4" />
                </Button>
              </div>
              {customType && (
                <Input
                  value={port.type}
                  disabled={!canCustomizePorts || connected}
                  onChange={(event) => updatePort(direction, port.id, { type: event.target.value })}
                  placeholder="Custom contract type"
                />
              )}
              <div className="grid grid-cols-[auto_1fr] items-center gap-2 pl-1">
                <label className="flex items-center gap-2 text-xs text-muted-foreground">
                  <input
                    type="checkbox"
                    checked={port.required}
                    disabled={!canCustomizePorts}
                    onChange={(event) => updatePort(direction, port.id, { required: event.target.checked })}
                  />
                  Required
                </label>
                <Input
                  value={port.description}
                  disabled={!canCustomizePorts}
                  onChange={(event) => updatePort(direction, port.id, { description: event.target.value })}
                  placeholder="Contract description"
                />
              </div>
            </div>
          );
        })}
      </div>
    );
  };

  return (
    <div className={cn("w-full border-l border-border bg-card text-card-foreground flex flex-col h-full", className)}>
      <div className="p-4 border-b border-border">
        <div className="flex items-center justify-between">
          <div>
            <h2 className="text-lg font-semibold">Properties</h2>
            <p className="text-xs text-muted-foreground mt-1">
              Configure the selected node
            </p>
          </div>
        </div>
      </div>

      {selectedNode ? (
        <div className="p-4 flex-1 overflow-y-auto">
          <div className="space-y-4">
            <InspectorSection
              id="inspector-validation"
              title="Validation"
              description="Select an issue to jump to the field or contract that needs attention."
              status={designValidationIssues.some((issue) => issue.severity === "error")
                ? "error"
                : designValidationIssues.length > 0
                  ? "warning"
                  : undefined}
            >
              {designValidationIssues.length === 0 ? (
                <p className="text-xs text-emerald-500">No design-time issues on this component.</p>
              ) : (
                <div className="space-y-1.5">
                  {designValidationIssues.map((issue, index) => (
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
            </InspectorSection>

            <InspectorSection
              id="inspector-graph"
              title="General"
              description="Choose a stable graph component and specialize it with a template."
              status={sectionStatus("graph")}
            >
              <div className="flex items-center justify-between">
                <Label className="text-sm">Component kind</Label>
                <Badge
                  variant="outline"
                  className={cn(
                    "flex items-center gap-1 px-2 text-xs font-normal",
                    getTypeColor(nodeType),
                  )}
                >
                  {getTypeIcon(nodeType)}
                  {getStepTypeLabel(nodeType)}
                </Badge>
              </div>

              <div className="space-y-2">
                <Label htmlFor="node-template" className="text-sm">
                  Template
                </Label>
                {nodeType === "task" && (
                  <Input
                    value={templateSearch}
                    onChange={(event) => setTemplateSearch(event.target.value)}
                    placeholder="Search templates or categories"
                    aria-label="Search templates"
                  />
                )}
                <select
                  id="node-template"
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
                <p className="text-xs text-muted-foreground">
                  Categories help you find concrete templates; changing one never changes the structural graph kind.
                </p>
              </div>

              <div className="space-y-2">
                <Label htmlFor="node-label" className="text-sm">Label</Label>
                <Input
                  id="node-label"
                  value={label}
                  onChange={handleLabelChange}
                  placeholder="Enter component label"
                />
              </div>

              <div className="space-y-2">
                <Label htmlFor="node-description" className="text-sm">Description</Label>
                <Input
                  id="node-description"
                  value={description}
                  onChange={handleDescriptionChange}
                  placeholder="Describe this pipeline component"
                />
              </div>
            </InspectorSection>

            <InspectorSection
              id="inspector-ports"
              title="Inputs / Outputs"
              description="The selected template owns this connection contract by default."
              status={sectionStatus("ports") || (designValidationIssues.some((issue) =>
                ["unknown-edge-port", "missing-edge-port"].includes(issue.code),
              ) ? "error" : undefined)}
            >
              <div className="flex items-center justify-between gap-3 rounded-md border border-border/70 bg-background/60 p-2">
                <div>
                  <Badge variant="outline">
                    {canCustomizePorts ? "Customized" : "Template-managed"}
                  </Badge>
                  <p className="mt-1 text-[11px] text-muted-foreground">
                    {isAdvancedMode
                      ? canCustomizePorts
                        ? "Connected port names, types, and deletion remain protected."
                        : "Unlock only when this component needs a non-standard contract."
                      : "Switch the canvas to Advanced mode to customize this contract."}
                  </p>
                </div>
                {isAdvancedMode && !portContractUnlocked && (
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    onClick={() => setPortContractUnlocked(true)}
                  >
                    <Unlock className="mr-1 h-3.5 w-3.5" /> Customize contract
                  </Button>
                )}
              </div>
              {renderPortDirection("inputs")}
              {renderPortDirection("outputs")}
            </InspectorSection>

            <InspectorSection
              id="inspector-configuration"
              title="Parameters"
              description="Static configuration stays on this component; only dynamic values belong on ports."
              status={configurationStatus === "invalid" ? "error" : sectionStatus("configuration")}
            >
              <div className="flex items-center justify-between">
                <Label className="text-sm">Parameters</Label>
                <Button type="button" variant="outline" size="sm" onClick={addParamRow}>
                  <PlusCircle className="mr-2 h-4 w-4" />
                  Add field
                </Button>
              </div>

              <div className="space-y-2">
                {editableParamEntries.length === 0 && (
                  <p className="text-xs text-muted-foreground">No static parameters configured.</p>
                )}

                {editableParamEntries.length > 0 && (
                  <p className="text-xs text-muted-foreground">
                    Secret values are masked by default. Use the lock to classify a field and the eye to reveal it locally.
                  </p>
                )}

                {editableParamEntries.map(([k, v]) => {
                  const isSecret = secretParamKeys.includes(k);
                  const isRevealed = revealedSecretParams.has(k);
                  return (
                    <div key={k} className="grid grid-cols-[1fr_1fr_auto_auto] gap-2 items-center">
                      <Input
                        value={draftKeys[k] ?? k}
                        placeholder="key"
                        onChange={(e) => {
                          e.stopPropagation();
                          setDraftKeys((prev) => ({ ...prev, [k]: e.target.value }));
                        }}
                        onKeyDown={(e) => {
                          e.stopPropagation();
                          if (e.key === "Enter") {
                            e.preventDefault();
                            const newKey = (draftKeys[k] ?? "").trim();
                            if (newKey && newKey !== k) renameParamKey(k, newKey);
                            setDraftKeys((prev) => {
                              const copy = { ...prev };
                              delete copy[k];
                              return copy;
                            });
                          }
                        }}
                        onBlur={() => {
                          const newKey = (draftKeys[k] ?? "").trim();
                          if (newKey && newKey !== k) renameParamKey(k, newKey);
                          setDraftKeys((prev) => {
                            const copy = { ...prev };
                            delete copy[k];
                            return copy;
                          });
                        }}
                      />
                      <div className="relative">
                        <Input
                          type={isSecret && !isRevealed ? "password" : "text"}
                          value={typeof v === "string" ? v : JSON.stringify(v ?? "")}
                          onChange={(e) => setParamValue(k, e.target.value)}
                          placeholder="value"
                          className={isSecret ? "pr-9" : undefined}
                        />
                        {isSecret && (
                          <Button
                            type="button"
                            variant="ghost"
                            size="icon"
                            className="absolute right-0 top-0 h-9 w-9"
                            onClick={() => toggleSecretVisibility(k)}
                            aria-label={isRevealed ? `Hide ${k}` : `Show ${k}`}
                            title={isRevealed ? "Hide secret value" : "Show secret value"}
                          >
                            {isRevealed
                              ? <EyeOff className="w-4 h-4" />
                              : <Eye className="w-4 h-4" />}
                          </Button>
                        )}
                      </div>
                      <Button
                        type="button"
                        variant={isSecret ? "secondary" : "ghost"}
                        size="sm"
                        onClick={() => toggleSecretParam(k)}
                        aria-label={isSecret ? `Make ${k} visible` : `Make ${k} secret`}
                        title={isSecret ? "Treat as ordinary parameter" : "Mask as secret parameter"}
                      >
                        {isSecret
                          ? <LockKeyhole className="w-4 h-4" />
                          : <Unlock className="w-4 h-4" />}
                      </Button>
                      <Button
                        type="button"
                        variant="ghost"
                        size="sm"
                        onClick={() => removeParamKey(k)}
                        aria-label={`Remove parameter ${k}`}
                      >
                        <X className="w-4 h-4" />
                      </Button>
                    </div>
                  );
                })}
              </div>
            </InspectorSection>

            {canGenerateScript && (
              <InspectorSection
                id="inspector-implementation"
                title="Runtime Package"
                description="Task code runs from this package. For Python, attach main.py and requirements.txt (not requirements.py). Source and Destination components use inLumen-managed adapters."
                status={sectionStatus("implementation")}
              >
                <div className="space-y-2">
                  <Label htmlFor="implementation-kind" className="text-sm">Runtime</Label>
                  <select
                    id="implementation-kind"
                    className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm"
                    value={implementationKind}
                    onChange={(event) => handleImplementationKindChange(event.target.value)}
                  >
                    {!IMPLEMENTATION_KIND_OPTIONS.some((option) => option.value === implementationKind) && (
                      <option value={implementationKind}>Custom runtime ({implementationKind})</option>
                    )}
                    {IMPLEMENTATION_KIND_OPTIONS.map((option) => (
                      <option key={option.value} value={option.value}>{option.label}</option>
                    ))}
                  </select>
                </div>

                {['container', 'repository', 'rest-api'].includes(implementationKind) && (
                  <div className="space-y-2">
                    <Label htmlFor="implementation-reference" className="text-sm">
                      {implementationKind === 'container'
                        ? 'Container image'
                        : implementationKind === 'repository'
                          ? 'Repository URL'
                          : 'API endpoint'}
                    </Label>
                    <Input
                      id="implementation-reference"
                      value={implementationReference}
                      onChange={(event) => {
                        const value = event.target.value;
                        setImplementationReference(value);
                        updateImplementation({
                          [implementationKind === 'container'
                            ? 'image'
                            : implementationKind === 'repository'
                              ? 'repository'
                              : 'endpoint']: value,
                        });
                      }}
                    />
                  </div>
                )}

                <div className="space-y-2">
                  <Label htmlFor="implementation-language" className="text-sm">Language</Label>
                  <Input
                    id="implementation-language"
                    value={implementationLanguage}
                    onChange={(event) => {
                      setImplementationLanguage(event.target.value);
                      updateImplementation({ language: event.target.value });
                    }}
                    placeholder="python"
                  />
                </div>

                <div className="space-y-2">
                  <Label htmlFor="implementation-entrypoint" className="text-sm">Entrypoint</Label>
                  <Input
                    id="implementation-entrypoint"
                    value={implementationEntrypoint}
                    onChange={(event) => {
                      setImplementationEntrypoint(event.target.value);
                      updateImplementation({ entrypoint: event.target.value });
                    }}
                    placeholder="main.py"
                  />
                </div>

                <div className="space-y-2">
                  <Label htmlFor="implementation-dependencies" className="text-sm">Dependencies</Label>
                  <Input
                    id="implementation-dependencies"
                    value={implementationDependencies}
                    onChange={(event) => {
                      const value = event.target.value;
                      setImplementationDependencies(value);
                      updateImplementation({
                        dependencies: value.split(',').map((item) => item.trim()).filter(Boolean),
                      });
                    }}
                    placeholder="pandas, pyarrow"
                  />
                </div>

                <div className="flex items-center justify-between gap-3">
                  <div>
                    <Label className="text-sm">Code</Label>
                    <p className="mt-1 text-xs text-muted-foreground">
                      Generate code or attach the entrypoint and dependency files used to execute this node.
                    </p>
                  </div>
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    onClick={() => { void handleGenerateScript(); }}
                    disabled={isGeneratingScript}
                  >
                    {isGeneratingScript
                      ? <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                      : <Wand2 className="mr-2 h-4 w-4" />}
                    Generate
                  </Button>
                </div>

                {renderFileArea("code", indexedCodeFiles, codeFileInputRef)}
              </InspectorSection>
            )}

            {nodeType === "source" && (
              <InspectorSection
                title="Source Settings"
                description="Use Parameters for ordinary source settings; the selected template defines the source contract."
              >
                {isAdvancedMode && (
                  <div className="space-y-2 rounded-md border border-border/70 p-2">
                    <Label htmlFor="source-configuration" className="text-sm">Advanced source settings (JSON)</Label>
                    <Textarea
                      id="source-configuration"
                      value={sourceConfigDraft}
                      onChange={(event) => setSourceConfigDraft(event.target.value)}
                      onBlur={persistSourceConfiguration}
                      className="min-h-28 font-mono text-xs"
                    />
                    <p className="text-xs text-muted-foreground">Only use this for source-specific settings that cannot be represented as Parameters.</p>
                  </div>
                )}
                <div className="space-y-2">
                  <Label htmlFor="node-content" className="text-sm">Source notes</Label>
                  <Textarea
                    id="node-content"
                    value={content}
                    onChange={handleContentChange}
                    placeholder={`Describe the ${nodeType === "source" ? "source" : "destination"} data contract...`}
                    className="h-24 resize-none"
                  />
                </div>
              </InspectorSection>
            )}

            {nodeType === "destination" && (
              <InspectorSection
                title="Destination Settings"
                description="Use Parameters for delivery settings; the selected template defines the destination contract."
              >
                <div className="space-y-2">
                  <Label htmlFor="node-content" className="text-sm">Destination notes</Label>
                  <Textarea
                    id="node-content"
                    value={content}
                    onChange={handleContentChange}
                    placeholder="Describe how pipeline results are delivered..."
                    className="h-24 resize-none"
                  />
                </div>
              </InspectorSection>
            )}

            {nodeType === "subpipeline" && (
              <InspectorSection
                title="Nested Graph"
                description="Subpipelines retain an independent reusable graph inside the Project JSON."
              >
                <Textarea
                  value={subpipelineGraphDraft}
                  onChange={(event) => setSubpipelineGraphDraft(event.target.value)}
                  onBlur={persistSubpipelineGraph}
                  className="min-h-36 font-mono text-xs"
                />
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={() => pushNodeUpdate({
                    subpipeline: {
                      ...(selectedNode.data.subpipeline || {}),
                      expanded: !selectedNode.data.subpipeline?.expanded,
                    },
                  })}
                >
                  {selectedNode.data.subpipeline?.expanded ? 'Collapse' : 'Expand'} subpipeline
                </Button>
              </InspectorSection>
            )}

            <InspectorSection
              title="Test Fixtures"
              description="Persisted fixtures support inspection, code generation, and repeatable tests. Supply actual input files from the Run tab; those are not attached to the node."
            >
              {canManageFiles && renderFileArea("data", indexedDataFiles, dataFileInputRef)}
            </InspectorSection>

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
