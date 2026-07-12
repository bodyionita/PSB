// Server state for the capture screen (TanStack Query, per the engineering rules in 06). The
// list polls only while something is still moving through the pipeline (08 M1: ~2s while
// in-flight, idle otherwise), and every write invalidates it so the strip reflects reality fast.
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../../api/client';
import type { CaptureView } from '../../api/types';

const CAPTURES_KEY = ['captures'] as const;
const TERMINAL: ReadonlySet<string> = new Set(['indexed', 'failed']);
const POLL_MS = 2000;
// A capture reaches `indexed` BEFORE its trailing follow-up nudge is generated (ADR-019 §1: the
// nudge is a non-blocking step after indexing). If we stopped polling at `indexed`, the nudge —
// the signature "dig deeper" feature — would never render. Keep polling a bounded window past
// the last update so it appears, without polling forever for a nudge that never comes (an
// Inbox-fallback capture gets none).
const SETTLE_MS = 20000;

export function isInFlight(c: CaptureView): boolean {
  return !TERMINAL.has(c.status);
}

// Whether the strip should keep polling this capture even though it is at a terminal status:
// it is `indexed`, no nudge has landed yet, and it was updated recently — the trailing nudge
// may still be on its way.
function isSettling(c: CaptureView): boolean {
  if (c.status !== 'indexed') return false;
  if (c.follow_up_question != null || c.follow_up_answer != null) return false;
  const updated = c.updated_at ? Date.parse(c.updated_at) : NaN;
  return Number.isFinite(updated) && Date.now() - updated < SETTLE_MS;
}

export function useCaptures() {
  return useQuery({
    queryKey: CAPTURES_KEY,
    queryFn: () => api.listCaptures(20),
    refetchInterval: (query) =>
      query.state.data?.some((c) => isInFlight(c) || isSettling(c)) ? POLL_MS : false,
  });
}

export function useCaptureText() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (text: string) => api.captureText(text),
    onSuccess: () => qc.invalidateQueries({ queryKey: CAPTURES_KEY }),
  });
}

export function useCaptureVoice() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (v: { blob: Blob; filename: string }) => api.captureVoice(v.blob, v.filename),
    onSuccess: () => qc.invalidateQueries({ queryKey: CAPTURES_KEY }),
  });
}

export function useRetryCapture() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.retryCapture(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: CAPTURES_KEY }),
  });
}

export function useSubmitFollowUp() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (v: { id: string; answer: string }) => api.submitFollowUp(v.id, v.answer),
    onSuccess: (_res, v) => {
      // Pass 2 was accepted (202) and runs async, cycling status indexed→organizing→…→indexed
      // AFTER this response (ADR-019 §2). A single refetch would race that flip and often catch
      // the capture still at `indexed`, so polling would never restart. Optimistically mark it
      // `organizing` so the strip resumes live polling deterministically and renders the
      // re-processing; the subsequent invalidate reconciles with the real server states.
      qc.setQueryData<CaptureView[]>(CAPTURES_KEY, (list) =>
        list?.map((c) =>
          c.capture_id === v.id ? { ...c, status: 'organizing', follow_up_answer: v.answer } : c,
        ),
      );
      void qc.invalidateQueries({ queryKey: CAPTURES_KEY });
    },
  });
}
