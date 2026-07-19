// Server state for the shared entity picker's name-typeahead (M9.8 T2, ADR-064 §2). Backed by
// `GET /entities` (diacritic-folded name/alias match). `enabled` gates the request (closed dropdown
// → no query); the query key includes the term + type so results cache per input, and previous
// data is kept during a new fetch so the list doesn't flicker empty between keystrokes.
import { useQuery } from '@tanstack/react-query';
import { api } from '../api/client';
import type { EntityBrowseItem } from '../api/types';

export function useEntitySearch(q: string, type: string | undefined, enabled: boolean) {
  const term = q.trim();
  return useQuery<EntityBrowseItem[]>({
    queryKey: ['entities', type ?? null, term],
    queryFn: () => api.entities(term || undefined, type, 20),
    enabled,
    staleTime: 30_000,
    placeholderData: (prev) => prev,
  });
}
