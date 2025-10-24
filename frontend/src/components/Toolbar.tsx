import React from 'react';
import { Button } from "@/components/ui/button";
import {
  Save,
  Undo,
  Redo,
  PlayCircle,
  PlusCircle,
  Download,
  Share2,
  HelpCircle,
  Settings,
  Sun,
  Moon
} from 'lucide-react';
import { Separator } from "@/components/ui/separator";
import { cn } from "@/lib/utils";
import inlumenLogo from "@/assets/inlumen-logo.svg";

interface ToolbarProps {
  className?: string;
  onRunFlow: () => void;
  isLightMode: boolean;
  onToggleLightMode: () => void;
}

export function Toolbar({ className, onRunFlow, isLightMode, onToggleLightMode }: ToolbarProps) {
  return (
    <div className={cn("h-14 border-b border-border flex items-center px-4 gap-2", className)}>
      <div className="p-4 border-b border-border flex flex-col justify-center">
          <h2 className="text-lg font-semibold tracking-wider">
            <span className="font-mono text-[#9EFF6B] drop-shadow-[0_0_4px_#9EFF6B]/40">
              in
            </span>
            <span className="font-orbitron text-[#E5E7EB] tracking-[0.15em] ml-1">
               LUMEN
            </span>
          </h2>
          <p className="text-xs text-muted-foreground mt-1 tracking-tight">
            AI pipeline design with visual interface and LLM support
          </p>
        </div>
      
      <div className="flex-1" />

      <Button variant="ghost" size="sm" className="text-xs h-8">
        <HelpCircle className="h-3.5 w-3.5 mr-1" />
        Help
      </Button>
      
      <Button variant="ghost" size="sm" className="text-xs h-8">
        <Settings className="h-3.5 w-3.5 mr-1" />
        Settings
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