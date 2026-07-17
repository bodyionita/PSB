import { AnimatePresence, motion } from 'framer-motion';
import { useState } from 'react';
import type { ActivityCategory, ActivityFeedItem, RunChildItem } from '../../api/types';
import { NodeRefChips } from '../capture/NodeRefChips';
import { useCapture } from '../capture/useCaptures';
import { Button } from '../../ui/Button';
import { Surface } from '../../ui/Surface';
import { TimeAgo } from '../../ui/TimeAgo';
import { StatusBadge } from './runStatus';
import { FAIL_COLOR } from './statusColors';
import { useActivityFeed, useRemoveCapture, useRun } from './useActivity';

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

function RunDetail({ runId }: { runId: string }) {
  const run = useRun(runId);
  if (run.isLoading) return <span style={{ fontSize: 12, color: 'var(--muted)' }}>Loading…</span>;
  if (run.isError || !run.data)
    return <span style={{ fontSize: 12, color: FAIL_COLOR }}>Couldn’t load run detail.</span>;
  const pills = Object.entries(run.data.details).filter(
    ([, v]) => typeof v === 'number' || v === true,
  );
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

function CaptureDetail({ captureId }: { captureId: string }) {
  const capture = useCapture(captureId);
  if (capture.isLoading)
    return <span style={{ fontSize: 12, color: 'var(--muted)' }}>Loading…</span>;
  if (capture.isError || !capture.data)
    return <span style={{ fontSize: 12, color: FAIL_COLOR }}>Couldn’t load this capture.</span>;
  const c = capture.data;
  return (
    <div style={{ display: 'grid', gap: 10 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
        <SourceBadge source={c.source ?? c.kind} />
        <span style={{ fontSize: 11, color: 'var(--muted)' }}>{c.status}</span>
      </div>
      {c.raw_text && (
        <p
          style={{
            margin: 0,
            minWidth: 0,
            fontSize: 13,
            color: 'var(--text)',
            lineHeight: 1.5,
            whiteSpace: 'pre-wrap',
            // Break long unbroken tokens (URLs, long strings) so the raw text can't run under the
            // card edge on a narrow phone (the grid item's default min-width: auto would let it).
            overflowWrap: 'anywhere',
            wordBreak: 'break-word',
          }}
        >
          {c.raw_text}
        </p>
      )}
      <NodeRefChips paths={c.node_paths} refs={c.node_refs} />
      {c.error && (
        <p style={{ margin: 0, fontSize: 12, color: FAIL_COLOR }}>{c.error}</p>
      )}
    </div>
  );
}

// --- One feed row -------------------------------------------------------------------------------

function FeedRow({ item, index }: { item: ActivityFeedItem; index: number }) {
  const [open, setOpen] = useState(false);
  const remove = useRemoveCapture();
  const isCapture = item.kind === KIND_CAPTURE;
  const isChatCapture = isCapture && item.source === 'chat';
  const expandable = (item.kind === KIND_AGENT_RUN && item.ref != null) || isCapture;

  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.22, delay: Math.min(index, 8) * 0.03, ease: 'easeOut' }}
      style={{
        padding: 14,
        borderRadius: 'var(--radius)',
        border: '1px solid var(--surface-border)',
        display: 'grid',
        gap: 8,
      }}
    >
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, flexWrap: 'wrap' }}>
        <span style={{ fontSize: 14, fontWeight: 700 }}>
          {item.kind === KIND_REVIEW_VERDICT
            ? `Reviewed: ${item.title ?? 'item'}`
            : isCapture
              ? captureHeadline(item.source)
              : (item.title ?? 'Run')}
        </span>
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
            fontSize: 13,
            color: 'var(--muted)',
            lineHeight: 1.5,
            display: '-webkit-box',
            WebkitLineClamp: 3,
            WebkitBoxOrient: 'vertical',
            overflow: 'hidden',
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
            {isCapture ? <CaptureDetail captureId={item.id} /> : item.ref && <RunDetail runId={item.ref} />}
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  );
}

// --- Feed list for one category -----------------------------------------------------------------

function FeedList({ category }: { category: ActivityCategory }) {
  const feed = useActivityFeed(category);
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
        <FeedRow key={`${item.kind}:${item.id}`} item={item} index={i} />
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

export function FeedView({ initialCategory }: { initialCategory?: ActivityCategory } = {}) {
  const [category, setCategory] = useState<ActivityCategory>(initialCategory ?? 'agents_jobs');
  return (
    <div style={{ display: 'grid', gap: 16 }}>
      <div
        style={{
          display: 'flex',
          gap: 4,
          padding: 4,
          background: 'var(--surface)',
          border: '1px solid var(--surface-border)',
          borderRadius: 'var(--radius)',
        }}
      >
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

      <Surface>
        <FeedList category={category} />
      </Surface>
    </div>
  );
}
