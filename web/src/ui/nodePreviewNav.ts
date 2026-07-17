import { createContext, useContext } from 'react';

// Cross-tree hook to open the shared NodePreview drawer (ADR-054 §5) — any NodeChip anywhere calls
// `openNode` and the single app-level drawer (mounted in AppShell) shows that node's preview. Mirrors
// mapNav's MapNavContext. The `hint` lets the drawer render its header (icon + title) instantly while
// NodePreview fetches the body. Kept out of the component module so react-refresh stays happy.

export interface NodeHint {
  type: string | null;
  title: string | null;
}

export interface NodePreviewNav {
  openNode: (nodeId: string, hint?: NodeHint) => void;
}

export const NodePreviewNavContext = createContext<NodePreviewNav | null>(null);

// Null when there is no provider (e.g. a NodeChip rendered outside the shell) — NodeChip then degrades
// to a static pill.
export function useNodePreview(): NodePreviewNav | null {
  return useContext(NodePreviewNavContext);
}
