import { useState, type CSSProperties } from 'react';
import { AnimatePresence, motion } from 'framer-motion';
import { ApiError } from '../../api/client';
import type { OrphanKeepItem } from '../../api/types';
import { Button } from '../../ui/Button';
import { MergeIntoPanel } from '../../ui/MergeIntoPanel';
import { NodeChip } from '../../ui/NodeChip';
import { Surface } from '../../ui/Surface';
import { TimeAgo } from '../../ui/TimeAgo';
import { typeIcon, typeLabel } from '../../ui/nodeTypes';
import { useEntityLikeTypes } from '../../ui/useEntityLikeTypes';
import { useReviewNav } from '../review/reviewNav';
import {
  useDeleteNode,
  useKeepNode,
  useOrphanKeeps,
  useRun,
  useUnkeepOrphan,
} from './useActivity';
import { FAIL_COLOR, OK_COLOR, WARN_COLOR } from './statusColors';

// The graph-health card (06 §3, ADR-053 §9 + M9.8 T6, ADR-064 §3): a readout of the LATEST
// graph-health run's findings, read from that run's `details` (no new table). Six checks stay
// **read-only**; the **orphan-nodes** check is now **inline-actionable** — each hub offender can be
// Deleted (T5), Merged away via the shared picker (T3 flow), or Kept/whitelisted (T5.5), with a
// collapsible "Kept (N)" strip below it. Duplicate candidates live in their own sibling card (a
// different run). `runId` is the roster's graph-health `last_run.run_id` (null until it has run).

interface HealthOffender {
  id: string;
  label: string;
  // The orphan-nodes check sets `type` (the node's entity/content kind) so the card can tell a hub
  // (Delete/Merge/Keep) from a content node (a degraded note — no inline capture-remove). Other
  // checks omit it (03-api §graph-health addendum, M9.8 T5.5).
  type: string | null;
}
interface HealthCheck {
  check: string;
  count: number;
  sample: HealthOffender[];
}

const CHECK_ORPHAN_NODES = 'orphan-nodes';
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

const SMALL_BTN: CSSProperties = { padding: '6px 12px', fontSize: 12 };

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
        return {
          id: String(o['id'] ?? ''),
          label: String(o['label'] ?? o['id'] ?? ''),
          type: typeof o['type'] === 'string' ? o['type'] : null,
        };
      }),
    };
  });
}

// The dot + label + count header shared by every check row (read-only and actionable alike).
function CheckHeader({ check }: { check: HealthCheck }) {
  const flagged = check.count > 0;
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
      <span
        aria-hidden
        style={{
          width: 9,
          height: 9,
          borderRadius: '50%',
          background: flagged ? WARN_COLOR : OK_COLOR,
          flex: 'none',
        }}
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
  );
}

const ROW_BOX: CSSProperties = {
  padding: 12,
  borderRadius: 'var(--radius)',
  border: '1px solid var(--surface-border)',
  display: 'grid',
  gap: 6,
};

// A read-only check row: header + a flat wrap of offender chips (M8 behaviour, unchanged).
function CheckRow({ check }: { check: HealthCheck }) {
  return (
    <div style={ROW_BOX}>
      <CheckHeader check={check} />
      {check.count > 0 && check.sample.length > 0 && (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
          {check.sample.map((o) =>
            check.check === REVIEW_AGING_CHECK ? (
              <ReviewOffenderChip key={o.id} id={o.id} label={o.label} />
            ) : (
              // Node-check offenders carry a `nodes.id`; the type isn't in the (non-orphan) payload,
              // so the chip falls back to the neutral glyph and the drawer fills in on open.
              <NodeChip key={o.id} nodeId={o.id} type={o.type} title={o.label} />
            ),
          )}
        </div>
      )}
    </div>
  );
}

// --- Orphan-nodes: the inline-actionable check (M9.8 T6, ADR-064 §3/§5) -------------------------

// The resolved-state line shown once an offender has been acted on in this session — the flagged
// sample lives in a PAST run's details, so we can't drop the row; we mark it settled instead.
function ResolvedLine({ text }: { text: string }) {
  return (
    <span style={{ fontSize: 12.5, color: OK_COLOR, display: 'inline-flex', alignItems: 'center', gap: 6 }}>
      <span aria-hidden>✓</span>
      {text}
    </span>
  );
}

// One orphan **hub** offender: Delete (git-rm the zero-degree hub), Merge (fold a dupe into the real
// hub via the shared picker — the T3 flow), or Keep (whitelist so it stops nagging). Delete confirms
// inline (there's no propose-preview step) and then polls the background run.
function OrphanHubRow({ offender }: { offender: HealthOffender }) {
  const del = useDeleteNode();
  const keep = useKeepNode();
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [deleteRunId, setDeleteRunId] = useState<string | null>(null);
  const deleteRun = useRun(deleteRunId);

  const deleteError = (() => {
    if (del.error instanceof ApiError) {
      if (del.error.status === 409)
        return 'Still has edges — Merge it into the real hub instead.';
      if (del.error.status === 400)
        return 'This is a content node — remove it from the Captures tab.';
      if (del.error.status === 404) return 'Already gone.';
      return del.error.message;
    }
    return del.isError ? 'Couldn’t delete.' : null;
  })();

  const deleted = deleteRun.data?.status === 'succeeded';
  const deleteFailed = deleteRun.data?.status === 'failed';
  const deleting = del.isPending || (deleteRunId != null && !deleted && !deleteFailed);

  return (
    <div style={{ ...ROW_BOX, gap: 10 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
        <span aria-hidden style={{ fontSize: 15 }}>
          {typeIcon(offender.type)}
        </span>
        <NodeChip nodeId={offender.id} type={offender.type} title={offender.label} />
        <span style={{ marginLeft: 'auto', fontSize: 11, color: 'var(--muted)' }}>
          {typeLabel(offender.type)}
        </span>
      </div>

      {keep.isSuccess ? (
        <ResolvedLine text="Kept — it won’t be flagged again." />
      ) : deleted ? (
        <ResolvedLine text="Deleted." />
      ) : (
        <div style={{ display: 'grid', gap: 8 }}>
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
            {confirmDelete ? (
              <>
                <span style={{ fontSize: 12.5, color: WARN_COLOR }}>Delete this node?</span>
                <Button
                  variant="ghost"
                  onClick={() =>
                    del.mutate(offender.id, { onSuccess: (r) => setDeleteRunId(r.run_id) })
                  }
                  disabled={deleting}
                  style={{ ...SMALL_BTN, color: FAIL_COLOR }}
                >
                  {deleting ? 'Deleting…' : 'Confirm delete'}
                </Button>
                {!deleting && (
                  <Button
                    variant="ghost"
                    onClick={() => setConfirmDelete(false)}
                    style={SMALL_BTN}
                  >
                    Cancel
                  </Button>
                )}
              </>
            ) : (
              <>
                <Button
                  variant="ghost"
                  onClick={() => setConfirmDelete(true)}
                  disabled={deleting}
                  style={SMALL_BTN}
                >
                  Delete
                </Button>
                <Button
                  variant="ghost"
                  onClick={() => keep.mutate(offender.id)}
                  disabled={keep.isPending || deleting}
                  style={SMALL_BTN}
                >
                  {keep.isPending ? 'Keeping…' : 'Keep'}
                </Button>
              </>
            )}
          </div>

          {/* Merge reuses the shared "Merge into…" picker (ADR-064 §2, T3): the orphan is the loser
              folded into a real hub the user picks by name. Self-contained (its own preview + run). */}
          {!confirmDelete && !deleting && (
            <MergeIntoPanel
              loser={{ id: offender.id, type: offender.type ?? '', title: offender.label }}
            />
          )}

          {deleteError && (
            <span style={{ fontSize: 12.5, color: FAIL_COLOR }}>{deleteError}</span>
          )}
          {deleteFailed && (
            <span style={{ fontSize: 12.5, color: FAIL_COLOR }}>
              {deleteRun.data?.error ?? 'The delete failed.'}
            </span>
          )}
          {keep.isError && (
            <span style={{ fontSize: 12.5, color: FAIL_COLOR }}>
              {keep.error instanceof ApiError ? keep.error.message : 'Couldn’t keep.'}
            </span>
          )}
        </div>
      )}
    </div>
  );
}

// A content (non-hub) orphan: no inline action — the API carries no node→capture link, so Delete
// would need the Captures tab. Degraded note only (ADR-064 §5, a logged T6 follow-up).
function ContentOrphanRow({ offender }: { offender: HealthOffender }) {
  return (
    <div style={{ ...ROW_BOX, gap: 8 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
        <span aria-hidden style={{ fontSize: 15 }}>
          {typeIcon(offender.type)}
        </span>
        <NodeChip nodeId={offender.id} type={offender.type} title={offender.label} />
        <span style={{ marginLeft: 'auto', fontSize: 11, color: 'var(--muted)' }}>
          {typeLabel(offender.type)}
        </span>
      </div>
      <span style={{ fontSize: 12, color: 'var(--muted)' }}>
        A content node — remove it from the Captures tab.
      </span>
    </div>
  );
}

// The collapsible "Kept (N)" strip: hubs intentionally whitelisted out of the orphan check (T5.5),
// each with Un-keep (keyed on the stable `keep_key`, not node id — survives a reprocess). Rendered
// whenever there are keeps, even when the orphan count is 0 (all orphans kept).
function KeptRow({ keep }: { keep: OrphanKeepItem }) {
  const unkeep = useUnkeepOrphan();
  if (unkeep.isSuccess) return null;
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
      <span aria-hidden style={{ fontSize: 14 }}>
        {typeIcon(keep.type)}
      </span>
      <span style={{ fontSize: 13, fontWeight: 600, overflowWrap: 'anywhere' }}>{keep.label}</span>
      <span style={{ fontSize: 11, color: 'var(--muted)' }}>{typeLabel(keep.type)}</span>
      <div style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 8 }}>
        {unkeep.isError && (
          <span style={{ fontSize: 11.5, color: FAIL_COLOR }}>Couldn’t un-keep.</span>
        )}
        <Button
          variant="ghost"
          onClick={() => unkeep.mutate(keep.key)}
          disabled={unkeep.isPending}
          style={{ padding: '5px 10px', fontSize: 11.5 }}
        >
          {unkeep.isPending ? 'Un-keeping…' : 'Un-keep'}
        </Button>
      </div>
    </div>
  );
}

function KeptStrip() {
  const { data } = useOrphanKeeps();
  const keeps = data ?? [];
  const [open, setOpen] = useState(false);
  if (keeps.length === 0) return null;
  return (
    <div style={{ display: 'grid', gap: 8 }}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 6,
          border: 'none',
          background: 'transparent',
          color: 'var(--muted)',
          cursor: 'pointer',
          fontSize: 12.5,
          fontWeight: 600,
          padding: 0,
        }}
        aria-expanded={open}
      >
        <span aria-hidden style={{ fontSize: 10 }}>
          {open ? '▾' : '▸'}
        </span>
        Kept ({keeps.length})
      </button>
      <AnimatePresence initial={false}>
        {open && (
          <motion.div
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: 'auto' }}
            exit={{ opacity: 0, height: 0 }}
            style={{ overflow: 'hidden' }}
          >
            <div
              style={{
                display: 'grid',
                gap: 10,
                padding: 12,
                borderRadius: 'var(--radius)',
                border: '1px solid var(--surface-border)',
              }}
            >
              {keeps.map((k) => (
                <KeptRow key={k.key} keep={k} />
              ))}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

// The orphan-nodes section: the shared header + per-offender actionable rows (hub vs content) + the
// always-present Kept strip. A hub is any offender whose `type` is entity-like (GET /types).
function OrphanCheckRow({ check }: { check: HealthCheck }) {
  const entityLikeTypes = useEntityLikeTypes();
  // The set is empty only while `GET /types` is still resolving (the vocabulary always has entity
  // seeds), so classifying then would mislabel every hub as a content node and hide its actions.
  // Wait for the types before splitting hub vs content.
  const typesReady = entityLikeTypes.size > 0;
  const hasSample = check.count > 0 && check.sample.length > 0;
  return (
    <div style={{ ...ROW_BOX, gap: 10 }}>
      <CheckHeader check={check} />
      {hasSample &&
        (typesReady ? (
          <div style={{ display: 'grid', gap: 8 }}>
            {check.sample.map((o) =>
              o.type != null && entityLikeTypes.has(o.type) ? (
                <OrphanHubRow key={o.id} offender={o} />
              ) : (
                <ContentOrphanRow key={o.id} offender={o} />
              ),
            )}
          </div>
        ) : (
          <span style={{ fontSize: 12.5, color: 'var(--muted)' }}>Loading node types…</span>
        ))}
      <KeptStrip />
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
        The nightly report — orphans, inbox backlog, review aging, missing dates, alias-less
        entities, tombstone integrity, and stale observations. Orphan hubs are actionable inline.
      </p>

      {runId == null ? (
        <p style={{ margin: 0, fontSize: 13, color: 'var(--muted)' }}>
          No graph-health run yet — run it from the roster above to see the report.
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
            {checks.map((c) =>
              c.check === CHECK_ORPHAN_NODES ? (
                <OrphanCheckRow key={c.check} check={c} />
              ) : (
                <CheckRow key={c.check} check={c} />
              ),
            )}
          </div>
        </>
      )}
    </Surface>
  );
}
