export type ChatMessage = {
  role: 'user' | 'assistant';
  content: string;
};

export type CanvasSyncState = 'idle' | 'syncing' | 'synced' | 'unchanged' | 'warning' | 'error';

export type CanvasSyncStatus = {
  state: CanvasSyncState;
  message: string;
  updatedAt?: string | null;
};
