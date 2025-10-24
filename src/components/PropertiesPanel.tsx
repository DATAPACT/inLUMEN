import React, { useState, useEffect, useRef } from 'react';
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Brain, MessageCircle, FileText, Zap, Database, Settings, Clipboard, PlusCircle, Upload, X, Trash2, Eye, Edit, Save } from 'lucide-react';

interface PropertiesPanelProps {
  selectedNode: any;
  onNodeUpdate: (id: string, data: any) => void;
  onRemoveNode?: (nodeId: string) => void;
}

export function PropertiesPanel({ selectedNode, onNodeUpdate, onRemoveNode }: PropertiesPanelProps) {
  const [label, setLabel] = useState('');
  const [description, setDescription] = useState('');
  const [content, setContent] = useState('');
  const [files, setFiles] = useState<File[]>([]);
  const [previewFile, setPreviewFile] = useState<File | null>(null);
  const [previewContent, setPreviewContent] = useState('');
  const [previewType, setPreviewType] = useState<'text' | 'image' | 'binary'>('text');
  const [isEditing, setIsEditing] = useState(false);
  const [editedContent, setEditedContent] = useState('');
  const [previewFileIndex, setPreviewFileIndex] = useState<number>(-1);
  const fileInputRef = useRef<HTMLInputElement>(null);
  
  useEffect(() => {
    if (selectedNode) {
      setLabel(selectedNode.data.label || '');
      setDescription(selectedNode.data.description || '');
      setContent(selectedNode.data.content || '');
      setFiles(selectedNode.data.files || []);
    } else {
      setLabel('');
      setDescription('');
      setContent('');
      setFiles([]);
    }
    setPreviewFile(null);
    setPreviewContent('');
    setPreviewType('text');
    setIsEditing(false);
    setEditedContent('');
    setPreviewFileIndex(-1);
  }, [selectedNode]);
  
  const handleLabelChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    setLabel(e.target.value);
    if (selectedNode) {
      onNodeUpdate(selectedNode.id, { ...selectedNode.data, label: e.target.value });
    }
  };
  
  const handleDescriptionChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    setDescription(e.target.value);
    if (selectedNode) {
      onNodeUpdate(selectedNode.id, { ...selectedNode.data, description: e.target.value });
    }
  };

  const handleContentChange = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    setContent(e.target.value);
    if (selectedNode) {
      onNodeUpdate(selectedNode.id, { ...selectedNode.data, content: e.target.value });
    }
  };
  
  const handleFileUpload = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files) {
      const newFiles = Array.from(e.target.files);
      const updatedFiles = [...files, ...newFiles];
      setFiles(updatedFiles);
      if (selectedNode) {
        onNodeUpdate(selectedNode.id, { ...selectedNode.data, files: updatedFiles });
      }
    }
  };

  const removeFile = (index: number) => {
    const updatedFiles = files.filter((_, i) => i !== index);
    setFiles(updatedFiles);
    if (selectedNode) {
      onNodeUpdate(selectedNode.id, { ...selectedNode.data, files: updatedFiles });
    }
  };

  const viewFile = async (file: File, index: number) => {
    setPreviewFile(file);
    setPreviewFileIndex(index);
    setIsEditing(false);
    
    // Check if it's an image
    if (file.type.startsWith('image/')) {
      setPreviewType('image');
      setPreviewContent(URL.createObjectURL(file));
      setEditedContent('');
    }
    // Check if it's a text file
    else if (file.type.startsWith('text/') || 
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
             file.name.endsWith('.h')) {
      setPreviewType('text');
      try {
        const content = await file.text();
        setPreviewContent(content);
        setEditedContent(content);
      } catch (error) {
        setPreviewContent('Error reading file content');
        setEditedContent('');
      }
    }
    // Binary file
    else {
      setPreviewType('binary');
      const info = `Binary file: ${file.name}\nSize: ${(file.size / 1024).toFixed(2)} KB\nType: ${file.type || 'Unknown'}`;
      setPreviewContent(info);
      setEditedContent('');
    }
  };

  const saveFileChanges = async () => {
    if (previewFileIndex === -1 || !previewFile || previewType !== 'text') return;
    
    // Create a new File object with the edited content
    const blob = new Blob([editedContent], { type: previewFile.type });
    const newFile = new File([blob], previewFile.name, { 
      type: previewFile.type,
      lastModified: Date.now()
    });
    
    // Update the files array
    const updatedFiles = [...files];
    updatedFiles[previewFileIndex] = newFile;
    setFiles(updatedFiles);
    
    // Update the node data
    if (selectedNode) {
      onNodeUpdate(selectedNode.id, { ...selectedNode.data, files: updatedFiles });
    }
    
    // Update preview content and exit edit mode
    setPreviewContent(editedContent);
    setPreviewFile(newFile);
    setIsEditing(false);
  };

  const getTypeIcon = (type: string) => {
    switch (type) {
      case 'system': return <Brain className="w-4 h-4" />;
      case 'input': return <FileText className="w-4 h-4" />;
      case 'output': return <MessageCircle className="w-4 h-4" />;
      case 'action': return <Zap className="w-4 h-4" />;
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
          {selectedNode && onRemoveNode && (
            <Button
              onClick={() => onRemoveNode(selectedNode.id)}
              variant="destructive"
              size="sm"
              className="ml-2"
            >
              <Trash2 className="w-4 h-4 mr-1" />
              Remove
            </Button>
          )}
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
                    getTypeColor(selectedNode.data.type)
                  )}
                >
                  {getTypeIcon(selectedNode.data.type)}
                  {selectedNode.data.type}
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
            
            <div className="space-y-2">
              <Label htmlFor="node-content" className="text-sm">Content</Label>
              <Textarea
                id="node-content"
                value={content}
                onChange={handleContentChange}
                placeholder={`Enter ${selectedNode.data.type} content...`}
                className="h-32 resize-none"
              />
              <p className="text-xs text-muted-foreground mt-1">
                {selectedNode.data.type === 'system' ? 
                  'Define AI behavior, personality, and instruction set.' : 
                  selectedNode.data.type === 'input' ?
                  'Template for processing user inputs.' :
                  selectedNode.data.type === 'output' ?
                  'Template for generating AI responses.' :
                  selectedNode.data.type === 'custom' ?
                  'Add your custom node content here.' :
                  'Content for this node.'}
              </p>
            </div>
            
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