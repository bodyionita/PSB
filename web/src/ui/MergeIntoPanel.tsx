// The profile "Merge into…" affordance (M9.8 T3, ADR-064 §2a). Sits on an entity's NodePreview and
// folds *this* node (the loser) into another entity the user picks by name via the shared
// <EntityPicker> — no UUID ever typed. It drives the same two-step propose → apply the AdminOps card
// uses (ADR-030 §5): preview the inbound-edge inventory, then confirm. Apply returns a background
// run we poll to a terminal state; on success the loser is tombstoned, so we invalidate its detail.
//
// Self-contained in `ui/` (talks to `api` directly, like <EntityPicker>) so the layering stays
// clean — the shared preview never reaches into a feature. Collapsed to a subtle button by default.
import { motion } from 'framer-motion';
import { useEffect, useState, type CSSProperties } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api, ApiError } from '../api/client';
import type { EntityBrowseItem, EntityMergeProposeResponse, RunStatus } from '../api/types';
import { Button } from './Button';
import { EntityPicker } from './EntityPicker';

const FAIL_COLOR = '#ff6b6b';
const TERMINAL = new Set<RunStatus>(['succeeded', 'failed', 'skipped']);

const pillStyle: CSSProperties = {
  fontSize: 11,
  fontWeight: 600,
  color: 'var(--muted)',
  border: '1px solid var(--surface-border)',
  borderRadius: 999,
  padding: '3px 9px',
  fontVariantNumeric: 'tabular-nums',
};

function errorText(err: unknown, fallback: string): string {
  return err instanceof ApiError ? err.message : fallback;
}

// Poll the background merge run until it settles (mirrors the activity console's run poll, kept
// local so `ui/` doesn't import a feature hook).
function useMergeRun(runId: string | null) {
  return useQuery({
    queryKey: ['node-merge-run', runId],
    queryFn: () => api.getRun(runId!),
    enabled: runId != null,
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status && TERMINAL.has(status) ? false : 1500;
    },
  });
}

export function MergeIntoPanel({
  loser,
  onMerged,
}: {
  loser: { id: string; type: string; title: string | null };
  // Fired once when the background merge run reaches `succeeded` — lets a host (the graph-health
  // orphan row) durably settle the loser to a resolved state (M9.8 T7 fix, ADR-064 §3).
  onMerged?: () => void;
}) {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [survivor, setSurvivor] = useState<EntityBrowseItem | null>(null);
  const [plan, setPlan] = useState<EntityMergeProposeResponse | null>(null);
  const [runId, setRunId] = useState<string | null>(null);

  const propose = useMutation({
    mutationFn: (v: { survivorId: string }) => api.mergeEntitiesPropose(loser.id, v.survivorId),
    onSuccess: (p) => setPlan(p),
  });
  const apply = useMutation({
    mutationFn: (v: { survivorId: string }) => api.mergeEntitiesApply(loser.id, v.survivorId),
    onSuccess: (r) => {
      setRunId(r.run_id);
      setPlan(null);
      // The loser is now tombstoned + its neighborhood retargeted — refresh both so the UI reflects it.
      qc.invalidateQueries({ queryKey: ['node', loser.id] });
      qc.invalidateQueries({ queryKey: ['neighbors'] });
    },
  });

  const run = useMergeRun(runId);

  // Notify the host exactly once the merge run settles as succeeded (the loser is now tombstoned).
  useEffect(() => {
    if (run.data?.status === 'succeeded') onMerged?.();
    // Fire on the transition to succeeded; onMerged is idempotent on the host side (marks a set).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [run.data?.status]);

  const reset = () => {
    setSurvivor(null);
    setPlan(null);
    setRunId(null);
    propose.reset();
    apply.reset();
  };

  if (!open) {
    return (
      <div style={{ marginTop: 14 }}>
        <Button
          variant="ghost"
          style={{ padding: '8px 14px', fontSize: 13 }}
          onClick={() => setOpen(true)}
        >
          Merge into…
        </Button>
      </div>
    );
  }

  const merging = apply.isPending || (runId != null && run.data?.status === 'running');

  return (
    <motion.div
      initial={{ opacity: 0, y: -4 }}
      animate={{ opacity: 1, y: 0 }}
      style={{ marginTop: 14, paddingTop: 14, borderTop: '1px solid var(--surface-border)', display: 'grid', gap: 10 }}
    >
      <p style={{ margin: 0, fontSize: 12.5, color: 'var(--muted)', lineHeight: 1.5 }}>
        Fold <b style={{ color: 'var(--text)' }}>{loser.title ?? loser.id}</b> into another entity —
        its inbound edges retarget, aliases union, and it's tombstoned. Pick the entity to keep, then
        review the edges before merging.
      </p>

      <EntityPicker
        value={survivor}
        onChange={(s) => {
          setSurvivor(s);
          setPlan(null);
        }}
        type={loser.type}
        excludeId={loser.id}
        placeholder="Search the entity to keep…"
        autoFocus
      />

      {survivor && plan == null && runId == null && (
        <div style={{ display: 'flex', gap: 10 }}>
          <Button
            variant="ghost"
            onClick={() => propose.mutate({ survivorId: survivor.id })}
            disabled={propose.isPending}
          >
            {propose.isPending ? 'Checking…' : 'Preview merge'}
          </Button>
          <Button variant="ghost" onClick={() => setOpen(false)}>
            Cancel
          </Button>
        </div>
      )}

      {propose.isError && (
        <p style={{ margin: 0, fontSize: 13, color: FAIL_COLOR }}>
          {propose.error instanceof ApiError && propose.error.status === 404
            ? 'That entity could not be found.'
            : errorText(propose.error, 'Couldn’t preview the merge.')}
        </p>
      )}

      {plan != null && (
        <div style={{ display: 'grid', gap: 10 }}>
          <span style={pillStyle}>
            {plan.inbound_count} inbound edge{plan.inbound_count === 1 ? '' : 's'} → {plan.survivor.title ?? plan.survivor.id}
          </span>
          <div style={{ display: 'flex', gap: 10 }}>
            <Button
              onClick={() => apply.mutate({ survivorId: plan.survivor.id })}
              disabled={apply.isPending}
            >
              {apply.isPending ? 'Merging…' : 'Merge'}
            </Button>
            <Button variant="ghost" onClick={() => setPlan(null)} disabled={apply.isPending}>
              Cancel
            </Button>
          </div>
        </div>
      )}

      {apply.isError && (
        <p style={{ margin: 0, fontSize: 13, color: FAIL_COLOR }}>
          {errorText(apply.error, 'Couldn’t apply the merge.')}
        </p>
      )}

      {runId != null && (
        <p style={{ margin: 0, fontSize: 13, color: run.data?.status === 'failed' ? FAIL_COLOR : 'var(--muted)', lineHeight: 1.5 }}>
          {run.data == null || run.data.status === 'running'
            ? 'Merging…'
            : run.data.status === 'succeeded'
              ? `Merged into ${survivor?.title ?? plan?.survivor.title ?? 'the survivor'}.`
              : run.data.status === 'failed'
                ? run.data.error ?? 'The merge failed.'
                : 'The merge was skipped.'}
          {run.data != null && TERMINAL.has(run.data.status) && (
            <>
              {' '}
              <button
                type="button"
                onClick={() => {
                  reset();
                  if (run.data?.status === 'succeeded') setOpen(false);
                }}
                style={{ border: 'none', background: 'transparent', color: 'var(--accent)', cursor: 'pointer', fontSize: 13, padding: 0 }}
              >
                {run.data.status === 'succeeded' ? 'Done' : 'Try again'}
              </button>
            </>
          )}
        </p>
      )}

      {!merging && runId == null && !survivor && (
        <div>
          <Button variant="ghost" onClick={() => setOpen(false)}>
            Cancel
          </Button>
        </div>
      )}
    </motion.div>
  );
}
