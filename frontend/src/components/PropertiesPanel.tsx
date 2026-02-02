import React, { useState, useEffect, useRef, useCallback } from 'react';
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import {
  Brain, MessageCircle, FileText, Zap, Database, Settings, Clipboard, PlusCircle,
  Upload, X, Trash2, Eye, Edit, Save
} from 'lucide-react';

interface PropertiesPanelProps {
  selectedNode: any;
  onNodeUpdate: (id: string, data: any) => void;
  onRemoveNode?: (nodeId: string) => void;
}

type StepType =
  | "action"
  | "input"
  | "output"
  | "config"
  | "storage"
  | "api"
  | "custom";

const STORAGE_DATABASE_OPTIONS = ["MinIO", "SQLite", "ChromaDB"] as const;

const normalizeType = (t: any): StepType => {
  const s = String(t ?? "").toLowerCase().trim();
  if (
    s === "action" ||
    s === "input" ||
    s === "output" ||
    s === "config" ||
    s === "storage" ||
    s === "api" ||
    s === "custom"
  ) return s;
  return "action";
};

// Files supported where backend sets has_files: input/output/action/custom
const typeHasFiles = (t: StepType) => t === "input" || t === "output" || t === "action" || t === "custom";

// Content supported only for input/output
const typeHasContent = (t: StepType) => t === "input" || t === "output";

// Endpoint supported only for storage/api
const typeHasEndpoint = (t: StepType) => t === "storage" || t === "api";

const toDbValue = (uiValue: string) => {
  // "MinIO" -> "minio", "SQLite" -> "sqlite", "ChromaDB" -> "chromadb"
  return String(uiValue ?? "").toLowerCase().trim();
};

function pickNeo4jUpdatableProps(nodeId: string, nodeData: any, nodeType: StepType) {
  // Only send what your backend expects / allows
  const props: Record<string, any> = {
    flow_id: nodeId,
    label: nodeData.label ?? "",
    type: nodeType,
    description: nodeData.description ?? ""
  };

  if (typeHasContent(nodeType)) {
    props.content = nodeData.content ?? "";
  }

  if (typeHasFiles(nodeType)) {
    props.has_files = nodeData.has_files ?? "no"; // derived already in pushNodeUpdate
    // We do NOT send actual files to Neo4j (File objects aren’t JSON-serializable)
  }

  if (nodeType === "config") {
    // Send param (object) and let backend convert to param_json
    props.param = nodeData.param ?? {};
  }

  if (typeHasEndpoint(nodeType)) {
    props.endpoint = nodeData.endpoint ?? "";
  }

  if (nodeType === "storage") {
    props.database = toDbValue(nodeData.database ?? "MinIO");
  }

  return props;
}

export function PropertiesPanel({ selectedNode, onNodeUpdate, onRemoveNode }: PropertiesPanelProps) {
  const nodeType: StepType = normalizeType(selectedNode?.data?.type ?? selectedNode?.type);

  const [label, setLabel] = useState('');
  const [description, setDescription] = useState('');

  // input/output only
  const [content, setContent] = useState('');

  // file state (input/output/action/custom)
  const [files, setFiles] = useState<File[]>([]);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // config param state (config only)
  const [param, setParam] = useState<Record<string, string>>({});
  const [draftKeys, setDraftKeys] = useState<Record<string, string>>({});

  // endpoint state (storage/api only)
  const [endpoint, setEndpoint] = useState('');

  // storage database dropdown
  const [databaseName, setDatabaseName] = useState<(typeof STORAGE_DATABASE_OPTIONS)[number]>("MinIO");

  // preview/edit file dialog state
  const [previewFile, setPreviewFile] = useState<File | null>(null);
  const [previewContent, setPreviewContent] = useState('');
  const [previewType, setPreviewType] = useState<'text' | 'image' | 'binary'>('text');
  const [isEditing, setIsEditing] = useState(false);
  const [editedContent, setEditedContent] = useState('');
  const [previewFileIndex, setPreviewFileIndex] = useState<number>(-1);

  // --- Neo4j update function ---
  const updatePropertyToNeo4J = useCallback(async (nodeId: string, properties: Record<string, any>) => {
    try {
      const response = await fetch('http://localhost:5001/neo4j_update_node', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        // Shape assumption: { flow_id, properties }
        body: JSON.stringify({
          flow_id: nodeId,
          properties
        })
      });

      if (!response.ok) throw new Error('Failed to update node in Neo4j');
      const result = await response.json();
      console.log("[PropertiesPanel.tsx] Neo4j update_node:", result);
    } catch (err) {
      console.error("[PropertiesPanel.tsx] Neo4j update node error:", err);
    }
  }, []);

  // Debounce Neo4j updates to avoid POST per keystroke
  const neo4jDebounceRef = useRef<number | null>(null);
  const debouncedUpdatePropertyToNeo4J = useCallback((nodeId: string, properties: Record<string, any>) => {
    if (neo4jDebounceRef.current) window.clearTimeout(neo4jDebounceRef.current);
    neo4jDebounceRef.current = window.setTimeout(() => {
      updatePropertyToNeo4J(nodeId, properties);
    }, 300);
  }, [updatePropertyToNeo4J]);

  useEffect(() => {
    return () => {
      if (neo4jDebounceRef.current) window.clearTimeout(neo4jDebounceRef.current);
    };
  }, []);

  // Enforce type-specific rules before persisting into node.data
  const pushNodeUpdate = (patch: Record<string, any>) => {
    if (!selectedNode) return;

    const next = { ...selectedNode.data, ...patch, type: nodeType };

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
      const filesArr: File[] = next.files ?? [];
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
      setFiles(typeHasFiles(nodeType) ? (selectedNode.data.files || []) : []);

      // param only for config
      if (nodeType === "config") {
        const p = selectedNode.data.param;
        setParam((p && typeof p === "object" && !Array.isArray(p)) ? p : {});
      } else {
        setParam({});
      }

      // endpoint only for storage/api
      setEndpoint(typeHasEndpoint(nodeType) ? (selectedNode.data.endpoint || "") : "");

      // database only for storage
      if (nodeType === "storage") {
        const db = selectedNode.data.database || "MinIO";
        setDatabaseName((STORAGE_DATABASE_OPTIONS.includes(db) ? db : "MinIO") as any);
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
    setPreviewContent('');
    setPreviewType('text');
    setIsEditing(false);
    setEditedContent('');
    setPreviewFileIndex(-1);
  }, [selectedNode]);

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

  const handleDatabaseChange = (val: (typeof STORAGE_DATABASE_OPTIONS)[number]) => {
    setDatabaseName(val);
    pushNodeUpdate({ database: val });
  };

  const handleFileUpload = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (!typeHasFiles(nodeType)) return;
    if (e.target.files) {
      const newFiles = Array.from(e.target.files);
      const updatedFiles = [...files, ...newFiles];
      setFiles(updatedFiles);
      pushNodeUpdate({ files: updatedFiles }); // has_files derived internally + updates Neo4j
    }
  };

  const removeFile = (index: number) => {
    const updatedFiles = files.filter((_, i) => i !== index);
    setFiles(updatedFiles);
    pushNodeUpdate({ files: updatedFiles }); // has_files derived internally + updates Neo4j
  };

  const viewFile = async (file: File, index: number) => {
    setPreviewFile(file);
    setPreviewFileIndex(index);
    setIsEditing(false);

    if (file.type.startsWith('image/')) {
      setPreviewType('image');
      setPreviewContent(URL.createObjectURL(file));
      setEditedContent('');
    } else if (
      file.type.startsWith('text/') ||
      file.name.endsWith('.json') ||
      file.name.endsWith('.xml') ||
      file.name.endsWith('.yaml') ||
      file.name.endsWith('.yml') ||
      file.name.endsWith('.md') ||
      file.name.endsWith('.js') ||
      file.name.endsWith('.ts') ||
      file.name.endsWith('.tsx') ||
      file.name.endsWith('.jsx') ||
      file.name.endsWith('.css') ||
      file.name.endsWith('.html') ||
      file.name.endsWith('.py') ||
      file.name.endsWith('.java') ||
      file.name.endsWith('.cpp') ||
      file.name.endsWith('.c') ||
      file.name.endsWith('.h')
    ) {
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
      const info = `Binary file: ${file.name}\nSize: ${(file.size / 1024).toFixed(2)} KB\nType: ${file.type || 'Unknown'}`;
      setPreviewContent(info);
      setEditedContent('');
    }
  };

  const saveFileChanges = async () => {
    if (previewFileIndex === -1 || !previewFile || previewType !== 'text') return;

    const blob = new Blob([editedContent], { type: previewFile.type });
    const newFile = new File([blob], previewFile.name, {
      type: previewFile.type,
      lastModified: Date.now()
    });

    const updatedFiles = [...files];
    updatedFiles[previewFileIndex] = newFile;
    setFiles(updatedFiles);
    pushNodeUpdate({ files: updatedFiles }); // has_files derived internally + updates Neo4j

    setPreviewContent(editedContent);
    setPreviewFile(newFile);
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

  const getTypeIcon = (type: string) => {
    switch (type) {
      case 'system': return <Brain className="w-4 h-4" />;
      case 'input': return <FileText className="w-4 h-4" />;
      case 'output': return <MessageCircle className="w-4 h-4" />;
      case 'action': return <Zap className="w-4 h-4" />;
      case 'storage': return <Database className="w-4 h-4" />;
      case 'api': return <Database className="w-4 h-4" />;
      case 'config': return <Settings className="w-4 h-4" />;
      case 'clipboard': return <Clipboard className="w-4 h-4" />;
      case 'custom': return <PlusCircle className="w-4 h-4" />;
      default: return null;
    }
  };

  const getTypeColor = (type: string) => {
    switch (type) {
      case 'system': return 'bg-purple-500/20 text-purple-300 border-purple-500/30';
      case 'input': return 'bg-blue-500/20 text-blue-300 border-blue-500/30';
      case 'output': return 'bg-green-500/20 text-green-300 border-green-500/30';
      case 'action': return 'bg-amber-500/20 text-amber-300 border-amber-500/30';
      case 'api': return 'bg-rose-500/20 text-rose-300 border-rose-500/30';
      case 'storage': return 'bg-emerald-500/20 text-emerald-300 border-emerald-500/30';
      case 'config': return 'bg-sky-500/20 text-sky-300 border-sky-500/30';
      case 'clipboard': return 'bg-teal-500/20 text-teal-300 border-teal-500/30';
      case 'custom': return 'bg-violet-500/20 text-violet-300 border-violet-500/30';
      default: return 'bg-gray-500/20 text-gray-300 border-gray-500/30';
    }
  };

  return (
    <div className="w-80 border-l border-border bg-card flex flex-col h-full">
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
                    onChange={(e) => handleDatabaseChange(e.target.value as any)}
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
                          <span className="text-xs truncate flex-1">{file.name}</span>
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
                              onClick={() => removeFile(index)}
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

      {/* File Preview Dialog */}
      <Dialog open={!!previewFile} onOpenChange={() => setPreviewFile(null)}>
        <DialogContent className="max-w-4xl max-h-[80vh] overflow-hidden">
          <DialogHeader>
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <DialogTitle>{previewFile?.name}</DialogTitle>
                <Badge variant="outline" className="text-xs">
                  {previewType === 'image' ? 'Image' : previewType === 'text' ? 'Text' : 'Binary'}
                </Badge>
              </div>
              <div className="flex items-center gap-2">
                {previewType === 'text' && !isEditing && (
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => setIsEditing(true)}
                  >
                    <Edit className="w-4 h-4 mr-1" />
                    Edit
                  </Button>
                )}
                {isEditing && (
                  <>
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => {
                        setIsEditing(false);
                        setEditedContent(previewContent);
                      }}
                    >
                      Cancel
                    </Button>
                    <Button
                      variant="default"
                      size="sm"
                      onClick={saveFileChanges}
                    >
                      <Save className="w-4 h-4 mr-1" />
                      Save Changes
                    </Button>
                  </>
                )}
              </div>
            </div>
          </DialogHeader>

          <div className="flex-1 overflow-auto">
            {previewType === 'image' ? (
              <div className="flex justify-center p-4">
                <img
                  src={previewContent}
                  alt={previewFile?.name}
                  className="max-w-full max-h-[60vh] object-contain rounded-lg border"
                />
              </div>
            ) : isEditing && previewType === 'text' ? (
              <Textarea
                value={editedContent}
                onChange={(e) => setEditedContent(e.target.value)}
                className="min-h-[400px] font-mono text-sm resize-none"
                placeholder="Edit your file content here..."
              />
            ) : (
              <pre className="whitespace-pre-wrap text-sm font-mono bg-muted/50 p-4 rounded">
                {previewContent}
              </pre>
            )}
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
