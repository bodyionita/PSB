import { AnimatePresence, motion } from 'framer-motion';
import { useEffect, useMemo, useState } from 'react';
import type {
  ActivityCategory,
  ActivityFeedItem,
  CaptureView,
  RunChildItem,
  RunDerivePart,
} from '../../api/types';
import { useCapture, useEditCaptureAnchor } from '../capture/useCaptures';
import { Button } from '../../ui/Button';
import { CaptureDetailBody } from '../../ui/media/CaptureDetail';
import { TimeAgo } from '../../ui/TimeAgo';
import { RunLogTail } from './RunLogTail';
import { StatusBadge } from './runStatus';
import { FAIL_COLOR, WARN_COLOR } from './statusColors';
import {
  useActivityFeed,
  usePipelines,
  useRemoveCapture,
  useReviewItem,
  useRun,
} from './useActivity';

// The Feed — "what did my brain do" (06 §3, ADR-053 §4; M8.1 ADR-054 §2/§4): the merged GET
// /activity as three categorized tabs (agents & jobs · captures · manual actions), newest-first
// keyset infinite scroll. An agent-run row expands to its recursive step subtree
// (GET /activity/runs/{id}, depth-indented, early→late); a capture row expands to its full detail
// (GET /captures/{id}: raw text + node chips + source badge) and, for a chat-sourced capture,
// carries the one-tap Remove that folds in the M6 auto-recorded control.

const KIND_AGENT_RUN = 'agent_run';
const KIND_CAPTURE = 'capture';
const KIND_REVIEW_VERDICT = 'review_verdict';

const TABS: { id: ActivityCategory; label: string }[] = [
  { id: 'agents_jobs', label: 'Agents & jobs' },
  { id: 'captures', label: 'Captures' },
  { id: 'manual_actions', label: 'Manual actions' },
];

function tabEmptyText(category: ActivityCategory): string {
  if (category === 'captures') return 'No captures yet.';
  if (category === 'manual_actions') return 'No manual actions yet.';
  return 'No scheduled runs yet.';
}

// A capture row's `title` is always null server-side (03-api §Activity) — the row headline is
// derived from its source badge instead. `chat` keeps the pre-M8.1 "Recorded from a conversation"
// phrasing (the one case with an established, warmer voice); the rest are plain and factual.
function captureHeadline(source: string | null): string {
  switch (source) {
    case 'chat':
      return 'Recorded from a conversation';
    case 'voice':
      return 'Voice capture';
    case 'text':
      return 'Text capture';
    case 'mcp':
      return 'MCP capture';
    default:
      return 'Capture';
  }
}

// --- Expanded agent-run detail: status/summary/error + the recursive step subtree --------------

// One node of the recursive `children[]` tree (M8.1 ADR-054 §2), depth-indented — a distiller
// step's spawned `capture` runs sit one level deeper, and since the tree is genuinely recursive
// (not client-grouped), any future deeper nesting renders its true depth for free.
function RunChildRow({ child, depth }: { child: RunChildItem; depth: number }) {
  return (
    <div style={{ display: 'grid', gap: 4 }}>
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 8,
          flexWrap: 'wrap',
          paddingLeft: depth * 16,
        }}
      >
        <span aria-hidden style={{ color: 'var(--muted)', fontSize: 11 }}>
          ↳
        </span>
        <StatusBadge status={child.status} />
        <span style={{ fontSize: 13, fontWeight: 600 }}>{child.name}</span>
        {child.ts && (
          <TimeAgo iso={child.ts} style={{ marginLeft: 'auto', fontSize: 11, color: 'var(--muted)' }} />
        )}
      </div>
      {child.summary && (
        <p
          style={{
            margin: 0,
            minWidth: 0,
            paddingLeft: depth * 16 + 19,
            fontSize: 12,
            color: 'var(--muted)',
            lineHeight: 1.4,
            overflowWrap: 'anywhere',
          }}
        >
          {child.summary}
        </p>
      )}
      {child.children.map((c) => (
        <RunChildRow key={c.id} child={c} depth={depth + 1} />
      ))}
    </div>
  );
}

// The per-part derivation records off an agent run's opaque `details.derive.parts[]` (M9.7 C,
// ADR-061 §7/§10) — present for a composite capture, absent otherwise. Narrowed defensively: any
// non-array (a single image/voice's scalar `derive` blob, an old run, a missing key) → [].
function deriveParts(details: Record<string, unknown>): RunDerivePart[] {
  const derive = details.derive as { parts?: unknown } | null | undefined;
  const parts = derive?.parts;
  return Array.isArray(parts) ? (parts as RunDerivePart[]) : [];
}

function derivePartColor(status: string | undefined): string {
  if (status === 'unavailable') return FAIL_COLOR;
  if (status === 'pending') return WARN_COLOR;
  return 'var(--muted)'; // derived / unknown
}

// The structured per-part block (M9.7 C): each composite part's terminal derivation — its 1-based
// position, kind, status, model, retry count, and any error — rendered after the run finishes (the
// live `RunLogTail` above streams the same milestones while it runs).
function DerivePartsBlock({ parts }: { parts: RunDerivePart[] }) {
  return (
    <div
      style={{
        display: 'grid',
        gap: 6,
        paddingTop: 8,
        borderTop: '1px solid var(--surface-border)',
      }}
    >
      <span
        style={{
          fontSize: 11,
          fontWeight: 700,
          letterSpacing: 0.3,
          textTransform: 'uppercase',
          color: 'var(--muted)',
        }}
      >
        Parts
      </span>
      {parts.map((p, i) => (
        <div
          key={p.media_id ?? i}
          style={{
            display: 'flex',
            gap: 8,
            alignItems: 'baseline',
            flexWrap: 'wrap',
            fontSize: 12,
            fontVariantNumeric: 'tabular-nums',
          }}
        >
          <span style={{ fontWeight: 600, color: 'var(--text)' }}>
            {p.marker_index ?? i + 1} · {p.kind ?? 'part'}
          </span>
          <span style={{ fontWeight: 600, color: derivePartColor(p.status) }}>{p.status ?? '—'}</span>
          {p.model && <span style={{ color: 'var(--muted)' }}>via {p.model}</span>}
          {typeof p.attempts === 'number' && p.attempts > 1 && (
            <span style={{ color: 'var(--muted)' }}>{p.attempts} attempts</span>
          )}
          {p.error && (
            <span style={{ color: FAIL_COLOR, minWidth: 0, overflowWrap: 'anywhere' }}>
              {p.error}
            </span>
          )}
        </div>
      ))}
    </div>
  );
}

function RunDetail({ runId }: { runId: string }) {
  const run = useRun(runId);
  // Latch the live log tail (M9.7 C, ADR-061 §10) on once the run is seen running, and keep it
  // mounted after it finishes so `RunLogTail` can drain the async on-finish flush (ADR-053 §2 — the
  // OpsView pattern). A terminal run opened cold (an old feed row) never latches, so historical
  // rows stay quiet — the tail is for a run followed live.
  const [showTail, setShowTail] = useState(false);
  useEffect(() => {
    if (run.data?.status === 'running') setShowTail(true);
  }, [run.data?.status]);

  if (run.isLoading) return <span style={{ fontSize: 12, color: 'var(--muted)' }}>Loading…</span>;
  if (run.isError || !run.data)
    return <span style={{ fontSize: 12, color: FAIL_COLOR }}>Couldn’t load run detail.</span>;
  const pills = Object.entries(run.data.details).filter(
    ([, v]) => typeof v === 'number' || v === true,
  );
  const parts = deriveParts(run.data.details);
  return (
    <div style={{ display: 'grid', gap: 8 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
        <StatusBadge status={run.data.status} />
        {run.data.fallback_used && (
          <span
            style={{
              fontSize: 11,
              fontWeight: 700,
              color: '#f5a623',
              border: '1px solid #f5a623',
              borderRadius: 999,
              padding: '1px 8px',
            }}
          >
            fallback{run.data.model_used ? ` · ${run.data.model_used}` : ''}
          </span>
        )}
      </div>
      {run.data.summary && (
        <p style={{ margin: 0, minWidth: 0, fontSize: 13, color: 'var(--text)', lineHeight: 1.5, overflowWrap: 'anywhere' }}>
          {run.data.summary}
        </p>
      )}
      {run.data.error && (
        <p style={{ margin: 0, minWidth: 0, fontSize: 13, color: FAIL_COLOR, lineHeight: 1.5, overflowWrap: 'anywhere' }}>
          {run.data.error}
        </p>
      )}
      {pills.length > 0 && (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
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
      )}
      {showTail && <RunLogTail runId={runId} />}
      {parts.length > 0 && <DerivePartsBlock parts={parts} />}
      {run.data.children.length > 0 && (
        <div
          style={{
            display: 'grid',
            gap: 8,
            paddingTop: 8,
            borderTop: '1px solid var(--surface-border)',
          }}
        >
          {run.data.children.map((c) => (
            <RunChildRow key={c.id} child={c} depth={0} />
          ))}
        </div>
      )}
    </div>
  );
}

// --- Expanded capture detail: full raw text + node chips + source badge -------------------------

function SourceBadge({ source }: { source: string | null }) {
  return (
    <span
      style={{
        fontSize: 11,
        fontWeight: 700,
        letterSpacing: 0.3,
        textTransform: 'uppercase',
        color: 'var(--muted)',
        border: '1px solid var(--surface-border)',
        borderRadius: 999,
        padding: '2px 8px',
      }}
    >
      {source ?? 'web'}
    </span>
  );
}

// An ISO instant → the `datetime-local` input value (local `YYYY-MM-DDThh:mm`, no seconds/zone).
function toLocalInput(iso: string): string {
  const d = new Date(iso);
  const p = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
}

// The anchor-edit affordance (M8.2, ADR-056 §5): correct when a capture was recorded. Overwriting
// the stored anchor triggers a background one-capture reorganize that re-resolves every relative
// date ("10 days ago") against the new anchor — the P10-deterministic replay. Voice + text alike.
function AnchorEditor({ capture }: { capture: CaptureView }) {
  const edit = useEditCaptureAnchor();
  const [open, setOpen] = useState(false);
  const [value, setValue] = useState('');
  const [done, setDone] = useState(false);

  if (!capture.created_at) return null;

  const startEditing = () => {
    setValue(toLocalInput(capture.created_at as string));
    setDone(false);
    setOpen(true);
    edit.reset();
  };

  const save = () => {
    if (!value) return;
    const iso = new Date(value).toISOString();
    edit.mutate(
      { id: capture.capture_id, anchor: iso },
      {
        onSuccess: () => {
          setOpen(false);
          setDone(true);
        },
      },
    );
  };

  return (
    <div style={{ display: 'grid', gap: 8 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
        <span style={{ fontSize: 12, color: 'var(--muted)' }}>Recorded</span>
        <TimeAgo iso={capture.created_at} style={{ fontSize: 12, color: 'var(--text)' }} />
        {!open && (
          <button
            type="button"
            onClick={startEditing}
            style={{
              background: 'none',
              border: 'none',
              padding: 0,
              fontSize: 12,
              fontWeight: 600,
              color: 'var(--accent)',
              cursor: 'pointer',
            }}
          >
            Edit time
          </button>
        )}
      </div>

      {open && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
          <input
            type="datetime-local"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            aria-label="Corrected recorded-at"
            style={{
              padding: '7px 10px',
              borderRadius: 'var(--radius)',
              border: '1px solid var(--surface-border)',
              background: 'var(--surface)',
              color: 'var(--text)',
              fontSize: 13,
              outline: 'none',
            }}
          />
          <Button
            onClick={save}
            disabled={edit.isPending || !value}
            style={{ padding: '7px 14px', fontSize: 13 }}
          >
            {edit.isPending ? 'Saving…' : 'Save'}
          </Button>
          <Button
            variant="ghost"
            onClick={() => setOpen(false)}
            disabled={edit.isPending}
            style={{ padding: '7px 14px', fontSize: 13 }}
          >
            Cancel
          </Button>
        </div>
      )}

      {edit.isError && (
        <p style={{ margin: 0, fontSize: 12, color: FAIL_COLOR }}>Couldn’t update the time — try again.</p>
      )}
      {done && (
        <p style={{ margin: 0, fontSize: 12, color: 'var(--muted)' }}>
          Re-resolving this capture’s dates in the background…
        </p>
      )}
    </div>
  );
}

function CaptureDetail({ captureId }: { captureId: string }) {
  const capture = useCapture(captureId);
  if (capture.isLoading)
    return <span style={{ fontSize: 12, color: 'var(--muted)' }}>Loading…</span>;
  if (capture.isError || !capture.data)
    return <span style={{ fontSize: 12, color: FAIL_COLOR }}>Couldn’t load this capture.</span>;
  const c = capture.data;
  // The shared capture-detail body (M9 T5, ADR-060 §7) — the SAME component the "see raw capture"
  // sheet renders (source badge, status, the capture's media, raw text, node chips). Activity adds the
  // anchor editor on top (its own affordance, not part of the shared traceability surface).
  return (
    <div style={{ display: 'grid', gap: 10 }}>
      <CaptureDetailBody capture={c} />
      <AnchorEditor capture={c} />
    </div>
  );
}

// --- Expanded review-verdict detail: what was decided (M8.1 follow-up) ---------------------------

const REVIEW_KIND_LABEL: Record<string, string> = {
  'entity-ambiguity': 'Entity match',
  'vocab-proposal': 'New vocabulary',
  'stance-candidate': 'Remember this?',
  'dedup-proposal': 'Possible duplicate',
};

function humanValue(v: unknown): string {
  if (v == null) return '—';
  if (typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean') return String(v);
  return JSON.stringify(v);
}

// A resolved review, fetched on expand (GET /review/{id}): its kind, final status, the excerpt it was
// filed from, and the recorded resolution (what was decided) rendered generically.
function ReviewVerdictDetail({ reviewId }: { reviewId: string }) {
  const q = useReviewItem(reviewId);
  if (q.isLoading) return <span style={{ fontSize: 12, color: 'var(--muted)' }}>Loading…</span>;
  if (q.isError || !q.data)
    return <span style={{ fontSize: 12, color: FAIL_COLOR }}>Couldn’t load this review.</span>;
  const r = q.data;
  const decided = Object.entries(r.resolution ?? {}).filter(([, v]) => v != null && v !== '');
  return (
    <div style={{ display: 'grid', gap: 10 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
        <SourceBadge source={REVIEW_KIND_LABEL[r.kind] ?? r.kind} />
        <span style={{ fontSize: 11, color: 'var(--muted)' }}>{r.status}</span>
      </div>
      {r.excerpt && (
        <p
          style={{
            margin: 0,
            minWidth: 0,
            fontSize: 13,
            color: 'var(--text)',
            lineHeight: 1.5,
            whiteSpace: 'pre-wrap',
            overflowWrap: 'anywhere',
            wordBreak: 'break-word',
          }}
        >
          {r.excerpt}
        </p>
      )}
      {decided.length > 0 ? (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
          {decided.map(([k, v]) => (
            <span
              key={k}
              style={{
                fontSize: 11,
                fontWeight: 600,
                color: 'var(--muted)',
                border: '1px solid var(--surface-border)',
                borderRadius: 999,
                padding: '3px 9px',
                maxWidth: '100%',
                overflowWrap: 'anywhere',
              }}
            >
              {k}: {humanValue(v)}
            </span>
          ))}
        </div>
      ) : (
        <span style={{ fontSize: 12, color: 'var(--muted)' }}>No recorded decision detail.</span>
      )}
    </div>
  );
}

// --- One feed row -------------------------------------------------------------------------------

function FeedRow({ item, index, isPipeline }: { item: ActivityFeedItem; index: number; isPipeline: boolean }) {
  const [open, setOpen] = useState(false);
  const remove = useRemoveCapture();
  const isCapture = item.kind === KIND_CAPTURE;
  const isChatCapture = isCapture && item.source === 'chat';
  const isReviewVerdict = item.kind === KIND_REVIEW_VERDICT;
  const expandable =
    (item.kind === KIND_AGENT_RUN && item.ref != null) || isCapture || (isReviewVerdict && item.ref != null);

  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.22, delay: Math.min(index, 8) * 0.03, ease: 'easeOut' }}
      style={{
        padding: 14,
        borderRadius: 'var(--radius)',
        border: '1px solid var(--surface-border)',
        // Pipeline runs (nightly/weekly) get an accent rail + tag so the scheduled aggregates stand
        // out from the individual agent/job runs at a glance.
        ...(isPipeline ? { borderLeft: '3px solid var(--accent)', paddingLeft: 12 } : {}),
        display: 'grid',
        gap: 8,
      }}
    >
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, flexWrap: 'wrap' }}>
        <span style={{ fontSize: 14, fontWeight: 700, minWidth: 0, overflowWrap: 'anywhere' }}>
          {isReviewVerdict
            ? `Reviewed: ${item.title ?? 'item'}`
            : isCapture
              ? captureHeadline(item.source)
              : (item.title ?? 'Run')}
        </span>
        {isPipeline && (
          <span
            style={{
              fontSize: 10,
              fontWeight: 700,
              letterSpacing: 0.4,
              textTransform: 'uppercase',
              color: 'var(--on-accent)',
              background: 'var(--accent)',
              borderRadius: 999,
              padding: '2px 8px',
            }}
          >
            pipeline
          </span>
        )}
        {isCapture && item.source && <SourceBadge source={item.source} />}
        <TimeAgo
          iso={item.ts}
          style={{ marginLeft: 'auto', fontSize: 12, color: 'var(--muted)' }}
        />
      </div>

      {item.snippet && (
        <p
          style={{
            margin: 0,
            minWidth: 0,
            fontSize: 13,
            color: 'var(--muted)',
            lineHeight: 1.5,
            display: '-webkit-box',
            WebkitLineClamp: 3,
            WebkitBoxOrient: 'vertical',
            overflow: 'hidden',
            // A long unbroken token (a url, an mcp payload) would push the line-clamp box past the
            // card edge on a phone; break it so the row can't overflow horizontally.
            overflowWrap: 'anywhere',
            wordBreak: 'break-word',
          }}
        >
          {item.snippet}
        </p>
      )}

      <div style={{ display: 'flex', gap: 12, alignItems: 'center' }}>
        {expandable && (
          <button
            onClick={() => setOpen((o) => !o)}
            style={{
              background: 'none',
              border: 'none',
              padding: 0,
              fontSize: 12,
              fontWeight: 600,
              color: 'var(--accent)',
              cursor: 'pointer',
            }}
          >
            {open ? 'Hide details' : 'Details'}
          </button>
        )}
        {isChatCapture && (
          <button
            onClick={() => remove.mutate(item.id)}
            disabled={remove.isPending}
            style={{
              marginLeft: 'auto',
              background: 'none',
              border: 'none',
              padding: 0,
              fontSize: 12,
              fontWeight: 600,
              color: remove.isPending ? 'var(--muted)' : FAIL_COLOR,
              cursor: remove.isPending ? 'default' : 'pointer',
            }}
          >
            {remove.isPending ? 'Removing…' : 'Remove'}
          </button>
        )}
      </div>

      <AnimatePresence>
        {open && expandable && (
          <motion.div
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: 'auto' }}
            exit={{ opacity: 0, height: 0 }}
            style={{ overflow: 'hidden', paddingTop: 4 }}
          >
            {isCapture ? (
              <CaptureDetail captureId={item.id} />
            ) : isReviewVerdict && item.ref ? (
              <ReviewVerdictDetail reviewId={item.ref} />
            ) : item.ref ? (
              <RunDetail runId={item.ref} />
            ) : null}
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  );
}

// --- Feed list for one category -----------------------------------------------------------------

function FeedList({ category }: { category: ActivityCategory }) {
  const feed = useActivityFeed(category);
  // Pipeline runs (nightly/weekly) are agent-run rows whose title is a registered pipeline name —
  // matched against the live pipeline list (no hardcode). A 503 (no scheduler) → empty set → the
  // coloring simply doesn't apply, never an error.
  const pipelines = usePipelines();
  const pipelineNames = useMemo(
    () => new Set((pipelines.data ?? []).map((p) => p.name)),
    [pipelines.data],
  );
  const items = feed.data?.pages.flatMap((p) => p.items) ?? [];

  if (feed.isLoading)
    return <p style={{ margin: 0, fontSize: 13, color: 'var(--muted)' }}>Loading…</p>;
  if (feed.isError)
    return <p style={{ margin: 0, fontSize: 13, color: FAIL_COLOR }}>Couldn’t load the feed.</p>;
  if (items.length === 0)
    return <p style={{ margin: 0, fontSize: 13, color: 'var(--muted)' }}>{tabEmptyText(category)}</p>;

  return (
    <div style={{ display: 'grid', gap: 10 }}>
      {items.map((item, i) => (
        <FeedRow
          key={`${item.kind}:${item.id}`}
          item={item}
          index={i}
          isPipeline={item.kind === KIND_AGENT_RUN && item.title != null && pipelineNames.has(item.title)}
        />
      ))}
      {feed.hasNextPage && (
        <div style={{ display: 'flex', justifyContent: 'center', paddingTop: 4 }}>
          <Button
            variant="ghost"
            onClick={() => feed.fetchNextPage()}
            disabled={feed.isFetchingNextPage}
          >
            {feed.isFetchingNextPage ? 'Loading…' : 'Load older'}
          </Button>
        </div>
      )}
    </div>
  );
}

export function FeedView({
  initialCategory,
  pinnedRun,
  onDismissRun,
}: {
  initialCategory?: ActivityCategory;
  // A capture's "See processing →" deep-link (activityNav's `openRun`, ADR-061 §10) pins that run's
  // detail atop the Feed — pagination-proof (fetched by id via useRun inside RunDetail), so the user
  // follows the per-part processing without hunting the keyset list. State is owned by ActivityScreen
  // (survives the Feed↔Ops toggle); FeedView just renders it + reports Dismiss.
  pinnedRun?: string | null;
  onDismissRun?: () => void;
} = {}) {
  const [category, setCategory] = useState<ActivityCategory>(initialCategory ?? 'agents_jobs');
  return (
    <div style={{ display: 'grid', gap: 12 }}>
      {pinnedRun && (
        <div
          style={{
            display: 'grid',
            gap: 10,
            padding: 14,
            borderRadius: 'var(--radius)',
            border: '1px solid var(--surface-border)',
            borderLeft: '3px solid var(--accent)',
            paddingLeft: 12,
          }}
        >
          <div style={{ display: 'flex', alignItems: 'baseline', gap: 10 }}>
            <span style={{ fontSize: 14, fontWeight: 700 }}>Processing run</span>
            <button
              type="button"
              onClick={onDismissRun}
              aria-label="Dismiss run detail"
              style={{
                marginLeft: 'auto',
                background: 'none',
                border: 'none',
                padding: 0,
                fontSize: 12,
                fontWeight: 600,
                color: 'var(--muted)',
                cursor: 'pointer',
              }}
            >
              Dismiss
            </button>
          </div>
          <RunDetail runId={pinnedRun} />
        </div>
      )}
      {/* No wrapping container — the active pill alone marks the selection (minimal chrome). */}
      <div style={{ display: 'flex', gap: 4 }}>
        {TABS.map((t) => {
          const selected = t.id === category;
          return (
            <button
              key={t.id}
              onClick={() => setCategory(t.id)}
              aria-pressed={selected}
              style={{
                position: 'relative',
                flex: 1,
                padding: '8px 6px',
                fontSize: 12.5,
                fontWeight: 600,
                background: 'transparent',
                border: 'none',
                borderRadius: 'var(--radius-sm, 8px)',
                color: selected ? 'var(--text)' : 'var(--muted)',
                cursor: 'pointer',
              }}
            >
              {selected && (
                <motion.span
                  layoutId="feed-tab-active"
                  transition={{ type: 'spring', stiffness: 500, damping: 34 }}
                  style={{
                    position: 'absolute',
                    inset: 0,
                    borderRadius: 'var(--radius-sm, 8px)',
                    background: 'var(--surface)',
                    border: '1px solid var(--surface-border)',
                  }}
                />
              )}
              <span style={{ position: 'relative' }}>{t.label}</span>
            </button>
          );
        })}
      </div>

      <FeedList category={category} />
    </div>
  );
}
