// Server state for the Review tab (TanStack Query, 06 §3b). The queue is a read; resolving an item
// is a mutation that materializes a pending edge / mints an entity / approves a type / ingests a
// stance-candidate / folds a dedup-proposal on the server, so on success we invalidate both queue
// lists (pending + parked maybe — a decision moves an item between them) plus the vocabulary + any
// node views the resolution may have changed. Batch resolve (ADR-048 §8) shares the same seams.
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../../api/client';
import type { ReviewBatchResponse, ReviewItemResponse, ReviewResolveBody } from '../../api/types';

const PENDING_KEY = ['review', 'pending'] as const;
const MAYBE_KEY = ['review', 'maybe'] as const;

// The active queue — items awaiting a first decision (salience-ordered client-side in the screen).
export function useReview() {
  return useQuery<ReviewItemResponse[]>({
    queryKey: PENDING_KEY,
    queryFn: () => api.listReview('pending'),
  });
}

// The parked pile — `maybe` items stay decidable and re-openable (no expiry, ADR-048 §7); the screen
// shows them under an aging indicator so the pile can't stall silently.
export function useReviewMaybe() {
  return useQuery<ReviewItemResponse[]>({
    queryKey: MAYBE_KEY,
    queryFn: () => api.listReview('maybe'),
  });
}

function invalidateReview(qc: ReturnType<typeof useQueryClient>) {
  qc.invalidateQueries({ queryKey: PENDING_KEY });
  qc.invalidateQueries({ queryKey: MAYBE_KEY });
  // Approving a type mutates the effective vocabulary; a pick/new/merge materializes graph edges.
  qc.invalidateQueries({ queryKey: ['types'] });
  qc.invalidateQueries({ queryKey: ['node'] });
}

export function useResolveReview() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (v: { id: string; body: ReviewResolveBody }) => api.resolveReview(v.id, v.body),
    onSuccess: () => invalidateReview(qc),
  });
}

// Multi-select batch resolve: one action string across many ids, best-effort per item. Resolves to
// the per-item results so the screen can surface how many landed vs failed.
export function useBatchResolve() {
  const qc = useQueryClient();
  return useMutation<ReviewBatchResponse, Error, { ids: string[]; action: string }>({
    mutationFn: (v) => api.resolveReviewBatch(v.ids, v.action),
    onSuccess: () => invalidateReview(qc),
  });
}
