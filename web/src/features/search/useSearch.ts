// Server state behind search (TanStack Query, 06 §5; ADR-054 §3 — folded into the Explore tab, but
// kept here since `usePlanes` is also the canonical plane-vocabulary read reused by Chat's plane
// chips). Semantic search over the graph is a read (no LLM): a query is issued only when the user
// submits one, cached by (query, planes, types) so re-expanding a node doesn't re-hit the server.
// `planes`/`types` stay in the contract for callers that still filter (the API, MCP) — Explore's own
// search panel just never sets them (filter-chip UI removed, ADR-054 §3). Node previews load lazily
// on expand.
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

// `useNode` (the node-detail read behind an expanded card) moved to the shared ui/NodePreview
// primitive, colocated with its only consumer.
