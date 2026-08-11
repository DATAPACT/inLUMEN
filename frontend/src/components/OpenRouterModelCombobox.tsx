import React from "react";
import {
  ArrowDownToLine,
  ArrowUpFromLine,
  Check,
  ChevronsUpDown,
  LoaderCircle,
  Search,
  Sparkles,
  Wrench,
  Zap,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { cn } from "@/lib/utils";
import {
  formatContextLength,
  formatTokenPrice,
  type OpenRouterModel,
} from "@/services/openRouterModels";

interface OpenRouterModelComboboxProps {
  value: string;
  models: OpenRouterModel[];
  isLoading: boolean;
  catalogError?: string;
  placeholder: string;
  onChange: (modelId: string, model?: OpenRouterModel) => void;
}

const ModelPrice = ({
  icon: Icon,
  label,
  price,
}: {
  icon: React.ComponentType<{ className?: string }>;
  label: string;
  price: number | null;
}) => (
  <div className="flex min-w-0 flex-1 items-center gap-2 rounded-lg border border-border/70 bg-background/70 px-3 py-2">
    <span className="rounded-md bg-primary/10 p-1.5 text-primary">
      <Icon className="h-3.5 w-3.5" />
    </span>
    <div className="min-w-0">
      <div className="text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
        {label}
      </div>
      <div className="truncate text-sm font-semibold tabular-nums">
        {formatTokenPrice(price)}
        <span className="ml-1 text-[10px] font-normal text-muted-foreground">/ 1M</span>
      </div>
    </div>
  </div>
);

export function OpenRouterModelCombobox({
  value,
  models,
  isLoading,
  catalogError,
  placeholder,
  onChange,
}: OpenRouterModelComboboxProps) {
  const [isOpen, setIsOpen] = React.useState(false);
  const [search, setSearch] = React.useState("");
  const selectedModel = React.useMemo(
    () => models.find((model) => model.id.toLowerCase() === value.trim().toLowerCase()),
    [models, value],
  );
  const cerebrasModels = React.useMemo(
    () => models.filter((model) => model.cerebrasAvailable),
    [models],
  );
  const otherModels = React.useMemo(
    () => models.filter((model) => !model.cerebrasAvailable),
    [models],
  );
  const normalizedSearch = search.trim();
  const hasExactMatch = models.some(
    (model) => model.id.toLowerCase() === normalizedSearch.toLowerCase(),
  );

  const selectModel = (modelId: string, model?: OpenRouterModel) => {
    onChange(modelId, model);
    setIsOpen(false);
    setSearch("");
  };

  const renderModel = (model: OpenRouterModel) => (
    <CommandItem
      key={model.id}
      value={`${model.id} ${model.name}`}
      onSelect={() => selectModel(model.id, model)}
      className="gap-2 py-2.5"
    >
      <Check
        className={cn(
          "h-4 w-4 shrink-0",
          value === model.id ? "opacity-100" : "opacity-0",
        )}
      />
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-1.5">
          <span className="truncate font-medium">{model.name}</span>
          {model.cerebrasAvailable && (
            <Badge className="h-5 shrink-0 border-amber-400/30 bg-amber-400/10 px-1.5 text-[10px] text-amber-500 hover:bg-amber-400/10">
              <Zap className="mr-0.5 h-3 w-3" /> Cerebras
            </Badge>
          )}
        </div>
        <div className="mt-0.5 flex items-center gap-2 truncate text-[11px] text-muted-foreground">
          <span className="truncate">{model.id}</span>
          <span aria-hidden="true">•</span>
          <span className="shrink-0">{formatTokenPrice(model.promptPrice)} in</span>
          <span aria-hidden="true">/</span>
          <span className="shrink-0">{formatTokenPrice(model.completionPrice)} out</span>
        </div>
      </div>
    </CommandItem>
  );

  return (
    <div className="space-y-2.5">
      <Popover
        open={isOpen}
        onOpenChange={(open) => {
          setIsOpen(open);
          if (!open) setSearch("");
        }}
      >
        <PopoverTrigger asChild>
          <Button
            type="button"
            variant="outline"
            role="combobox"
            aria-expanded={isOpen}
            className="h-auto min-h-10 w-full justify-between px-3 py-2 text-left font-normal hover:bg-muted/60 hover:text-foreground"
          >
            <span className="flex min-w-0 items-center gap-2">
              <Search className="h-4 w-4 shrink-0 text-muted-foreground" />
              <span className={cn("truncate", !value && "text-muted-foreground")}>
                {selectedModel?.name || value || placeholder}
              </span>
              {selectedModel?.cerebrasAvailable && (
                <Zap className="h-3.5 w-3.5 shrink-0 text-amber-500" />
              )}
            </span>
            <ChevronsUpDown className="ml-2 h-4 w-4 shrink-0 opacity-50" />
          </Button>
        </PopoverTrigger>
        <PopoverContent
          align="start"
          className="w-[calc(100vw-2rem)] p-0 sm:w-[560px]"
        >
          <Command>
            <CommandInput
              placeholder="Search model name or paste a model ID…"
              value={search}
              onValueChange={setSearch}
            />
            <CommandList className="max-h-[240px]">
              {isLoading && (
                <div className="flex items-center justify-center gap-2 py-8 text-sm text-muted-foreground">
                  <LoaderCircle className="h-4 w-4 animate-spin" /> Loading OpenRouter models…
                </div>
              )}
              {!isLoading && cerebrasModels.length > 0 && (
                <CommandGroup heading="Cerebras accelerated">
                  {cerebrasModels.map(renderModel)}
                </CommandGroup>
              )}
              {!isLoading && otherModels.length > 0 && (
                <CommandGroup heading="All OpenRouter models">
                  {otherModels.map(renderModel)}
                </CommandGroup>
              )}
              {!isLoading && normalizedSearch && !hasExactMatch && (
                <CommandGroup heading="Use custom model ID">
                  <CommandItem
                    value={`custom ${normalizedSearch}`}
                    onSelect={() => selectModel(normalizedSearch)}
                    className="gap-2 py-2.5"
                  >
                    <Sparkles className="h-4 w-4 text-primary" />
                    <div className="min-w-0">
                      <div className="font-medium">Use “{normalizedSearch}”</div>
                      <div className="text-[11px] text-muted-foreground">
                        Save the exact OpenRouter model ID you entered
                      </div>
                    </div>
                  </CommandItem>
                </CommandGroup>
              )}
              {!isLoading && models.length === 0 && (
                <CommandEmpty>
                  {catalogError || "No matching model found. Type a model ID to use it directly."}
                </CommandEmpty>
              )}
            </CommandList>
          </Command>
        </PopoverContent>
      </Popover>

      {selectedModel ? (
        <div className="rounded-xl border border-border/70 bg-gradient-to-br from-muted/70 to-muted/30 p-3">
          <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
            <div className="flex items-center gap-2 text-xs text-muted-foreground">
              <span>{formatContextLength(selectedModel.contextLength)}</span>
              {selectedModel.supportsTools && (
                <span className="flex items-center gap-1">
                  <Wrench className="h-3 w-3" /> Tools
                </span>
              )}
            </div>
            {selectedModel.cerebrasAvailable ? (
              <Badge className="border-amber-400/30 bg-amber-400/10 text-amber-500 hover:bg-amber-400/10">
                <Zap className="mr-1 h-3 w-3" /> Cerebras locked
              </Badge>
            ) : (
              <Badge variant="outline" className="font-normal text-muted-foreground">
                OpenRouter auto-routing
              </Badge>
            )}
          </div>
          <div className="flex gap-2">
            <ModelPrice icon={ArrowDownToLine} label="Input tokens" price={selectedModel.promptPrice} />
            <ModelPrice icon={ArrowUpFromLine} label="Output tokens" price={selectedModel.completionPrice} />
          </div>
          <p className="mt-2 text-[10px] leading-relaxed text-muted-foreground">
            OpenRouter list price per million tokens. BYOK billing and fees may differ.
          </p>
        </div>
      ) : catalogError ? (
        <p className="text-xs text-amber-500">
          Live suggestions are unavailable. You can still enter an exact model ID.
        </p>
      ) : null}
    </div>
  );
}
