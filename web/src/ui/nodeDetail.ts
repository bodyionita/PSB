// Node-detail read + naming helper shared by the node preview and its consumers (Search / Chat).
// Kept separate from the component module so each file exports one kind of thing (react-refresh).
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../api/client';
import type { NodeDateTokenEdit } from '../api/types';

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

// The mechanical date-token edit (M8.2, ADR-056 §5). On success the node's body + occurred may have
// changed, so invalidate that node's detail (the preview re-renders the new phrase) and any map
// neighborhood that node sits in. A 400 (bad/absent token, uninterpretable date) surfaces as an
// ApiError the caller shows inline.
export function useEditNodeDateToken(nodeId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: NodeDateTokenEdit) => api.editNodeDateToken(nodeId, body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['node', nodeId] });
      qc.invalidateQueries({ queryKey: ['neighbors'] });
    },
  });
}
