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

// Whether the strip should keep polling this capture even though it sits at the terminal
// `indexed` status, because a further transition is still expected:
//   - the trailing follow-up nudge is generated AFTER indexing (question still null), or
//   - the user just answered the nudge and Pass 2 re-cycles organizing→…→indexed AFTER the
//     202 (answer set) — its status flip can lag a refetch, so we must not stop on the
//     transient `indexed` in between.
// We keep polling for a bounded window past the last server update (updated_at is bumped on
// every capture write, incl. the answer), then give up — an Inbox-fallback capture never gets
// a nudge, so this must not poll forever. The one genuinely-settled case is a nudge shown and
// awaiting the user's answer: question present, answer absent → stop.
function isSettling(c: CaptureView): boolean {
  if (c.status !== 'indexed') return false;
  if (c.follow_up_question != null && c.follow_up_answer == null) return false;
  const updated = c.updated_at ? Date.parse(c.updated_at) : NaN;
  return Number.isFinite(updated) && Date.now() - updated < SETTLE_MS;
}

// `limit` (M8.1, ADR-054 §4): the Capture-tab Recents strip shrinks to ~5 with a "see all → Activity"
// link; the default 20 stays for any caller that still wants the fuller strip.
export function useCaptures(limit = 20) {
  return useQuery({
    queryKey: [...CAPTURES_KEY, limit],
    queryFn: () => api.listCaptures(limit),
    refetchInterval: (query) =>
      query.state.data?.some((c) => isInFlight(c) || isSettling(c)) ? POLL_MS : false,
  });
}

// One capture's full detail (GET /captures/{id}) — the Activity Captures-tab row expand (M8.1,
// ADR-054 §4): the feed row only carries a truncated snippet, so drilling in re-fetches the full
// raw_text + node_refs + status/source. `id` null ⇒ disabled (not expanded yet).
export function useCapture(id: string | null) {
  return useQuery({
    queryKey: ['captures', 'detail', id],
    queryFn: () => api.getCapture(id!),
    enabled: id != null,
  });
}

// --- Composite compose draft (M9.6 T5, ADR-061 §3) ---
// The draft is server-held state keyed on the single active draft. `useDraft` fetches/resumes it;
// the part/text/submit/discard mutations invalidate it (and the captures list on submit). The
// compose screen owns the draft id and threads it into each mutation.
const DRAFT_KEY = ['capture', 'draft'] as const;

export function useDraft(enabled: boolean) {
  return useQuery({
    queryKey: DRAFT_KEY,
    // POST opens-or-resumes (idempotent, one active draft); we treat it as the draft "read".
    queryFn: () => api.openDraft(),
    enabled,
    staleTime: Infinity, // the draft only changes via the mutations below, which set it directly
    gcTime: 0,
  });
}

export function useAddDraftPart() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (v: { id: string; blob: Blob; filename: string; kind: 'photo' | 'voice' }) =>
      api.addDraftPart(v.id, v.blob, v.filename, v.kind),
    onSuccess: () => qc.invalidateQueries({ queryKey: DRAFT_KEY }),
  });
}

export function useRemoveDraftPart() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (v: { id: string; mediaId: string }) => api.removeDraftPart(v.id, v.mediaId),
    onSuccess: () => qc.invalidateQueries({ queryKey: DRAFT_KEY }),
  });
}

export function useEditDraftText() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (v: { id: string; text: string }) => api.editDraftText(v.id, v.text),
    // The PUT returns the fresh draft — seed it directly so the field doesn't flicker on refetch.
    onSuccess: (draft) => qc.setQueryData(DRAFT_KEY, draft),
  });
}

export function useSubmitDraft() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.submitDraft(id),
    onSuccess: () => {
      // The draft became a committed capture — drop the draft cache (a fresh openDraft opens a new
      // one) and refresh the captures strip so the new capture appears + polls.
      qc.removeQueries({ queryKey: DRAFT_KEY });
      qc.invalidateQueries({ queryKey: CAPTURES_KEY });
    },
  });
}

export function useDiscardDraft() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.discardDraft(id),
    onSuccess: () => qc.removeQueries({ queryKey: DRAFT_KEY }),
  });
}

export function useRetryCapture() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.retryCapture(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: CAPTURES_KEY }),
  });
}

// The anchor edit (M8.2, ADR-056 §5): correct a capture's recorded-at, triggering a background
// one-capture reorganize (202). Invalidate both the list and the capture's detail so the corrected
// recorded-at + the re-cycling status render; polling re-engages via `isSettling`/`isInFlight` as
// the reorganize moves it through organizing→…→indexed again.
export function useEditCaptureAnchor() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (v: { id: string; anchor: string }) => api.editCaptureAnchor(v.id, v.anchor),
    onSuccess: (_data, v) => {
      qc.invalidateQueries({ queryKey: CAPTURES_KEY });
      qc.invalidateQueries({ queryKey: ['captures', 'detail', v.id] });
    },
  });
}

export function useSubmitFollowUp() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (v: { id: string; answer: string }) => api.submitFollowUp(v.id, v.answer),
    // Pass 2 was accepted (202) and re-cycles the capture indexed→organizing→…→indexed AFTER
    // this response (ADR-019 §2). Invalidating re-engages the (now idle) list query; polling
    // then keeps running because `isSettling` treats an `indexed` capture whose nudge has been
    // answered as still-in-flight for a bounded window (the answer bumps updated_at), so the
    // re-processing renders live without depending on a single refetch winning the race.
    onSuccess: () => qc.invalidateQueries({ queryKey: CAPTURES_KEY }),
  });
}
