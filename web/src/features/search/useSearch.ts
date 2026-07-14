// Server state for the Search tab (TanStack Query, 06 §5). Semantic search over the graph is a
// read (no LLM): a query is issued only when the user submits one, cached by (query, planes,
// types) so re-toggling a chip or re-expanding a node doesn't re-hit the server. Node previews
// load lazily on expand.
import { useQuery } from '@tanstack/react-query';
import { api } from '../../api/client';
import type { SearchResultItem } from '../../api/types';

// The submitted search — null until the user runs one. `planes`/`types` are kept sorted so a set
// that differs only in order shares a cache entry.
export interface Submitted {
  query: string;
  planes: string[];
  types: string[];
}

export function usePlanes() {
  return useQuery({
    queryKey: ['planes'],
    queryFn: () => api.planes(),
    // The plane vocabulary is server config; it effectively never changes within a session.
    staleTime: Infinity,
  });
}

// The effective type vocabulary (config seeds ∪ approved additions) for the type-filter chips.
export function useTypes() {
  return useQuery({
    queryKey: ['types'],
    queryFn: () => api.types(),
    staleTime: Infinity,
  });
}

export function useSearch(submitted: Submitted | null) {
  return useQuery<SearchResultItem[]>({
    queryKey: ['search', submitted?.query ?? '', submitted?.planes ?? [], submitted?.types ?? []],
    queryFn: () => api.search(submitted!.query, submitted!.planes, submitted!.types),
    enabled: submitted != null && submitted.query.trim() !== '',
  });
}

export function useNode(nodeId: string | null) {
  return useQuery({
    queryKey: ['node', nodeId],
    queryFn: () => api.getNode(nodeId!),
    enabled: nodeId != null,
  });
}
