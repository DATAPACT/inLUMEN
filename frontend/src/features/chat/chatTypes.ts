export type GraphProposalStatus = 'pending' | 'applied' | 'discarded';

export type ChatMessage = {
  id?: string;
  turnId?: string;
  sequence?: number;
  status?: string;
  role: 'user' | 'assistant';
  content: string;
  graphProposalStatus?: GraphProposalStatus;
};

export type CanvasSyncState = 'idle' | 'syncing' | 'synced' | 'unchanged' | 'warning' | 'error';

export type CanvasSyncStatus = {
  state: CanvasSyncState;
  message: string;
  updatedAt?: string | null;
};
