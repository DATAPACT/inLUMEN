import React, { useState, useEffect, useRef, useCallback } from 'react';
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import { PlusCircle, Upload, X, Eye } from 'lucide-react';
import { FilePreviewDialog, PreviewType } from '@/components/properties/FilePreviewDialog';
import { getTypeColor, getTypeIcon } from '@/components/properties/nodeAppearance';
import {
  normalizeType,
  pickNeo4jUpdatableProps,
  StepType,
  STORAGE_DATABASE_OPTIONS,
  StorageDatabaseOption,
  normalizeStorageDatabaseOption,
  getNodeFileName,
  isBrowserFile,
  typeHasContent,
  typeHasEndpoint,
  typeHasFiles,
  isImagePreviewName,
  isTextPreviewName,
  isTextPreviewFile,
  NodeFileReference,
} from '@/features/nodes/nodeSchema';
import {
  readNodeFile,
  removeNodeFile,
  updateNodeTextFile,
  updateNodePropertiesInNeo4j,
  uploadNodeFile,
} from '@/features/nodes/nodePersistence';
import { Node } from 'reactflow';

type NodeParamMap = Record<string, string>;

export type PropertyNodeData = {
  label?: string;
  description?: string;
  type?: StepType | string;
  content?: string;
  files?: NodeFileReference[];
  has_files?: string;
  param?: NodeParamMap;
  endpoint?: string;
  database?: StorageDatabaseOption | string;
  [key: string]: unknown;
};

const normalizeFileReferences = (value: unknown): NodeFileReference[] => {
  if (!Array.isArray(value)) return [];
  return value.filter((file): file is NodeFileReference => {
    return getNodeFileName(file as NodeFileReference).length > 0;
  });
};

interface PropertiesPanelProps {
  selectedNode: Node<PropertyNodeData> | null;
  onNodeUpdate: (id: string, data: PropertyNodeData) => void;
  onRemoveNode?: (nodeId: string) => void;
  className?: string;
}

export function PropertiesPanel({ selectedNode, onNodeUpdate, onRemoveNode, className }: PropertiesPanelProps) {
  const nodeType: StepType = normalizeType(selectedNode?.data?.type ?? selectedNode?.type);

  const [label, setLabel] = useState('');
  const [description, setDescription] = useState('');

  // input/output only
  const [content, setContent] = useState('');

  // file state (input/output/action/custom)
  const [files, setFiles] = useState<NodeFileReference[]>([]);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // config param state (config only)
  const [param, setParam] = useState<NodeParamMap>({});
  const [draftKeys, setDraftKeys] = useState<NodeParamMap>({});

  // endpoint state (storage/api only)
  const [endpoint, setEndpoint] = useState('');

  // storage database dropdown
  const [databaseName, setDatabaseName] = useState<StorageDatabaseOption>("MinIO");

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

  // Debounce Neo4j updates to avoid POST per keystroke
  const neo4jDebounceRef = useRef<number | null>(null);
  const debouncedUpdatePropertyToNeo4J = useCallback((nodeId: string, properties: Record<string, unknown>) => {
    if (neo4jDebounceRef.current) window.clearTimeout(neo4jDebounceRef.current);
    neo4jDebounceRef.current = window.setTimeout(() => {
      updateNodePropertiesInNeo4j(nodeId, properties);
    }, 300);
  }, []);

  useEffect(() => {
    return () => {
      if (neo4jDebounceRef.current) window.clearTimeout(neo4jDebounceRef.current);
    };
  }, []);

  // Enforce type-specific rules before persisting into node.data
  const pushNodeUpdate = (patch: Partial<PropertyNodeData>) => {
    if (!selectedNode) return;

    const next: PropertyNodeData = { ...selectedNode.data, ...patch, type: nodeType };

    // Content only for input/output
    if (!typeHasContent(nodeType)) {
      delete next.content;
    } else {
      if (next.content == null) next.content = "";
    }

    // Files only for input/output/action/custom, and has_files is internal/derived
    if (!typeHasFiles(nodeType)) {
      delete next.files;
      delete next.has_files;
    } else {
      const filesArr = normalizeFileReferences(next.files);
      next.files = filesArr;
      next.has_files = filesArr.length > 0 ? "yes" : "no"; // internal
    }

    // Config: param editor, no content, no files
    if (nodeType === "config") {
      next.param = next.param ?? {};
      if (typeof next.param !== "object" || Array.isArray(next.param) || next.param == null) next.param = {};
      delete next.content;
      delete next.files;
      delete next.has_files;
    } else {
      delete next.param;
    }

    // Endpoint only for storage/api
    if (!typeHasEndpoint(nodeType)) {
      delete next.endpoint;
    } else {
      if (next.endpoint == null) next.endpoint = "";
    }

    // Storage database dropdown only for storage
    if (nodeType === "storage") {
      if (!next.database) next.database = "MinIO";
    } else {
      delete next.database;
    }

    // 1) update local reactflow node
    onNodeUpdate(selectedNode.id, next);

    // 2) update Neo4j (only allowed props)
    const neo4jProps = pickNeo4jUpdatableProps(selectedNode.id, next, nodeType);
    debouncedUpdatePropertyToNeo4J(selectedNode.id, neo4jProps);
  };

  useEffect(() => {
    if (selectedNode) {
      setLabel(selectedNode.data.label || '');
      setDescription(selectedNode.data.description || '');

      // content only for input/output
      setContent(typeHasContent(nodeType) ? (selectedNode.data.content || '') : '');

      // files only for input/output/action/custom
      setFiles(typeHasFiles(nodeType) ? normalizeFileReferences(selectedNode.data.files) : []);

      // param only for config
      if (nodeType === "config") {
        const p = selectedNode.data.param;
        setParam((p && typeof p === "object" && !Array.isArray(p)) ? p as NodeParamMap : {});
      } else {
        setParam({});
      }

      // endpoint only for storage/api
      setEndpoint(typeHasEndpoint(nodeType) ? (selectedNode.data.endpoint || "") : "");

      // database only for storage
      if (nodeType === "storage") {
        setDatabaseName(normalizeStorageDatabaseOption(selectedNode.data.database));
      } else {
        setDatabaseName("MinIO");
      }
    } else {
      setLabel('');
      setDescription('');
      setContent('');
      setFiles([]);
      setParam({});
      setEndpoint("");
      setDatabaseName("MinIO");
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

  const handleEndpointChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    setEndpoint(e.target.value);
    pushNodeUpdate({ endpoint: e.target.value });
  };

  const handleDatabaseChange = (val: StorageDatabaseOption) => {
    setDatabaseName(val);
    pushNodeUpdate({ database: val });
  };

  // Upload newly added files to MinIO (bucket_id = selectedNode.id). Overwrite behavior in UI: same filename replaces older entry
  const handleFileUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    if (!typeHasFiles(nodeType)) return;
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
    const updatedFiles = [...existing];
    const changedFiles: File[] = [];
    for (const f of picked) {
      const idx = nameToIndex.get(f.name);
      if (idx != null) {
        // Replace existing file with same name
        updatedFiles[idx] = f;
      } else {
        // Add new file
        updatedFiles.push(f);
        nameToIndex.set(f.name, updatedFiles.length - 1);
      }
      changedFiles.push(f);
    }
    setFiles(updatedFiles);
    pushNodeUpdate({ files: updatedFiles });
    const nodeId = selectedNode.id;
    const results = await Promise.allSettled(
      changedFiles.map((f) => uploadNodeFile(nodeId, f))
    );
    const failed = results.filter((r) => r.status === "rejected");
    if (failed.length > 0) {
      console.warn(
        `[PropertiesPanel.tsx] ${failed.length}/${changedFiles.length} uploads failed for node ${nodeId}`
      );
    }
    e.target.value = "";
  };

  const removeFile = async (index: number) => {
    if (!selectedNode) return;
    const fileToRemove = files[index];
    if (!fileToRemove) return;
    const updatedFiles = files.filter((_, i) => i !== index);
    setFiles(updatedFiles);
    pushNodeUpdate({ files: updatedFiles });
    try {
      await removeNodeFile(selectedNode.id, fileToRemove);
    } catch (err) {
      console.warn("[PropertiesPanel.tsx] Removed locally, but MinIO removal failed:", err);
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

  // Save edited text file and re-upload to MinIO
  const saveFileChanges = async () => {
    if (!selectedNode) return;
    if (previewFileIndex === -1 || previewType !== 'text') return;

    const currentFile = files[previewFileIndex];
    if (!currentFile) return;
    const currentFileName = getNodeFileName(currentFile);
    if (!currentFileName) return;
    const fileType = previewFile?.type || "text/plain";
    const newFile = new File([new Blob([editedContent], { type: fileType })], currentFileName, {
      type: fileType,
      lastModified: Date.now(),
    });

    const updatedFiles = [...files];
    updatedFiles[previewFileIndex] = isBrowserFile(currentFile) ? newFile : currentFile;

    // 1) Update UI/state + Neo4j
    setFiles(updatedFiles);
    pushNodeUpdate({ files: updatedFiles });

    // 2) Upload updated file to MinIO
    try {
      if (isBrowserFile(currentFile)) {
        await uploadNodeFile(selectedNode.id, newFile);
        setPreviewFile(newFile);
      } else {
        await updateNodeTextFile(selectedNode.id, currentFile, editedContent);
      }
    } catch (err) {
      console.warn("[PropertiesPanel.tsx] File saved locally, but MinIO update failed:", err);
    }

    setPreviewContent(editedContent);
    setIsEditing(false);
  };

  // Config param helpers
  const addParamRow = () => {
    let i = 1;
    let key = `key_${i}`;
    while (param[key] != null) {
      i += 1;
      key = `key_${i}`;
    }
    const next = { ...param, [key]: "" };
    setParam(next);
    pushNodeUpdate({ param: next });
  };

  const renameParamKey = (oldKey: string, newKeyRaw: string) => {
    const newKey = newKeyRaw.trim();
    if (!newKey || newKey === oldKey) return;
    const next: Record<string, string> = {};
    Object.entries(param).forEach(([k, v]) => {
      if (k === oldKey) next[newKey] = v ?? "";
      else next[k] = v ?? "";
    });
    setParam(next);
    pushNodeUpdate({ param: next });
    setDraftKeys((prev) => {
      const copy = { ...prev };
      delete copy[oldKey];
      return copy;
    });
  };

  const setParamValue = (key: string, value: string) => {
    const next = { ...param, [key]: value };
    setParam(next);
    pushNodeUpdate({ param: next });
  };

  const removeParamKey = (key: string) => {
    const next = { ...param };
    delete next[key];
    setParam(next);
    pushNodeUpdate({ param: next });
    setDraftKeys((prev) => {
      const copy = { ...prev };
      delete copy[key];
      return copy;
    });
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
            <div>
              <div className="flex items-center justify-between mb-3">
                <Label htmlFor="node-type" className="text-sm">Node Type</Label>
                <Badge
                  variant="outline"
                  className={cn(
                    "text-xs font-normal flex items-center gap-1 px-2",
                    getTypeColor(nodeType)
                  )}
                >
                  {getTypeIcon(nodeType)}
                  {nodeType}
                </Badge>
              </div>
            </div>

            <div className="space-y-2">
              <Label htmlFor="node-label" className="text-sm">Label</Label>
              <Input
                id="node-label"
                value={label}
                onChange={handleLabelChange}
                placeholder="Enter node label"
              />
            </div>

            <div className="space-y-2">
              <Label htmlFor="node-description" className="text-sm">Description</Label>
              <Input
                id="node-description"
                value={description}
                onChange={handleDescriptionChange}
                placeholder="Enter node description"
              />
            </div>

            {/* Content ONLY for input/output */}
            {typeHasContent(nodeType) && (
              <div className="space-y-2">
                <Label htmlFor="node-content" className="text-sm">Content</Label>
                <Textarea
                  id="node-content"
                  value={content}
                  onChange={handleContentChange}
                  placeholder={`Enter ${nodeType} content...`}
                  className="h-32 resize-none"
                />
                <p className="text-xs text-muted-foreground mt-1">
                  {nodeType === 'input'
                    ? 'Content for input nodes.'
                    : 'Content for output nodes.'}
                </p>
              </div>
            )}

            {/* Config ONLY: param editor */}
            {nodeType === "config" && (
              <div className="space-y-2">
                <div className="flex items-center justify-between">
                  <Label className="text-sm">Config parameters</Label>
                  <Button type="button" variant="outline" size="sm" onClick={addParamRow}>
                    <PlusCircle className="w-4 h-4 mr-2" />
                    Add field
                  </Button>
                </div>

                <div className="space-y-2">
                  {Object.entries(param).length === 0 && (
                    <p className="text-xs text-muted-foreground">No parameters yet.</p>
                  )}

                  {Object.entries(param).map(([k, v]) => (
                    <div key={k} className="grid grid-cols-[1fr_1fr_auto] gap-2 items-center">
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
                      <Input
                        value={v ?? ""}
                        onChange={(e) => setParamValue(k, e.target.value)}
                        placeholder="value"
                      />
                      <Button
                        type="button"
                        variant="ghost"
                        size="sm"
                        onClick={() => removeParamKey(k)}
                      >
                        <X className="w-4 h-4" />
                      </Button>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* Storage ONLY: endpoint + database dropdown */}
            {nodeType === "storage" && (
              <div className="space-y-3">
                <div className="space-y-2">
                  <Label className="text-sm">Endpoint</Label>
                  <Input
                    value={endpoint}
                    onChange={handleEndpointChange}
                    placeholder="http://..."
                  />
                </div>

                <div className="space-y-2">
                  <Label className="text-sm">Database</Label>
                  <select
                    className="w-full h-9 rounded-md border border-input bg-background px-3 text-sm"
                    value={databaseName}
                    onChange={(e) => handleDatabaseChange(normalizeStorageDatabaseOption(e.target.value))}
                  >
                    {STORAGE_DATABASE_OPTIONS.map((opt) => (
                      <option key={opt} value={opt}>{opt}</option>
                    ))}
                  </select>
                  <p className="text-xs text-muted-foreground">
                    Stored in Neo4j as lowercase (e.g. <code>minio</code>, <code>sqlite</code>, <code>chromadb</code>).
                  </p>
                </div>
              </div>
            )}

            {/* API ONLY: endpoint */}
            {nodeType === "api" && (
              <div className="space-y-2">
                <Label className="text-sm">Endpoint</Label>
                <Input
                  value={endpoint}
                  onChange={handleEndpointChange}
                  placeholder="http://..."
                />
              </div>
            )}

            {/* Files ONLY for input/output/action/custom (has_files derived internally) */}
            {typeHasFiles(nodeType) && (
              <div className="space-y-2">
                <Label className="text-sm">Files</Label>
                <div className="border border-dashed border-border rounded-lg p-4">
                  <input
                    ref={fileInputRef}
                    type="file"
                    multiple
                    onChange={handleFileUpload}
                    className="hidden"
                  />
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    onClick={() => fileInputRef.current?.click()}
                    className="w-full"
                  >
                    <Upload className="w-4 h-4 mr-2" />
                    Upload Files
                  </Button>

                  {files.length > 0 && (
                    <div className="mt-3 space-y-2">
                      {files.map((file, index) => (
                        <div key={index} className="flex items-center justify-between bg-muted/50 p-2 rounded">
                          <span className="text-xs truncate flex-1">{getNodeFileName(file)}</span>
                          <div className="flex items-center gap-1">
                            <Button
                              type="button"
                              variant="ghost"
                              size="sm"
                              onClick={() => viewFile(file, index)}
                            >
                              <Eye className="w-3 h-3" />
                            </Button>
                            <Button
                              type="button"
                              variant="ghost"
                              size="sm"
                              onClick={() => { void removeFile(index); }}
                            >
                              <X className="w-3 h-3" />
                            </Button>
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              </div>
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
