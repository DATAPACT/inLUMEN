import React from 'react';
import { Button } from "@/components/ui/button";
import {
  HelpCircle,
  Settings,
  Sun,
  Moon,
  PanelLeft,
  SlidersHorizontal,
  MessageSquare,
  History,
  Trash2,
  FileText
} from 'lucide-react';
import { Separator } from "@/components/ui/separator";
import { cn } from "@/lib/utils";
import inlumenLogo from "@/assets/inlumen-logo.svg";

interface ToolbarProps {
  className?: string;
  isLightMode: boolean;
  activeVersionName?: string;
  onToggleLightMode: () => void;
  isLibraryOpen: boolean;
  isInspectorOpen: boolean;
  isChatOpen: boolean;
  isVersionsOpen: boolean;
  onToggleLibrary: () => void;
  onToggleInspector: () => void;
  onToggleChat: () => void;
  onToggleVersions: () => void;
  onClearAll: () => void;
  onGenerateProvenanceReport: () => void;
  onOpenHelp: () => void;
  onOpenSettings: () => void;
  isClearingAll?: boolean;
  isGeneratingProvenanceReport?: boolean;
}

export function Toolbar({
  className,
  isLightMode,
  activeVersionName,
  onToggleLightMode,
  isLibraryOpen,
  isInspectorOpen,
  isChatOpen,
  isVersionsOpen,
  onToggleLibrary,
  onToggleInspector,
  onToggleChat,
  onToggleVersions,
  onClearAll,
  onGenerateProvenanceReport,
  onOpenHelp,
  onOpenSettings,
  isClearingAll = false,
  isGeneratingProvenanceReport = false
}: ToolbarProps) {
  const panelButtonClass = (isActive: boolean) =>
    cn(
      "h-8 rounded-lg px-2.5 text-xs",
      isActive
        ? "border border-emerald-400/40 bg-emerald-500/15 text-emerald-500 hover:bg-emerald-500/20"
        : "border border-transparent text-muted-foreground"
    );
  const currentVersionName = activeVersionName?.trim() || "Main";

  return (
    <div className={cn("relative h-14 border-b border-border bg-card/95 flex items-center px-3 gap-2 shadow-sm backdrop-blur supports-[backdrop-filter]:bg-card/80", className)}>
      <div className="flex shrink-0 min-w-0 items-center gap-2 pr-2">
        <img src={inlumenLogo} alt="inLUMEN" className="h-8 w-8 shrink-0 rounded-lg" />
        <div className="hidden min-w-0 flex-col justify-center sm:flex">
          <h1 className="truncate text-sm font-semibold tracking-[0.18em]">
            <span className="font-mono text-[#9EFF6B] drop-shadow-[0_0_4px_rgba(158,255,107,0.35)]">in</span>
            <span className="ml-1 text-foreground">LUMEN</span>
          </h1>
          <p className="truncate text-[11px] text-muted-foreground">
            Visual AI pipeline workspace
          </p>
        </div>
      </div>

      <Separator orientation="vertical" className="hidden h-6 sm:block" />

      <div className="pointer-events-none absolute left-1/2 top-1/2 hidden -translate-x-1/2 -translate-y-1/2 sm:flex">
        <div className="max-w-[min(40vw,20rem)] rounded-full border border-border bg-background/60 px-3 py-1 text-xs text-muted-foreground shadow-sm">
          <span className="block truncate text-foreground">{currentVersionName}</span>
        </div>
      </div>

      <div className="flex shrink-0 items-center gap-1 rounded-xl border border-border bg-background/60 p-1">
        <Button
          variant="ghost"
          size="sm"
          className={panelButtonClass(isLibraryOpen)}
          aria-pressed={isLibraryOpen}
          onClick={onToggleLibrary}
          title="Toggle node library"
        >
          <PanelLeft className="h-3.5 w-3.5" />
          <span className="hidden lg:inline">Library</span>
        </Button>
        <Button
          variant="ghost"
          size="sm"
          className={panelButtonClass(isInspectorOpen)}
          aria-pressed={isInspectorOpen}
          onClick={onToggleInspector}
          title="Toggle node inspector"
        >
          <SlidersHorizontal className="h-3.5 w-3.5" />
          <span className="hidden lg:inline">Inspector</span>
        </Button>
        <Button
          variant="ghost"
          size="sm"
          className={panelButtonClass(isChatOpen)}
          aria-pressed={isChatOpen}
          onClick={onToggleChat}
          title="Toggle pipeline chat"
        >
          <MessageSquare className="h-3.5 w-3.5" />
          <span className="hidden lg:inline">Chat</span>
        </Button>
        <Button
          variant="ghost"
          size="sm"
          className={panelButtonClass(isVersionsOpen)}
          aria-pressed={isVersionsOpen}
          onClick={onToggleVersions}
          title="Toggle saved versions"
        >
          <History className="h-3.5 w-3.5" />
          <span className="hidden lg:inline">Versions</span>
        </Button>
      </div>
      
      <div className="ml-auto flex shrink-0 items-center gap-1">
        <Button
          variant="ghost"
          size="sm"
          className="h-8 text-xs text-destructive hover:text-destructive"
          onClick={onClearAll}
          disabled={isClearingAll}
          title="Clear canvas, chat, and saved versions"
        >
          <Trash2 className="h-3.5 w-3.5 mr-1" />
          <span className="hidden sm:inline">{isClearingAll ? "Clearing..." : "Clear all"}</span>
        </Button>

        <Button variant="ghost" size="sm" className="h-8 text-xs" onClick={onOpenHelp}>
          <HelpCircle className="h-3.5 w-3.5 mr-1" />
          <span className="hidden sm:inline">Help</span>
        </Button>

        <Button
          variant="ghost"
          size="sm"
          className="h-8 text-xs"
          onClick={onGenerateProvenanceReport}
          disabled={isGeneratingProvenanceReport}
          title="Generate Provenance Report"
        >
          <FileText className="h-3.5 w-3.5 mr-1" />
          <span className="hidden sm:inline">
            {isGeneratingProvenanceReport ? "Generating..." : "Provenance"}
          </span>
        </Button>

        <Button variant="ghost" size="sm" className="h-8 text-xs" onClick={onOpenSettings}>
          <Settings className="h-3.5 w-3.5 mr-1" />
          <span className="hidden sm:inline">Settings</span>
        </Button>

        <Separator orientation="vertical" className="h-6" />

        <Button
          variant="ghost"
          size="icon"
          className="h-8 w-8"
          onClick={onToggleLightMode}
          title={isLightMode ? "Switch to dark mode" : "Switch to light mode"}
        >
          {isLightMode ? <Moon className="h-4 w-4" /> : <Sun className="h-4 w-4" />}
        </Button>
      </div>
      
    </div>
  );
}
