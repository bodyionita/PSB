import { useEffect, useState, type CSSProperties } from 'react';
import { AnimatePresence, motion } from 'framer-motion';
import { ApiError } from '../../api/client';
import type { DedupCandidate, EntityMergeProposeResponse } from '../../api/types';
import { Button } from '../../ui/Button';
import { Surface } from '../../ui/Surface';
import { TimeAgo } from '../../ui/TimeAgo';
import { typeIcon } from '../../ui/nodeTypes';
import { useReviewNav } from '../review/reviewNav';
import { useMergeEntitiesApply, useMergeEntitiesPropose, useRun } from './useActivity';
import { useResolvedRunItems } from './useResolvedRunItems';
import { FAIL_COLOR, OK_COLOR } from './statusColors';

// The duplicate-candidates card (M9.8 T6, ADR-064 §3/§4): the conservative entity-hub dedup
// detector's **high-confidence** pairs, read off the LATEST `entity-dedup` run's
// `details.high_confidence[]` (the same run-details mechanism the graph-health card uses). Each pair
// pre-fills a one-click Merge (survivor = the higher-degree hub kept; loser folded away) through the
// shared two-step propose→apply — no picker, both sides known. Lower-confidence pairs T4 filed to
// Review, so the card links there. `runId` is the roster's `entity-dedup` last_run.run_id.

const pillStyle: CSSProperties = {
  fontSize: 11,
  fontWeight: 600,
  color: 'var(--muted)',
  border: '1px solid var(--surface-border)',
  borderRadius: 999,
  padding: '3px 9px',
  fontVariantNumeric: 'tabular-nums',
};

interface Parsed {
  candidates: DedupCandidate[];
  lowConfidenceFiled: number;
}

function parseDedup(details: Record<string, unknown> | undefined): Parsed {
  const rawHc = Array.isArray(details?.['high_confidence'])
    ? (details!['high_confidence'] as unknown[])
    : [];
  const candidates: DedupCandidate[] = [];
  for (const r of rawHc) {
    const o = r as Record<string, unknown>;
    const survivor = o['survivor'] as Record<string, unknown> | undefined;
    const loser = o['loser'] as Record<string, unknown> | undefined;
    if (!survivor?.['id'] || !loser?.['id']) continue;
    const signals = (o['signals'] ?? {}) as Record<string, unknown>;
    const nameMatch = (signals['name_match'] ?? {}) as Record<string, unknown>;
    candidates.push({
      survivor: { id: String(survivor['id']), title: (survivor['title'] as string) ?? null },
      loser: { id: String(loser['id']), title: (loser['title'] as string) ?? null },
      type: typeof o['type'] === 'string' ? (o['type'] as string) : '',
      shared_count: typeof signals['shared_count'] === 'number' ? (signals['shared_count'] as number) : 0,
      name_match_kind: typeof nameMatch['kind'] === 'string' ? (nameMatch['kind'] as string) : '',
    });
  }
  const low = details?.['low_confidence_filed'];
  return { candidates, lowConfidenceFiled: typeof low === 'number' ? low : 0 };
}

// One high-confidence pair: a one-click Merge that runs the shared propose (inbound-edge inventory)
// → confirm → apply, pre-filled with the detector's survivor/loser (apply uses the server-
// authoritative ids from the proposal, mirroring the AdminOps merge card).
function DuplicateCandidateRow({
  candidate,
  resolved,
  onMerged,
}: {
  candidate: DedupCandidate;
  // Durable per-run resolution (T7 fix): a merged pair stays settled across remount instead of
  // re-showing its Merge button (or degrading to a 404 on re-propose) until the next dedup run.
  resolved: boolean;
  onMerged: () => void;
}) {
  const propose = useMergeEntitiesPropose();
  const apply = useMergeEntitiesApply();
  const [plan, setPlan] = useState<EntityMergeProposeResponse | null>(null);
  const [runId, setRunId] = useState<string | null>(null);
  const run = useRun(runId);

  const runSucceeded = run.data?.status === 'succeeded';
  const merged = resolved || runSucceeded;
  const failed = run.data?.status === 'failed';

  useEffect(() => {
    if (runSucceeded) onMerged();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runSucceeded]);
  const merging = apply.isPending || (runId != null && !merged && !failed);

  const survivorName = candidate.survivor.title ?? candidate.survivor.id;
  const loserName = candidate.loser.title ?? candidate.loser.id;

  return (
    <div
      style={{
        padding: 12,
        borderRadius: 'var(--radius)',
        border: '1px solid var(--surface-border)',
        display: 'grid',
        gap: 8,
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
        <span aria-hidden style={{ fontSize: 15 }}>
          {typeIcon(candidate.type || null)}
        </span>
        <span style={{ fontSize: 13, overflowWrap: 'anywhere' }}>
          <b style={{ color: 'var(--text)' }}>{loserName}</b>
          <span style={{ color: 'var(--muted)' }}> → </span>
          <b style={{ color: 'var(--text)' }}>{survivorName}</b>
        </span>
      </div>
      <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
        <span style={pillStyle}>
          {candidate.shared_count} shared neighbour{candidate.shared_count === 1 ? '' : 's'}
        </span>
        {candidate.name_match_kind && <span style={pillStyle}>{candidate.name_match_kind}</span>}
      </div>

      {merged ? (
        <span style={{ fontSize: 12.5, color: OK_COLOR, display: 'inline-flex', gap: 6 }}>
          <span aria-hidden>✓</span>
          Merged into {survivorName}.
        </span>
      ) : plan == null ? (
        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          <Button
            variant="ghost"
            onClick={() =>
              propose.mutate(
                { loser: candidate.loser.id, survivor: candidate.survivor.id },
                { onSuccess: (p) => setPlan(p) },
              )
            }
            disabled={propose.isPending}
            style={{ padding: '6px 12px', fontSize: 12 }}
          >
            {propose.isPending ? 'Checking…' : 'Merge'}
          </Button>
          {propose.isError && (
            <span style={{ fontSize: 12.5, color: FAIL_COLOR }}>
              {propose.error instanceof ApiError && propose.error.status === 404
                ? 'One of these was already merged away.'
                : 'Couldn’t preview the merge.'}
            </span>
          )}
        </div>
      ) : (
        <div style={{ display: 'grid', gap: 8 }}>
          <span style={pillStyle}>
            {plan.inbound_count} inbound edge{plan.inbound_count === 1 ? '' : 's'} → {survivorName}
          </span>
          <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            <Button
              onClick={() =>
                apply.mutate(
                  { loser: plan.loser.id, survivor: plan.survivor.id },
                  { onSuccess: (r) => setRunId(r.run_id) },
                )
              }
              disabled={merging}
              style={{ padding: '6px 12px', fontSize: 12 }}
            >
              {merging ? 'Merging…' : 'Confirm merge'}
            </Button>
            {!merging && (
              <Button
                variant="ghost"
                onClick={() => setPlan(null)}
                style={{ padding: '6px 12px', fontSize: 12 }}
              >
                Cancel
              </Button>
            )}
          </div>
          {(apply.isError || failed) && (
            <span style={{ fontSize: 12.5, color: FAIL_COLOR }}>
              {failed ? (run.data?.error ?? 'The merge failed.') : 'Couldn’t apply the merge.'}
            </span>
          )}
        </div>
      )}
    </div>
  );
}

// The lower-confidence tail: T4 already filed these to the Review queue (`entity-dedup` kind), so we
// just count them and link into Review (no single item to seed-highlight).
function LowConfidenceLink({ count }: { count: number }) {
  const nav = useReviewNav();
  if (count === 0) return null;
  const text = `${count} lower-confidence pair${count === 1 ? '' : 's'} filed to Review`;
  if (!nav?.openReview) {
    return <p style={{ margin: 0, fontSize: 12.5, color: 'var(--muted)' }}>{text}.</p>;
  }
  return (
    <button
      type="button"
      onClick={() => nav.openReview!()}
      style={{
        alignSelf: 'start',
        border: 'none',
        background: 'transparent',
        color: 'var(--accent)',
        cursor: 'pointer',
        fontSize: 12.5,
        fontWeight: 600,
        padding: 0,
      }}
    >
      {text} →
    </button>
  );
}

export function DuplicateCandidatesCard({ runId }: { runId: string | null }) {
  const run = useRun(runId);
  const { candidates, lowConfidenceFiled } = parseDedup(run.data?.details);
  const resolved = useResolvedRunItems('dedup-candidates', runId);

  return (
    <Surface>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, flexWrap: 'wrap' }}>
        <h2 style={{ margin: 0, fontSize: 16 }}>Duplicate candidates</h2>
        {run.data?.finished_at && (
          <span style={{ fontSize: 12, color: 'var(--muted)' }}>
            checked <TimeAgo iso={run.data.finished_at} />
          </span>
        )}
      </div>
      <p style={{ margin: '6px 0 16px', fontSize: 13, color: 'var(--muted)', lineHeight: 1.5 }}>
        Likely-duplicate entity hubs the nightly detector paired by name and shared neighbourhood.
        Merge the clear ones inline; the rest wait in Review.
      </p>

      {runId == null ? (
        <p style={{ margin: 0, fontSize: 13, color: 'var(--muted)' }}>
          No dedup run yet — run <b>entity-dedup</b> from the roster above.
        </p>
      ) : run.isLoading ? (
        <p style={{ margin: 0, fontSize: 13, color: 'var(--muted)' }}>Loading the latest run…</p>
      ) : candidates.length === 0 && lowConfidenceFiled === 0 ? (
        <p style={{ margin: 0, fontSize: 13, color: OK_COLOR }}>
          No duplicate candidates — the hub set looks clean.
        </p>
      ) : (
        <div style={{ display: 'grid', gap: 10 }}>
          <AnimatePresence initial={false}>
            {candidates.map((c) => (
              <motion.div key={`${c.loser.id}:${c.survivor.id}`} layout>
                <DuplicateCandidateRow
                  candidate={c}
                  resolved={resolved.statusOf(c.loser.id) === 'merged'}
                  onMerged={() => resolved.mark(c.loser.id, 'merged')}
                />
              </motion.div>
            ))}
          </AnimatePresence>
          <LowConfidenceLink count={lowConfidenceFiled} />
        </div>
      )}
    </Surface>
  );
}
