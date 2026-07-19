// The set of entity-like node kinds (from GET /types), so a surface can tell an entity hub — the
// only thing that can be merged (ADR-030 §5 / ADR-064 §2) — from a content node. Used to gate the
// profile "Merge into…" affordance to entity nodes. Vocabulary changes rarely, so it's cached long.
import { useMemo } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api } from '../api/client';

export function useEntityLikeTypes(): Set<string> {
  const { data } = useQuery({
    queryKey: ['types'],
    queryFn: () => api.types(),
    staleTime: 5 * 60_000,
  });
  return useMemo(() => new Set(data?.entity_like_types ?? []), [data]);
}
