// Server state for Settings → Vocabulary (TanStack Query, 06 §4 / ADR-027). `GET /types` is the
// one authoritative view of the effective vocabulary + pending proposals; approving/rejecting a
// proposal goes through `PUT /settings/vocabulary` (the same governance choke point as the Review
// queue). The `['types']` key is shared with the search type-filter chips, so an approval refreshes
// both surfaces at once.
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../../api/client';
import type { ReviewVerdict, TypesResponse } from '../../api/types';

export function useVocabulary() {
  return useQuery<TypesResponse>({
    queryKey: ['types'],
    queryFn: () => api.types(),
  });
}

export function useResolveVocabulary() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (v: { reviewId: string; verdict: ReviewVerdict }) =>
      api.resolveVocabulary(v.reviewId, v.verdict),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['types'] });
      // The same proposal item is visible in the Review queue — keep it in sync.
      qc.invalidateQueries({ queryKey: ['review', 'pending'] });
    },
  });
}
