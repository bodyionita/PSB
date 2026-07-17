// Server state for the Activity screen (TanStack Query, 06 §3 / ADR-053). The Feed is an infinite
// keyset scroll over GET /activity; the Ops console reads the live scheduler roster + pipelines and
// triggers/polls background runs. Poll cadence is active ONLY while something is running (06 §3).
import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';
import { api } from '../../api/client';
import { AUTO_RECORDED_KEY } from '../chat/useChat';
import type {
  ActivityCategory,
  ActivityFeedResponse,
  AgentRosterItem,
  AgentRunResponse,
  EdgeRetypeItem,
  PipelineItem,
  RunLogLine,
  RunStatus,
  TagMergeItem,
} from '../../api/types';

const TERMINAL: ReadonlySet<RunStatus> = new Set<RunStatus>(['succeeded', 'failed', 'skipped']);
const RUN_POLL_MS = 1500;
const ROSTER_POLL_MS = 2000;
const LOG_POLL_MS = 1000;

export function isTerminal(status: RunStatus): boolean {
  return TERMINAL.has(status);
}

// --- Feed (GET /activity — keyset infinite scroll) ----------------------------------------------

export const FEED_KEY = ['activity', 'feed'] as const;

// `category` null ⇒ the "all" view (server unions all three). Each page carries `next_before`, the
// opaque cursor for the older page; undefined getNextPageParam ends the scroll.
export function useActivityFeed(category: ActivityCategory | null) {
  return useInfiniteQuery<ActivityFeedResponse>({
    queryKey: [...FEED_KEY, category],
    queryFn: ({ pageParam }) =>
      api.activityFeed(category ?? undefined, pageParam as string | null),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_before ?? undefined,
  });
}

// --- One run's live status (drill-down + admin-op runs) -----------------------------------------

// Polls one background run until terminal. `runId` null ⇒ idle (disabled).
export function useRun(runId: string | null) {
  return useQuery<AgentRunResponse>({
    queryKey: ['activity', 'run', runId],
    queryFn: () => api.getRun(runId!),
    enabled: runId != null,
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status && isTerminal(status) ? false : RUN_POLL_MS;
    },
    // A live run must keep updating even when the app isn't the foreground tab (06 §3 "while a run
    // is active"), so poll in the background too — it self-stops the moment the run is terminal.
    refetchIntervalInBackground: true,
  });
}

// --- Live log tail (GET /activity/runs/{id}/logs — poll, accumulate) ----------------------------

// Accumulates the tail across polls (the endpoint returns only lines after the cursor). Keeps
// paging while the run is running AND — critically — until a page comes back empty even after it
// flips terminal, because the on-finish flush is async and one page is capped (ADR-053 §2): stopping
// the instant `running==false` can miss the last lines.
export function useRunLogs(runId: string | null): { lines: RunLogLine[]; running: boolean } {
  const [lines, setLines] = useState<RunLogLine[]>([]);
  const [running, setRunning] = useState(true);
  const cursorRef = useRef(0);
  const drainingRef = useRef(true);
  // The run this accumulator is bound to — guards against a late poll from a previous run id
  // resolving after we've switched runs and clobbering the new run's lines/cursor.
  const boundRunRef = useRef(runId);

  useEffect(() => {
    // Reset the accumulator whenever we switch runs.
    boundRunRef.current = runId;
    setLines([]);
    setRunning(true);
    cursorRef.current = 0;
    drainingRef.current = true;
  }, [runId]);

  useQuery({
    queryKey: ['activity', 'run-logs', runId],
    enabled: runId != null,
    queryFn: async () => {
      const res = await api.getRunLogs(runId!, cursorRef.current);
      // Drop a response that resolved after the hook switched runs (stale-poll guard).
      if (boundRunRef.current !== runId) return res;
      if (res.logs.length > 0) setLines((prev) => [...prev, ...res.logs]);
      cursorRef.current = res.next_after_seq;
      drainingRef.current = res.logs.length > 0;
      setRunning(res.running);
      return res;
    },
    // Keep polling while the run is live, or while the last page still returned lines (draining the
    // async on-finish flush). Stop only once it's terminal AND a page came back empty.
    refetchInterval: () => (running || drainingRef.current ? LOG_POLL_MS : false),
    refetchIntervalInBackground: true,
  });

  return { lines, running };
}

// --- Ops roster + pipelines (live scheduler) ----------------------------------------------------

export const ROSTER_KEY = ['activity', 'agents'] as const;
export const PIPELINES_KEY = ['activity', 'pipelines'] as const;

function anyAgentRunning(agents: AgentRosterItem[] | undefined): boolean {
  return (agents ?? []).some((a) => a.running);
}
function anyPipelineRunning(pipelines: PipelineItem[] | undefined): boolean {
  return (pipelines ?? []).some((p) => p.last_run?.status === 'running');
}

export function useAgents() {
  return useQuery<AgentRosterItem[]>({
    queryKey: ROSTER_KEY,
    queryFn: () => api.listAgents(),
    refetchInterval: (query) => (anyAgentRunning(query.state.data) ? ROSTER_POLL_MS : false),
    refetchIntervalInBackground: true,
  });
}

export function usePipelines() {
  return useQuery<PipelineItem[]>({
    queryKey: PIPELINES_KEY,
    queryFn: () => api.listPipelines(),
    refetchInterval: (query) => (anyPipelineRunning(query.state.data) ? ROSTER_POLL_MS : false),
    refetchIntervalInBackground: true,
  });
}

// After a manual trigger the run id isn't in the 202 body — it surfaces on the roster's
// last_run.run_id — so we just invalidate the roster + pipelines to pick up the new running state.
export function useRunAgent() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (name: string) => api.runAgent(name),
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ROSTER_KEY });
      qc.invalidateQueries({ queryKey: PIPELINES_KEY });
    },
  });
}

export function useRunPipeline() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (name: string) => api.runPipeline(name),
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ROSTER_KEY });
      qc.invalidateQueries({ queryKey: PIPELINES_KEY });
    },
  });
}

// --- Rehomed parameterized admin ops (ADR-053 §8 — the ops that can't collapse to a bare Run;
// zero-arg reindex/backup are the roster's Run buttons, not cards) -------------------------------

export function useProposeTags() {
  return useMutation({ mutationFn: () => api.proposeTags() });
}
export function useApplyTags() {
  return useMutation({ mutationFn: (plan: TagMergeItem[]) => api.applyTags(plan) });
}
export function useReprocessPreview() {
  return useMutation({ mutationFn: () => api.reprocessPreview() });
}
export function useReprocessConfirm() {
  return useMutation({ mutationFn: () => api.reprocessConfirm() });
}
export function useMergeEntitiesPropose() {
  return useMutation({
    mutationFn: (v: { loser: string; survivor: string }) =>
      api.mergeEntitiesPropose(v.loser, v.survivor),
  });
}
export function useMergeEntitiesApply() {
  return useMutation({
    mutationFn: (v: { loser: string; survivor: string }) =>
      api.mergeEntitiesApply(v.loser, v.survivor),
  });
}
export function useConsolidateVocabPropose() {
  return useMutation({ mutationFn: (rel: string) => api.consolidateVocabPropose(rel) });
}
export function useConsolidateVocabApply() {
  return useMutation({
    mutationFn: (v: { rel: string; plan: EdgeRetypeItem[] }) =>
      api.consolidateVocabApply(v.rel, v.plan),
  });
}

// --- Conversations one-tap remove (folds in the M6 auto-recorded remove, ADR-053 §4) ------------

// A conversation feed row's `id` IS the capture id, so remove targets it directly. On success we
// invalidate the feed (the row drops via the server's `removed_at` filter) + the chat-scoped
// auto-recorded list so both views stay in sync.
export function useRemoveConversation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (captureId: string) => api.removeAutoRecorded(captureId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: FEED_KEY });
      qc.invalidateQueries({ queryKey: AUTO_RECORDED_KEY });
    },
  });
}
