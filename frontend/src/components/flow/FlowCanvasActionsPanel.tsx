import React from 'react';
import { Panel } from 'reactflow';
import { AlertTriangle, CircleDot, Download, Redo2, Save, ShieldCheck, Trash2, Undo2, Upload, Wand2 } from 'lucide-react';
import { Button } from '@/components/ui/button';

type FlowCanvasActionsPanelProps = {
  fileInputRef: React.RefObject<HTMLInputElement>;
  onSave: () => void;
  onUndo: () => void;
  onRedo: () => void;
  onExportJson: () => void;
  onImportClick: () => void;
  onImport: (event: React.ChangeEvent<HTMLInputElement>) => void;
  onGenerateScripts: () => void;
  isGeneratingScripts?: boolean;
  showPortDetails: boolean;
  onTogglePortDetails: () => void;
  validationErrors: number;
  validationWarnings: number;
  onValidationClick: () => void;
  onClear: () => void;
  canUndo: boolean;
  canRedo: boolean;
  isHistoryRestoring?: boolean;
};

export const FlowCanvasActionsPanel = ({
  fileInputRef,
  onSave,
  onUndo,
  onRedo,
  onExportJson,
  onImportClick,
  onImport,
  onGenerateScripts,
  isGeneratingScripts,
  showPortDetails,
  onTogglePortDetails,
  validationErrors,
  validationWarnings,
  onValidationClick,
  onClear,
  canUndo,
  canRedo,
  isHistoryRestoring = false,
}: FlowCanvasActionsPanelProps) => (
  <Panel position="top-center" className="mt-2 max-w-[calc(100vw-1rem)]">
    <div className="flex flex-nowrap items-center gap-1 rounded-xl border border-border/80 bg-card/85 p-1.5 text-xs shadow-xl shadow-black/10 backdrop-blur-md">
      <Button size="sm" variant="outline" onClick={onSave} className="flex h-7 items-center gap-1 px-2.5">
        <Save className="h-3.5 w-3.5" />
        Save
      </Button>
      <Button
        size="sm"
        variant="outline"
        className={validationErrors > 0
          ? "flex h-7 items-center gap-1 border-red-500/60 px-2 text-red-500 hover:bg-red-500/10 hover:text-red-500"
          : validationWarnings > 0
            ? "flex h-7 items-center gap-1 border-amber-500/60 px-2 text-amber-500 hover:bg-amber-500/10 hover:text-amber-500"
            : "flex h-7 items-center gap-1 border-emerald-500/50 px-2 text-emerald-500 hover:bg-emerald-500/10 hover:text-emerald-500"}
        onClick={onValidationClick}
        title="Open pipeline validation"
      >
        {validationErrors > 0 || validationWarnings > 0
          ? <AlertTriangle className="h-3.5 w-3.5" />
          : <ShieldCheck className="h-3.5 w-3.5" />}
        {validationErrors > 0
          ? `${validationErrors} error${validationErrors === 1 ? "" : "s"}${validationWarnings > 0 ? ` · ${validationWarnings} warning${validationWarnings === 1 ? "" : "s"}` : ""}`
          : validationWarnings > 0
            ? `${validationWarnings} warning${validationWarnings === 1 ? "" : "s"}`
            : "Valid"}
      </Button>
      <Button
        size="sm"
        variant="ghost"
        onClick={onUndo}
        disabled={!canUndo || isHistoryRestoring}
        title="Undo graph change"
        aria-label="Undo graph change"
        className="h-7 w-7 p-0"
      >
        <Undo2 className="h-3.5 w-3.5" />
      </Button>
      <Button
        size="sm"
        variant="ghost"
        onClick={onRedo}
        disabled={!canRedo || isHistoryRestoring}
        title="Redo graph change"
        aria-label="Redo graph change"
        className="h-7 w-7 p-0"
      >
        <Redo2 className="h-3.5 w-3.5" />
      </Button>
      <div className="mx-0.5 h-5 w-px bg-border" />
      <Button size="sm" variant="ghost" onClick={onExportJson} className="flex h-7 items-center gap-1 px-2" title="Export project JSON">
        <Download className="h-3.5 w-3.5" />
        JSON
      </Button>
      <Button
        size="sm"
        variant="ghost"
        className="flex h-7 items-center gap-1 px-2"
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
        variant="ghost"
        className="flex h-7 items-center gap-1 px-2"
        onClick={onGenerateScripts}
        disabled={isGeneratingScripts}
      >
        <Wand2 className="h-3.5 w-3.5" />
        {isGeneratingScripts ? "Generating" : "Scripts"}
      </Button>
      <Button
        size="sm"
        variant={showPortDetails ? "secondary" : "outline"}
        className="flex h-7 items-center gap-1 px-2"
        onClick={onTogglePortDetails}
        title={showPortDetails ? "Switch to Compact mode" : "Switch to Advanced mode and show ports, contracts, and validation"}
        aria-pressed={showPortDetails}
      >
        <CircleDot className="h-3.5 w-3.5" />
        {showPortDetails ? "Advanced" : "Compact"}
      </Button>
      <Button
        size="sm"
        variant="ghost"
        onClick={onClear}
        title="Clear canvas"
        aria-label="Clear canvas"
        className="h-7 w-7 p-0 text-red-400 hover:bg-red-500/10 hover:text-red-300"
      >
        <Trash2 className="h-3.5 w-3.5" />
      </Button>
    </div>
  </Panel>
);
