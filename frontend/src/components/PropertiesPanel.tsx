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
import { ChatbotConfig, buildCodegenLLMRequestConfig } from '@/services/chatbotService';
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
  definition_id?: string;
  definition_version?: number;
  implementation?: Record<string, unknown>;
  configuration_status?: "unconfigured" | "valid" | "invalid";
  generated_artifact?: GeneratedArtifact;
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
  title,
  description,
  children,
}: {
  title: string;
  description?: string;
  children: React.ReactNode;
}) => (
  <section className="space-y-3 rounded-lg border border-border bg-muted/10 p-3">
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
  className?: string;
}

export function PropertiesPanel({
  selectedNode,
  onNodeUpdate,
  onRemoveNode,
  activeChatbotConfig,
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
  const [implementationKind, setImplementationKind] = useState(() =>
    normalizeImplementationKind(selectedNode?.data?.implementation?.kind),
  );
  const currentTemplate = String(
    selectedNode?.data?.template_label || defaultTemplateForType(nodeType),
  );
  const templateOptions = templateOptionsForType(nodeType, currentTemplate);
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

    } else {
      setLabel('');
      setDescription('');
      setContent('');
      setFiles([]);
      setParam({});
      setSecretParamKeys([]);
      setPorts(normalizeNodePorts(undefined, nodeType));
      setImplementationKind("generated-code");
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
    pushNodeUpdate({ template_label: templateLabel });
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
    let llmConfig: Record<string, unknown> | undefined;
    try {
      if (activeChatbotConfig) {
        llmConfig = buildCodegenLLMRequestConfig(activeChatbotConfig);
      }
    } catch (error) {
      toast("Code generation model required", {
        description: error instanceof Error ? error.message : "LLM settings are incomplete.",
      });
      setIsGeneratingScript(false);
      return;
    }

    try {
      const result = await generateNodeScript(selectedNode.id, {
        ...(llmConfig ? { llm_config: llmConfig } : {}),
      });
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
      [direction]: [...existing, { id, label: id }],
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
        Upload {role === "code" ? "Code" : "Data"} Files
      </Button>

      {indexedFiles.length === 0 ? (
        <p className="mt-3 text-center text-xs text-muted-foreground">
          No {role === "code" ? "implementation" : "data"} files attached.
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
              title="General"
              description="Choose a stable graph component and specialize it with a template."
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
                  {nodeType === "source" ? "Adapter template" : "Template"}
                </Label>
                <select
                  id="node-template"
                  className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm"
                  value={currentTemplate}
                  onChange={(event) => handleTemplateChange(event.target.value)}
                >
                  {templateOptions.map((option) => (
                    <option key={option.value} value={option.value}>{option.label}</option>
                  ))}
                </select>
                <p className="text-xs text-muted-foreground">
                  Changing this selection never changes the structural graph kind.
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
              title="Inputs / Outputs"
              description="Named ports are the logical data contract used by graph connections."
            >
              <div className="space-y-2">
                <div className="flex items-center justify-between">
                  <Label className="text-xs uppercase tracking-wide text-muted-foreground">Inputs</Label>
                  {nodeType !== "source" && (
                    <Button type="button" variant="outline" size="sm" onClick={() => addPort("inputs")}>
                      <PlusCircle className="mr-1 h-3.5 w-3.5" /> Add
                    </Button>
                  )}
                </div>
                {ports.inputs.length === 0 && (
                  <p className="text-xs text-muted-foreground">
                    {nodeType === "source" ? "Source adapters have no pipeline input." : "No inputs defined."}
                  </p>
                )}
                {ports.inputs.map((port) => (
                  <div key={port.id} className="grid grid-cols-[1fr_1fr_auto] gap-2">
                    <Input
                      value={port.label}
                      onChange={(event) => updatePort("inputs", port.id, { label: event.target.value })}
                      placeholder="name"
                    />
                    <Input
                      value={port.data_type || ""}
                      onChange={(event) => updatePort("inputs", port.id, { data_type: event.target.value })}
                      placeholder="data type"
                    />
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      onClick={() => removePort("inputs", port.id)}
                      aria-label={`Remove input ${port.label}`}
                    >
                      <X className="h-4 w-4" />
                    </Button>
                  </div>
                ))}
              </div>

              <div className="space-y-2">
                <div className="flex items-center justify-between">
                  <Label className="text-xs uppercase tracking-wide text-muted-foreground">Outputs</Label>
                  {nodeType !== "sink" && (
                    <Button type="button" variant="outline" size="sm" onClick={() => addPort("outputs")}>
                      <PlusCircle className="mr-1 h-3.5 w-3.5" /> Add
                    </Button>
                  )}
                </div>
                {ports.outputs.length === 0 && (
                  <p className="text-xs text-muted-foreground">
                    {nodeType === "sink" ? "Destinations have no pipeline output." : "No outputs defined."}
                  </p>
                )}
                {ports.outputs.map((port) => (
                  <div key={port.id} className="grid grid-cols-[1fr_1fr_auto] gap-2">
                    <Input
                      value={port.label}
                      onChange={(event) => updatePort("outputs", port.id, { label: event.target.value })}
                      placeholder="name"
                    />
                    <Input
                      value={port.data_type || ""}
                      onChange={(event) => updatePort("outputs", port.id, { data_type: event.target.value })}
                      placeholder="data type"
                    />
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      onClick={() => removePort("outputs", port.id)}
                      aria-label={`Remove output ${port.label}`}
                    >
                      <X className="h-4 w-4" />
                    </Button>
                  </div>
                ))}
              </div>
            </InspectorSection>

            <InspectorSection
              title="Parameters"
              description="Static configuration stays on this component; only dynamic values belong on ports."
            >
              <div className="flex items-center justify-between">
                <Label className="text-sm">Configuration fields</Label>
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
                title="Implementation"
                description="Runtime technology is independent of the Task template and graph kind."
              >
                <div className="space-y-2">
                  <Label htmlFor="implementation-kind" className="text-sm">Runtime</Label>
                  <select
                    id="implementation-kind"
                    className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm"
                    value={implementationKind}
                    onChange={(event) => handleImplementationKindChange(event.target.value)}
                  >
                    {IMPLEMENTATION_KIND_OPTIONS.map((option) => (
                      <option key={option.value} value={option.value}>{option.label}</option>
                    ))}
                  </select>
                </div>

                <div className="flex items-center justify-between gap-3">
                  <div>
                    <Label className="text-sm">Code</Label>
                    <p className="mt-1 text-xs text-muted-foreground">
                      Generate code or attach files from an upload, Git repository, or container workflow.
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

            <InspectorSection
              title="Data"
              description="Data artifacts are stored separately from Task implementation code."
            >
              {typeHasContent(nodeType) && (
                <div className="space-y-2">
                  <Label htmlFor="node-content" className="text-sm">Adapter notes</Label>
                  <Textarea
                    id="node-content"
                    value={content}
                    onChange={handleContentChange}
                    placeholder={`Describe the ${nodeType === "source" ? "source" : "destination"} data contract...`}
                    className="h-24 resize-none"
                  />
                </div>
              )}
              {canManageFiles && renderFileArea("data", indexedDataFiles, dataFileInputRef)}
            </InspectorSection>

            <InspectorSection
              title="Validation"
              description="Configuration and generated implementation checks are reported here."
            >
              <div className="flex items-center justify-between text-sm">
                <span>Configuration</span>
                <Badge variant="outline" className="capitalize">{configurationStatus}</Badge>
              </div>
              <div className="flex items-center justify-between text-sm">
                <span>Implementation</span>
                <Badge variant="outline" className="capitalize">
                  {String(validationReport?.status || (canGenerateScript ? "not validated" : "not applicable"))}
                </Badge>
              </div>
              {Array.isArray(validationReport?.errors) && validationReport.errors.length > 0 && (
                <div className="rounded border border-destructive/30 bg-destructive/10 p-2 text-xs text-destructive">
                  {validationReport.errors.join(" ")}
                </div>
              )}
              {Array.isArray(validationReport?.warnings) && validationReport.warnings.length > 0 && (
                <div className="rounded border border-amber-500/30 bg-amber-500/10 p-2 text-xs text-amber-200">
                  {validationReport.warnings.join(" ")}
                </div>
              )}
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
