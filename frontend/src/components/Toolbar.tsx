import React from 'react';
import { Button } from "@/components/ui/button";
import {
  PlayCircle,
  HelpCircle,
  Settings,
  Sun,
  Moon,
  PanelLeft,
  SlidersHorizontal,
  MessageSquare
} from 'lucide-react';
import { Separator } from "@/components/ui/separator";
import { cn } from "@/lib/utils";
import inlumenLogo from "@/assets/inlumen-logo.svg";

interface ToolbarProps {
  className?: string;
  onRunFlow: () => void;
  isLightMode: boolean;
  onToggleLightMode: () => void;
  isLibraryOpen: boolean;
  isInspectorOpen: boolean;
  isChatOpen: boolean;
  onToggleLibrary: () => void;
  onToggleInspector: () => void;
  onToggleChat: () => void;
  onOpenHelp: () => void;
  onOpenSettings: () => void;
}

export function Toolbar({
  className,
  onRunFlow,
  isLightMode,
  onToggleLightMode,
  isLibraryOpen,
  isInspectorOpen,
  isChatOpen,
  onToggleLibrary,
  onToggleInspector,
  onToggleChat,
  onOpenHelp,
  onOpenSettings
}: ToolbarProps) {
  const panelButtonClass = (isActive: boolean) =>
    cn(
      "h-8 rounded-lg px-2.5 text-xs",
      isActive
        ? "border border-emerald-400/40 bg-emerald-500/15 text-emerald-500 hover:bg-emerald-500/20"
        : "border border-transparent text-muted-foreground"
    );

  return (
    <div className={cn("h-14 border-b border-border bg-card/95 flex items-center px-3 gap-2 shadow-sm backdrop-blur supports-[backdrop-filter]:bg-card/80", className)}>
      <div className="flex min-w-0 items-center gap-2 pr-2">
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

      <div className="flex items-center gap-1 rounded-xl border border-border bg-background/60 p-1">
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
      </div>
      
      <div className="flex-1" />

      <Button variant="outline" size="sm" className="hidden h-8 text-xs md:inline-flex" onClick={onRunFlow}>
        <PlayCircle className="h-3.5 w-3.5" />
        Run
      </Button>

      <Button variant="ghost" size="sm" className="h-8 text-xs" onClick={onOpenHelp}>
        <HelpCircle className="h-3.5 w-3.5 mr-1" />
        <span className="hidden sm:inline">Help</span>
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
  );
}
