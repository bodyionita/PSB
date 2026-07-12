// Server state for the capture screen (TanStack Query, per the engineering rules in 06). The
// list polls only while something is still moving through the pipeline (08 M1: ~2s while
// in-flight, idle otherwise), and every write invalidates it so the strip reflects reality fast.
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../../api/client';
import type { CaptureView } from '../../api/types';

const CAPTURES_KEY = ['captures'] as const;
const TERMINAL: ReadonlySet<string> = new Set(['indexed', 'failed']);
const POLL_MS = 2000;

export function isInFlight(c: CaptureView): boolean {
  return !TERMINAL.has(c.status);
}

export function useCaptures() {
  return useQuery({
    queryKey: CAPTURES_KEY,
    queryFn: () => api.listCaptures(20),
    refetchInterval: (query) => (query.state.data?.some(isInFlight) ? POLL_MS : false),
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
    onSuccess: () => qc.invalidateQueries({ queryKey: CAPTURES_KEY }),
  });
}
