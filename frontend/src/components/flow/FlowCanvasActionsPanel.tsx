import React from 'react';
import { Panel } from 'reactflow';
import { Download, Save, Trash2, Upload } from 'lucide-react';
import { Button } from '@/components/ui/button';

type FlowCanvasActionsPanelProps = {
  fileInputRef: React.RefObject<HTMLInputElement>;
  onSave: () => void;
  onExportJson: () => void;
  onExportYaml: () => void;
  onImportClick: () => void;
  onImport: (event: React.ChangeEvent<HTMLInputElement>) => void;
  onClear: () => void;
};

export const FlowCanvasActionsPanel = ({
  fileInputRef,
  onSave,
  onExportJson,
  onExportYaml,
  onImportClick,
  onImport,
  onClear,
}: FlowCanvasActionsPanelProps) => (
  <Panel position="top-center" className="mt-2 max-w-[calc(100vw-2rem)]">
    <div className="bg-card/90 backdrop-blur-sm border border-border rounded-lg py-1.5 px-3 text-xs flex flex-wrap items-center justify-center gap-2">
      <Button size="sm" variant="outline" onClick={onSave} className="flex items-center gap-1 h-7">
        <Save className="h-3.5 w-3.5" />
        Save
      </Button>
      <Button size="sm" variant="outline" onClick={onExportJson} className="flex items-center gap-1 h-7">
        <Download className="h-3.5 w-3.5" />
        JSON
      </Button>
      <Button size="sm" variant="outline" onClick={onExportYaml} className="flex items-center gap-1 h-7">
        <Download className="h-3.5 w-3.5" />
        YAML
      </Button>
      <Button
        size="sm"
        variant="outline"
        className="flex items-center gap-1 h-7"
        onClick={onImportClick}
      >
        <Upload className="h-3.5 w-3.5" />
        Import
      </Button>
      <input
        ref={fileInputRef}
        type="file"
        accept=".json"
        className="hidden"
        onChange={onImport}
      />
      <Button
        size="sm"
        variant="destructive"
        onClick={onClear}
        className="flex items-center gap-1 h-7 bg-red-600 hover:bg-red-700 text-white"
      >
        <Trash2 className="h-3.5 w-3.5" />
        Clear
      </Button>
    </div>
  </Panel>
);
