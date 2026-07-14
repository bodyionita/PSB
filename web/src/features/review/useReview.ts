// Server state for the Review tab (TanStack Query, 06 §3b — the M3 minimal admin surface, polished
// UX at M6). The queue is a read; resolving an item is a mutation that materializes a pending edge
// / mints an entity / approves a type on the server, so on success we invalidate the queue plus the
// vocabulary + any node views the resolution may have changed.
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../../api/client';
import type { ReviewChoice, ReviewItemResponse, ReviewVerdict } from '../../api/types';

const REVIEW_KEY = ['review', 'pending'] as const;

export function useReview() {
  return useQuery<ReviewItemResponse[]>({
    queryKey: REVIEW_KEY,
    queryFn: () => api.listReview('pending'),
  });
}

export function useResolveReview() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (v: { id: string; body: { choice?: ReviewChoice; verdict?: ReviewVerdict } }) =>
      api.resolveReview(v.id, v.body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: REVIEW_KEY });
      // Approving a type mutates the effective vocabulary; a pick/new materializes graph edges.
      qc.invalidateQueries({ queryKey: ['types'] });
      qc.invalidateQueries({ queryKey: ['node'] });
    },
  });
}
