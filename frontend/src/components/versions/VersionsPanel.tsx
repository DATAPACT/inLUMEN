import React, { useCallback, useEffect, useState } from 'react';
import { CheckCircle2, GitBranch, History, RefreshCw, Trash2 } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { ScrollArea } from '@/components/ui/scroll-area';
import { cn } from '@/lib/utils';
import {
  deletePipelineVersion,
  fetchPipelineVersions,
  type PipelineVersionSummary,
} from '@/features/flow/flowPersistence';

type VersionsPanelProps = {
  className?: string;
  refreshKey?: number;
  activeVersionUid?: string;
  isRestoring?: boolean;
  onRestoreVersion: (version: PipelineVersionSummary) => void;
  onSetMainVersion: (version: PipelineVersionSummary) => void;
};

const formatDate = (value?: string | null) => {
  if (!value) return 'Unknown';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
};

export const VersionsPanel = ({
  className,
  refreshKey = 0,
  activeVersionUid,
  isRestoring = false,
  onRestoreVersion,
  onSetMainVersion,
}: VersionsPanelProps) => {
  const [versions, setVersions] = useState<PipelineVersionSummary[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [deletingUid, setDeletingUid] = useState<string | null>(null);
  const [error, setError] = useState('');
  const savedVersionCount = versions.filter((version) => !version.is_main).length;

  const loadVersions = useCallback(async () => {
    try {
      setIsLoading(true);
      setError('');
      setVersions(await fetchPipelineVersions());
    } catch (err) {
      console.error('[VersionsPanel.tsx] Failed to load versions:', err);
      setError(err instanceof Error ? err.message : 'Failed to load versions.');
    } finally {
      setIsLoading(false);
    }
  }, []);

  const handleDeleteVersion = async (version: PipelineVersionSummary) => {
    if (version.is_main) return;
    const confirmed = window.confirm(`Delete version "${version.name}"?`);
    if (!confirmed) return;

    try {
      setDeletingUid(version.uid);
      setError('');
      await deletePipelineVersion(version.uid);
      setVersions((current) => current.filter((item) => item.uid !== version.uid));
      void loadVersions();
    } catch (err) {
      console.error('[VersionsPanel.tsx] Failed to delete version:', err);
      setError(err instanceof Error ? err.message : 'Failed to delete version.');
    } finally {
      setDeletingUid(null);
    }
  };

  const handleVersionActivate = (version: PipelineVersionSummary) => {
    if (isRestoring || deletingUid === version.uid) return;
    onRestoreVersion(version);
  };

  useEffect(() => {
    void loadVersions();
  }, [loadVersions, refreshKey]);

  return (
    <div className={cn('flex h-full w-full flex-col border-l border-border bg-card text-card-foreground', className)}>
      <div className="border-b border-border p-4">
        <div className="flex items-center justify-between gap-3">
          <div className="min-w-0">
            <h2 className="flex items-center gap-2 text-lg font-semibold">
              <History className="h-5 w-5 text-emerald-500" />
              Versions
            </h2>
            <p className="mt-1 text-xs text-muted-foreground">
              Main + {savedVersionCount} saved snapshot{savedVersionCount === 1 ? '' : 's'}
            </p>
          </div>
          <Button
            type="button"
            variant="outline"
            size="icon"
            className="h-8 w-8 shrink-0"
            onClick={() => { void loadVersions(); }}
            disabled={isLoading}
            title="Refresh versions"
          >
            <RefreshCw className={cn('h-4 w-4', isLoading && 'animate-spin')} />
          </Button>
        </div>
      </div>

      <ScrollArea className="min-h-0 flex-1">
        <div className="space-y-3 p-4">
          {error && (
            <div className="rounded-md border border-red-500/30 bg-red-500/10 p-3 text-xs text-red-400">
              {error}
            </div>
          )}

          {!error && !isLoading && versions.length === 0 && (
            <div className="rounded-md border border-dashed border-border p-4 text-sm text-muted-foreground">
              No saved versions yet.
            </div>
          )}

          {versions.map((version) => {
            const isActive = version.uid === activeVersionUid;
            return (
              <div
                key={version.uid}
                role="button"
                tabIndex={0}
                aria-disabled={isRestoring || deletingUid === version.uid}
                onClick={() => { handleVersionActivate(version); }}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' || event.key === ' ') {
                    event.preventDefault();
                    handleVersionActivate(version);
                  }
                }}
                className={cn(
                  'rounded-md border bg-muted/25 p-3 text-left transition-colors',
                  version.is_main ? 'border-emerald-500/40 bg-emerald-500/5' : 'border-border',
                  isActive && 'border-sky-400/70 ring-1 ring-sky-400/30',
                  !(isRestoring || deletingUid === version.uid) && 'cursor-pointer hover:bg-muted/40',
                  (isRestoring || deletingUid === version.uid) && 'cursor-not-allowed opacity-70',
                )}
              >
                <div className="mb-3 flex items-start justify-between gap-3">
                  <div className="min-w-0 flex-1">
                    <div className="flex min-w-0 items-start gap-2 text-sm font-medium leading-snug">
                      {version.is_main && <GitBranch className="h-3.5 w-3.5 shrink-0 text-emerald-500" />}
                      {isActive && <CheckCircle2 className="h-3.5 w-3.5 shrink-0 text-sky-400" />}
                      <span className="min-w-0 break-words">{version.name}</span>
                    </div>
                    <div className="text-xs text-muted-foreground">
                      {version.is_main ? 'Working version' : 'Saved'} {formatDate(version.updated_at ?? version.created_at)}
                    </div>
                  </div>
                  <div className="flex w-[7.5rem] shrink-0 flex-col items-stretch gap-1">
                    {!version.is_main && (
                      <Button
                        type="button"
                        variant="outline"
                        size="sm"
                        className="h-8 justify-start gap-1"
                        onClick={(event) => {
                          event.stopPropagation();
                          onSetMainVersion(version);
                        }}
                        disabled={isRestoring || deletingUid === version.uid}
                        title="Set as Main"
                      >
                        <GitBranch className="h-3.5 w-3.5" />
                        Set Main
                      </Button>
                    )}
                    {!version.is_main && (
                      <Button
                        type="button"
                        variant="ghost"
                        size="sm"
                        className="h-8 justify-start gap-1 text-red-500 hover:bg-red-500/10 hover:text-red-500"
                        onClick={(event) => {
                          event.stopPropagation();
                          void handleDeleteVersion(version);
                        }}
                        disabled={isRestoring || deletingUid === version.uid}
                        title="Delete version"
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                        Delete
                      </Button>
                    )}
                  </div>
                </div>
                <div className="flex flex-wrap gap-2 text-[11px] text-muted-foreground">
                  <span className="rounded bg-background/70 px-2 py-1">
                    {version.node_count ?? 0} steps
                  </span>
                  <span className="rounded bg-background/70 px-2 py-1">
                    {version.edge_count ?? 0} links
                  </span>
                  <span className="rounded bg-background/70 px-2 py-1">
                    {version.file_count ?? 0} files
                  </span>
                  {!version.is_main && version.version_index != null && (
                    <span className="rounded bg-background/70 px-2 py-1">
                      #{version.version_index}
                    </span>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      </ScrollArea>
    </div>
  );
};
