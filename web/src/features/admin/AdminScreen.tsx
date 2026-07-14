import { AnimatePresence, motion } from 'framer-motion';
import { useState } from 'react';
import { ApiError } from '../../api/client';
import type { AgentRunResponse, RunStatus, TagMergeItem } from '../../api/types';
import { Button } from '../../ui/Button';
import { Surface } from '../../ui/Surface';
import { isTerminal, useApplyTags, useBackup, useProposeTags, useReindex, useRun } from './useAdmin';

// Admin tab (06 §6): a lightweight operations panel — Reindex, Backup now, Consolidate tags.
// Each long-running action opens a background run we poll for live status + counts.

const FAIL_COLOR = '#ff6b6b';
const OK_COLOR = '#4ade80';

function statusColor(status: RunStatus): string {
  if (status === 'failed') return FAIL_COLOR;
  if (status === 'succeeded') return OK_COLOR;
  return 'var(--muted)'; // running / skipped
}

function errorText(err: unknown, fallback: string): string {
  return err instanceof ApiError ? err.message : fallback;
}

// Compact numeric/flag readout of a run's `details` (shape varies by agent — reindex vs
// tags-apply), rendered generically so it works for any run without hardcoding keys.
function DetailPills({ details }: { details: Record<string, unknown> }) {
  const pills = Object.entries(details).filter(
    ([, v]) => typeof v === 'number' || v === true,
  );
  if (pills.length === 0) return null;
  return (
    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginTop: 10 }}>
      {pills.map(([k, v]) => (
        <span
          key={k}
          style={{
            fontSize: 11,
            fontWeight: 600,
            color: 'var(--muted)',
            border: '1px solid var(--surface-border)',
            borderRadius: 999,
            padding: '3px 9px',
            fontVariantNumeric: 'tabular-nums',
          }}
        >
          {k} {v === true ? '✓' : (v as number)}
        </span>
      ))}
    </div>
  );
}

// Live status of a background run (reindex / tags-apply); polls until terminal.
function RunPanel({ run }: { run: AgentRunResponse | undefined }) {
  if (!run) return null;
  const running = !isTerminal(run.status);
  return (
    <motion.div
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      style={{ marginTop: 14, paddingTop: 14, borderTop: '1px solid var(--surface-border)' }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        {running ? (
          <motion.span
            aria-hidden
            animate={{ opacity: [1, 0.25, 1] }}
            transition={{ duration: 1.1, repeat: Infinity, ease: 'easeInOut' }}
            style={{ width: 8, height: 8, borderRadius: '50%', background: 'var(--muted)' }}
          />
        ) : (
          <span aria-hidden style={{ color: statusColor(run.status) }}>
            {run.status === 'succeeded' ? '✓' : run.status === 'failed' ? '✕' : '—'}
          </span>
        )}
        <span
          style={{
            fontSize: 12,
            fontWeight: 700,
            letterSpacing: 0.4,
            textTransform: 'uppercase',
            color: statusColor(run.status),
          }}
        >
          {run.status}
        </span>
      </div>
      {run.summary && (
        <p style={{ margin: '8px 0 0', fontSize: 13, color: 'var(--text)', lineHeight: 1.5 }}>
          {run.summary}
        </p>
      )}
      {run.error && (
        <p style={{ margin: '8px 0 0', fontSize: 13, color: FAIL_COLOR, lineHeight: 1.5 }}>
          {run.error}
        </p>
      )}
      <DetailPills details={run.details} />
    </motion.div>
  );
}

function AdminCard({
  title,
  description,
  children,
}: {
  title: string;
  description: string;
  children: React.ReactNode;
}) {
  return (
    <Surface>
      <h2 style={{ margin: '0 0 6px', fontSize: 16 }}>{title}</h2>
      <p style={{ margin: '0 0 16px', fontSize: 13, color: 'var(--muted)', lineHeight: 1.5 }}>
        {description}
      </p>
      {children}
    </Surface>
  );
}

function ReindexCard() {
  const reindex = useReindex();
  const [runId, setRunId] = useState<string | null>(null);
  const run = useRun(runId);
  const active = run.data != null && !isTerminal(run.data.status);

  const start = () =>
    reindex.mutate(undefined, { onSuccess: (r) => setRunId(r.run_id) });

  return (
    <AdminCard
      title="Reindex"
      description="Rescan the graph store, re-materialize canonical edges, recompute similarity, and push — the full reconciliation pass."
    >
      <Button onClick={start} disabled={reindex.isPending || active}>
        {reindex.isPending ? 'Starting…' : active ? 'Running…' : 'Reindex now'}
      </Button>
      {reindex.isError && (
        <p style={{ margin: '12px 0 0', fontSize: 13, color: FAIL_COLOR }}>
          {reindex.error instanceof ApiError && reindex.error.status === 409
            ? 'A reindex is already running.'
            : errorText(reindex.error, 'Couldn’t start the reindex.')}
        </p>
      )}
      <AnimatePresence>{runId && <RunPanel run={run.data} />}</AnimatePresence>
    </AdminCard>
  );
}

function BackupCard() {
  const backup = useBackup();
  return (
    <AdminCard
      title="Backup now"
      description="Force an immediate graph-store commit and push, ahead of the debounced and nightly backups."
    >
      <Button variant="ghost" onClick={() => backup.mutate()} disabled={backup.isPending}>
        {backup.isPending ? 'Backing up…' : 'Back up now'}
      </Button>
      {backup.isError && (
        <p style={{ margin: '12px 0 0', fontSize: 13, color: FAIL_COLOR }}>
          {errorText(backup.error, 'Backup failed.')}
        </p>
      )}
      {backup.isSuccess && (
        <p style={{ margin: '12px 0 0', fontSize: 13, color: OK_COLOR }}>
          {backup.data.committed
            ? `Committed and ${backup.data.pushed ? 'pushed to remote.' : 'saved locally (push pending).'}`
            : 'Nothing to commit — already up to date.'}
        </p>
      )}
    </AdminCard>
  );
}

function MergeReview({
  merges,
  onApply,
  onDiscard,
  applying,
}: {
  merges: TagMergeItem[];
  onApply: () => void;
  onDiscard: () => void;
  applying: boolean;
}) {
  if (merges.length === 0) {
    return (
      <p style={{ margin: '12px 0 0', fontSize: 13, color: 'var(--muted)' }}>
        Nothing to consolidate — your tags are already tidy.
      </p>
    );
  }
  return (
    <div style={{ marginTop: 14 }}>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
        {merges.map((m) => (
          <div
            key={m.canonical}
            style={{
              padding: 12,
              borderRadius: 'var(--radius)',
              border: '1px solid var(--surface-border)',
            }}
          >
            <span style={{ fontSize: 13, fontWeight: 700, color: 'var(--accent)' }}>
              #{m.canonical}
            </span>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginTop: 8 }}>
              {m.variants.map((v) => (
                <span
                  key={v}
                  style={{
                    fontSize: 11,
                    color: 'var(--muted)',
                    textDecoration: 'line-through',
                    border: '1px solid var(--surface-border)',
                    borderRadius: 999,
                    padding: '2px 8px',
                  }}
                >
                  #{v}
                </span>
              ))}
            </div>
          </div>
        ))}
      </div>
      <div style={{ display: 'flex', gap: 10, marginTop: 14 }}>
        <Button onClick={onApply} disabled={applying}>
          {applying ? 'Applying…' : `Apply ${merges.length} merge${merges.length > 1 ? 's' : ''}`}
        </Button>
        <Button variant="ghost" onClick={onDiscard} disabled={applying}>
          Discard
        </Button>
      </div>
    </div>
  );
}

function ConsolidateTagsCard() {
  const propose = useProposeTags();
  const apply = useApplyTags();
  const [merges, setMerges] = useState<TagMergeItem[] | null>(null);
  const [runId, setRunId] = useState<string | null>(null);
  const run = useRun(runId);

  const startPropose = () => {
    setRunId(null);
    propose.mutate(undefined, { onSuccess: (r) => setMerges(r.merges) });
  };

  const doApply = () => {
    if (!merges) return;
    apply.mutate(merges, {
      onSuccess: (r) => {
        setRunId(r.run_id);
        setMerges(null);
      },
    });
  };

  const proposeUnavailable =
    propose.error instanceof ApiError && propose.error.status === 503;

  return (
    <AdminCard
      title="Consolidate tags"
      description="Propose a merge plan that folds near-duplicate tags together, review it, then apply — rewriting the affected nodes."
    >
      <Button variant="ghost" onClick={startPropose} disabled={propose.isPending}>
        {propose.isPending ? 'Proposing…' : merges != null ? 'Re-propose' : 'Propose merges'}
      </Button>
      {propose.isError && (
        <p style={{ margin: '12px 0 0', fontSize: 13, color: FAIL_COLOR }}>
          {proposeUnavailable
            ? 'Tag consolidation is unavailable right now (model).'
            : errorText(propose.error, 'Couldn’t build a proposal.')}
        </p>
      )}
      {merges != null && (
        <MergeReview
          merges={merges}
          onApply={doApply}
          onDiscard={() => setMerges(null)}
          applying={apply.isPending}
        />
      )}
      {apply.isError && (
        <p style={{ margin: '12px 0 0', fontSize: 13, color: FAIL_COLOR }}>
          {errorText(apply.error, 'Couldn’t apply the merges.')}
        </p>
      )}
      <AnimatePresence>{runId && <RunPanel run={run.data} />}</AnimatePresence>
    </AdminCard>
  );
}

export function AdminScreen() {
  return (
    <div style={{ display: 'grid', gap: 16 }}>
      <h1 style={{ margin: 0, fontSize: 24, fontWeight: 700, letterSpacing: -0.4 }}>Admin</h1>
      <ReindexCard />
      <BackupCard />
      <ConsolidateTagsCard />
    </div>
  );
}
