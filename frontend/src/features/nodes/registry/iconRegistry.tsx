import React from "react";
import {
  Brain,
  Boxes,
  Clipboard,
  Database,
  FileOutput,
  FileText,
  GitCompare,
  GitBranch,
  Info,
  Key,
  ListPlus,
  MessageCircle,
  Network,
  PlusCircle,
  ScanSearch,
  Settings,
  Table,
  Tags,
  WandSparkles,
  Zap,
} from "lucide-react";

const ICONS: Record<string, React.ComponentType<{ className?: string }>> = {
  brain: Brain,
  boxes: Boxes,
  clipboard: Clipboard,
  database: Database,
  "file-output": FileOutput,
  "file-text": FileText,
  "git-compare": GitCompare,
  "git-branch": GitBranch,
  info: Info,
  key: Key,
  "list-plus": ListPlus,
  "message-circle": MessageCircle,
  network: Network,
  "plus-circle": PlusCircle,
  "scan-search": ScanSearch,
  settings: Settings,
  table: Table,
  tags: Tags,
  "wand-sparkles": WandSparkles,
  zap: Zap,
};

const COLOR_CLASSES: Record<string, string> = {
  amber: "bg-amber-500/20 text-amber-300 border-amber-500/30",
  blue: "bg-blue-500/20 text-blue-300 border-blue-500/30",
  cyan: "bg-cyan-500/20 text-cyan-300 border-cyan-500/30",
  emerald: "bg-emerald-500/20 text-emerald-300 border-emerald-500/30",
  fuchsia: "bg-fuchsia-500/20 text-fuchsia-300 border-fuchsia-500/30",
  indigo: "bg-indigo-500/20 text-indigo-300 border-indigo-500/30",
  lime: "bg-lime-500/20 text-lime-300 border-lime-500/30",
  orange: "bg-orange-500/20 text-orange-300 border-orange-500/30",
  purple: "bg-purple-500/20 text-purple-300 border-purple-500/30",
  rose: "bg-rose-500/20 text-rose-300 border-rose-500/30",
  sky: "bg-sky-500/20 text-sky-300 border-sky-500/30",
  teal: "bg-teal-500/20 text-teal-300 border-teal-500/30",
  violet: "bg-violet-500/20 text-violet-300 border-violet-500/30",
  yellow: "bg-yellow-500/20 text-yellow-300 border-yellow-500/30",
};

export const getNodeDefinitionIcon = (iconName: string, className = "w-4 h-4") => {
  const Icon = ICONS[iconName] ?? Info;
  return <Icon className={className} />;
};

export const getNodeDefinitionColorClasses = (colorName: string) =>
  COLOR_CLASSES[colorName] ?? "bg-gray-500/20 text-gray-300 border-gray-500/30";
