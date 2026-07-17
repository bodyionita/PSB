import type { CSSProperties } from 'react';
import { NodeChip } from '../../ui/NodeChip';
import { Surface } from '../../ui/Surface';
import { TimeAgo } from '../../ui/TimeAgo';
import { useReviewNav } from '../review/reviewNav';
import { useRun } from './useActivity';
import { OK_COLOR, WARN_COLOR } from './statusColors';

// The graph-health card (06 §3, ADR-053 §9): a read-only readout of the LATEST graph-health run's
// findings, read from that run's `details` (no new table). `runId` is the roster's graph-health
// `last_run.run_id` (null until it has ever run). Seven checks, each `{check, count, sample}` — a
// non-zero count is a flag (amber); all-zero is a clean bill of health (green). Read-only in M8 —
// acting on a flag is M10.

interface HealthOffender {
  id: string;
  label: string;
}
interface HealthCheck {
  check: string;
  count: number;
  sample: HealthOffender[];
}

// The one non-node check: its offenders are review-queue ids (not nodes), so they deep-link into the
// Review tab instead of opening a NodePreview (ADR-054 §5 replan; T1 carve-out). Every other check's
// offenders carry a `nodes.id` → clickable NodeChip.
const REVIEW_AGING_CHECK = 'pending-review-aging';

const OFFENDER_PILL: CSSProperties = {
  fontSize: 11,
  color: 'var(--muted)',
  border: '1px solid var(--surface-border)',
  borderRadius: 999,
  padding: '2px 8px',
  maxWidth: '100%',
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap',
};

// A graph-health aging-review offender — a review-queue id, so it jumps into the Review tab and
// highlights the item (not a node preview). Degrades to a static pill outside a ReviewNav provider.
function ReviewOffenderChip({ id, label }: { id: string; label: string }) {
  const nav = useReviewNav();
  if (!nav) {
    return (
      <span title={id} style={OFFENDER_PILL}>
        {label}
      </span>
    );
  }
  return (
    <button
      type="button"
      title={id}
      onClick={() => nav.openReviewItem(id)}
      style={{ ...OFFENDER_PILL, cursor: 'pointer', textAlign: 'left' }}
    >
      {label}
    </button>
  );
}

// Friendly labels for the stable check keys (graph_health.py). Kept here (presentation) rather than
// derived — the server ships raw keys.
const CHECK_LABELS: Record<string, string> = {
  'orphan-nodes': 'Orphan nodes',
  'inbox-depth': 'Unorganized (inbox) backlog',
  'pending-review-aging': 'Aging review items',
  'memories-missing-occurred': 'Memories missing a date',
  'alias-less-entities': 'Entities without aliases',
  'tombstone-integrity': 'Dangling tombstones',
  'stale-observations': 'Stale profile observations',
};

function parseChecks(details: Record<string, unknown> | undefined): HealthCheck[] {
  const raw = details?.['checks'];
  if (!Array.isArray(raw)) return [];
  return raw.map((c) => {
    const obj = c as Record<string, unknown>;
    const sampleRaw = Array.isArray(obj['sample']) ? (obj['sample'] as unknown[]) : [];
    return {
      check: String(obj['check'] ?? ''),
      count: typeof obj['count'] === 'number' ? obj['count'] : 0,
      sample: sampleRaw.map((s) => {
        const o = s as Record<string, unknown>;
        return { id: String(o['id'] ?? ''), label: String(o['label'] ?? o['id'] ?? '') };
      }),
    };
  });
}

function CheckRow({ check }: { check: HealthCheck }) {
  const flagged = check.count > 0;
  const dot = flagged ? WARN_COLOR : OK_COLOR;
  return (
    <div
      style={{
        padding: 12,
        borderRadius: 'var(--radius)',
        border: '1px solid var(--surface-border)',
        display: 'grid',
        gap: 6,
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
        <span
          aria-hidden
          style={{ width: 9, height: 9, borderRadius: '50%', background: dot, flex: 'none' }}
        />
        <span style={{ fontSize: 14, fontWeight: 600 }}>
          {CHECK_LABELS[check.check] ?? check.check}
        </span>
        <span
          style={{
            marginLeft: 'auto',
            fontSize: 13,
            fontWeight: 700,
            fontVariantNumeric: 'tabular-nums',
            color: flagged ? WARN_COLOR : 'var(--muted)',
          }}
        >
          {check.count}
        </span>
      </div>
      {flagged && check.sample.length > 0 && (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
          {check.sample.map((o) =>
            check.check === REVIEW_AGING_CHECK ? (
              <ReviewOffenderChip key={o.id} id={o.id} label={o.label} />
            ) : (
              // Node-check offenders carry a `nodes.id` (uuid) — the label is the node's title (or
              // store path); the type isn't in the health payload, so the chip falls back to the
              // neutral glyph and the drawer fills in the real detail on open.
              <NodeChip key={o.id} nodeId={o.id} type={null} title={o.label} />
            ),
          )}
        </div>
      )}
    </div>
  );
}

export function GraphHealthCard({ runId }: { runId: string | null }) {
  const run = useRun(runId);
  const checks = parseChecks(run.data?.details);
  const flagged = checks.filter((c) => c.count > 0).length;

  return (
    <Surface>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, flexWrap: 'wrap' }}>
        <h2 style={{ margin: 0, fontSize: 16 }}>Graph health</h2>
        {run.data?.finished_at && (
          <span style={{ fontSize: 12, color: 'var(--muted)' }}>
            checked <TimeAgo iso={run.data.finished_at} />
          </span>
        )}
      </div>
      <p style={{ margin: '6px 0 16px', fontSize: 13, color: 'var(--muted)', lineHeight: 1.5 }}>
        The nightly read-only report — orphans, inbox backlog, review aging, missing dates, alias-less
        entities, tombstone integrity, and stale observations.
      </p>

      {runId == null ? (
        <p style={{ margin: 0, fontSize: 13, color: 'var(--muted)' }}>
          No graph-health run yet — run it from the roster below to see the report.
        </p>
      ) : run.isLoading ? (
        <p style={{ margin: 0, fontSize: 13, color: 'var(--muted)' }}>Loading the latest report…</p>
      ) : checks.length === 0 ? (
        <p style={{ margin: 0, fontSize: 13, color: 'var(--muted)' }}>
          The latest run recorded no findings.
        </p>
      ) : (
        <>
          <p style={{ margin: '0 0 12px', fontSize: 13, color: flagged ? WARN_COLOR : OK_COLOR }}>
            {flagged === 0
              ? 'All clear — no checks flagged.'
              : `${flagged} of ${checks.length} check${checks.length > 1 ? 's' : ''} flagged.`}
          </p>
          <div style={{ display: 'grid', gap: 10 }}>
            {checks.map((c) => (
              <CheckRow key={c.check} check={c} />
            ))}
          </div>
        </>
      )}
    </Surface>
  );
}
