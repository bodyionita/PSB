// Server state for the Admin tab (TanStack Query, 06 §6). The operational actions (reindex,
// backup, tag consolidation) are mutations; reindex + tags-apply return a background run_id that
// we then poll via GET /activity/runs/{id} until it reaches a terminal status.
import { useMutation, useQuery } from '@tanstack/react-query';
import { api } from '../../api/client';
import type { AgentRunResponse, RunStatus, TagMergeItem } from '../../api/types';

const TERMINAL: ReadonlySet<RunStatus> = new Set<RunStatus>(['succeeded', 'failed', 'skipped']);
const RUN_POLL_MS = 1500;

export function isTerminal(status: RunStatus): boolean {
  return TERMINAL.has(status);
}

export function useReindex() {
  return useMutation({ mutationFn: () => api.reindex() });
}

export function useBackup() {
  return useMutation({ mutationFn: () => api.backup() });
}

export function useProposeTags() {
  return useMutation({ mutationFn: () => api.proposeTags() });
}

export function useApplyTags() {
  return useMutation({ mutationFn: (plan: TagMergeItem[]) => api.applyTags(plan) });
}

// Polls one background run until it finishes. `runId` null ⇒ idle (disabled).
export function useRun(runId: string | null) {
  return useQuery<AgentRunResponse>({
    queryKey: ['run', runId],
    queryFn: () => api.getRun(runId!),
    enabled: runId != null,
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status && isTerminal(status) ? false : RUN_POLL_MS;
    },
  });
}
