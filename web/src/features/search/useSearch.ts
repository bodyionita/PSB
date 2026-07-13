// Server state for the Search tab (TanStack Query, 06 §5). Semantic search is a read (no LLM):
// a query is issued only when the user submits one, cached by (query, planes) so re-toggling a
// chip or re-expanding a note doesn't re-hit the server. Note previews load lazily on expand.
import { useQuery } from '@tanstack/react-query';
import { api } from '../../api/client';
import type { SearchResultItem } from '../../api/types';

// The submitted search — null until the user runs one. `planes` is kept sorted so a set that
// differs only in order shares a cache entry.
export interface Submitted {
  query: string;
  planes: string[];
}

export function usePlanes() {
  return useQuery({
    queryKey: ['planes'],
    queryFn: () => api.planes(),
    // The plane vocabulary is server config; it effectively never changes within a session.
    staleTime: Infinity,
  });
}

export function useSearch(submitted: Submitted | null) {
  return useQuery<SearchResultItem[]>({
    queryKey: ['search', submitted?.query ?? '', submitted?.planes ?? []],
    queryFn: () => api.search(submitted!.query, submitted!.planes),
    enabled: submitted != null && submitted.query.trim() !== '',
  });
}

export function useNote(noteId: string | null) {
  return useQuery({
    queryKey: ['note', noteId],
    queryFn: () => api.getNote(noteId!),
    enabled: noteId != null,
  });
}
