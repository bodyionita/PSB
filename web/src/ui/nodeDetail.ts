// Node-detail read + naming helper shared by the node preview and its consumers (Search / Chat).
// Kept separate from the component module so each file exports one kind of thing (react-refresh).
import { useQuery } from '@tanstack/react-query';
import { api } from '../api/client';

// The store path's file stem — the human name when a node has no explicit title.
export function baseName(path: string): string {
  const parts = path.split('/');
  return (parts[parts.length - 1] ?? path).replace(/\.md$/, '');
}

// Lazy node-detail read; loads only when a card is expanded (nodeId non-null).
export function useNode(nodeId: string | null) {
  return useQuery({
    queryKey: ['node', nodeId],
    queryFn: () => api.getNode(nodeId!),
    enabled: nodeId != null,
  });
}
