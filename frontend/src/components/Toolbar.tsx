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
  FileText,
  Braces,
  ChevronDown
} from 'lucide-react';
import { Separator } from "@/components/ui/separator";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
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
  onDownloadProvO: () => void;
  onOpenHelp: () => void;
  onOpenSettings: () => void;
  isClearingAll?: boolean;
  isGeneratingProvenanceReport?: boolean;
  isDownloadingProvO?: boolean;
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
  onDownloadProvO,
  onOpenHelp,
  onOpenSettings,
  isClearingAll = false,
  isGeneratingProvenanceReport = false,
  isDownloadingProvO = false
}: ToolbarProps) {
  const [isClearWarningOpen, setIsClearWarningOpen] = React.useState(false);
  const panelButtonClass = (isActive: boolean) =>
    cn(
      "h-8 rounded-lg px-2.5 text-xs",
      isActive
        ? "border border-emerald-400/40 bg-emerald-500/15 text-[hsl(var(--success-foreground))] hover:bg-emerald-500/20"
        : "border border-transparent text-muted-foreground"
    );
  const currentVersionName = activeVersionName?.trim() || "Main";

  return (
    <div className={cn("relative h-14 border-b border-border bg-card/95 flex items-center px-3 gap-2 shadow-sm backdrop-blur supports-[backdrop-filter]:bg-card/80", className)}>
      <div className="flex shrink-0 min-w-0 items-center gap-2 pr-2">
        <img src={inlumenLogo} alt="inLUMEN" className="h-8 w-8 shrink-0 rounded-lg" />
        <div className="hidden min-w-0 flex-col justify-center sm:flex">
          <h1 className="truncate text-sm font-semibold tracking-[0.18em]">
            <span className="font-mono text-[hsl(var(--brand-foreground))] drop-shadow-[0_0_4px_hsl(var(--brand-foreground)/0.25)]">in</span>
            <span className="ml-1 text-foreground">LUMEN</span>
          </h1>
          <p className="truncate text-[11px] text-muted-foreground">
            Visual AI pipeline design workspace
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
        <AlertDialog open={isClearWarningOpen} onOpenChange={setIsClearWarningOpen}>
          <Button
            variant="ghost"
            size="sm"
            className="h-8 text-xs text-destructive hover:text-destructive"
            onClick={() => setIsClearWarningOpen(true)}
            disabled={isClearingAll}
            title="Permanently clear the entire workspace"
          >
            <Trash2 className="h-3.5 w-3.5 mr-1" />
            <span className="hidden sm:inline">{isClearingAll ? "Clearing..." : "Clear all"}</span>
          </Button>
          <AlertDialogContent>
            <AlertDialogHeader>
              <AlertDialogTitle>Clear the entire workspace?</AlertDialogTitle>
              <AlertDialogDescription asChild>
                <div className="space-y-3">
                  <p>This action cannot be undone. It will permanently:</p>
                  <ul className="list-disc space-y-1 pl-5">
                    <li>empty the Main pipeline canvas,</li>
                    <li>delete every saved and reusable pipeline version,</li>
                    <li>remove all pipeline files and generated packages,</li>
                    <li>clear the current chat session, and</li>
                    <li>delete all Neo4j graph data and provenance.</li>
                  </ul>
                </div>
              </AlertDialogDescription>
            </AlertDialogHeader>
            <AlertDialogFooter>
              <AlertDialogCancel>Keep workspace</AlertDialogCancel>
              <AlertDialogAction
                className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
                onClick={onClearAll}
              >
                Clear workspace
              </AlertDialogAction>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>

        <Button variant="ghost" size="sm" className="h-8 text-xs" onClick={onOpenHelp}>
          <HelpCircle className="h-3.5 w-3.5 mr-1" />
          <span className="hidden sm:inline">Help</span>
        </Button>

        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button
              variant="ghost"
              size="sm"
              className="h-8 text-xs"
              disabled={isGeneratingProvenanceReport || isDownloadingProvO}
              title="Download provenance"
            >
              <FileText className="h-3.5 w-3.5 mr-1" />
              <span className="hidden sm:inline">
                {isGeneratingProvenanceReport
                  ? "Generating..."
                  : isDownloadingProvO
                    ? "Exporting..."
                    : "Provenance"}
              </span>
              <ChevronDown className="ml-1 h-3 w-3" />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <DropdownMenuItem onSelect={onGenerateProvenanceReport}>
              <FileText className="mr-2 h-4 w-4" />
              PDF report
            </DropdownMenuItem>
            <DropdownMenuItem onSelect={onDownloadProvO}>
              <Braces className="mr-2 h-4 w-4" />
              PROV-O (JSON-LD)
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>

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
