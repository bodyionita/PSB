import { AnimatePresence, motion } from 'framer-motion';
import { useState } from 'react';
import type { ActivityCategory, ActivityFeedItem } from '../../api/types';
import { Button } from '../../ui/Button';
import { Surface } from '../../ui/Surface';
import { relativeTime } from '../../ui/relativeTime';
import { StatusBadge } from './runStatus';
import { FAIL_COLOR } from './statusColors';
import { useActivityFeed, useRemoveConversation, useRun } from './useActivity';

// The Feed — "what did my brain do" (06 §3, ADR-053 §4): the merged GET /activity as three
// categorized tabs (agents/jobs · conversations · manual actions), newest-first keyset infinite
// scroll. An agent-run / review row expands to its full run detail; a conversation row carries the
// one-tap remove that folds in the M6 auto-recorded control.

const KIND_AGENT_RUN = 'agent_run';
const KIND_CHAT_CAPTURE = 'chat_capture';
const KIND_REVIEW_VERDICT = 'review_verdict';

const TABS: { id: ActivityCategory; label: string }[] = [
  { id: 'agents_jobs', label: 'Agents & jobs' },
  { id: 'conversations', label: 'Conversations' },
  { id: 'manual_actions', label: 'Manual actions' },
];

function tabEmptyText(category: ActivityCategory): string {
  if (category === 'conversations') return 'No memories recorded from conversations yet.';
  if (category === 'manual_actions') return 'No manual actions yet.';
  return 'No scheduled runs yet.';
}

// --- Expanded run detail (agent_run / review rows) ----------------------------------------------

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
        <p style={{ margin: 0, fontSize: 13, color: 'var(--text)', lineHeight: 1.5 }}>
          {run.data.summary}
        </p>
      )}
      {run.data.error && (
        <p style={{ margin: 0, fontSize: 13, color: FAIL_COLOR, lineHeight: 1.5 }}>
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
    </div>
  );
}

// --- One feed row -------------------------------------------------------------------------------

function FeedRow({ item, index }: { item: ActivityFeedItem; index: number }) {
  const [open, setOpen] = useState(false);
  const remove = useRemoveConversation();
  const isChild = item.parent_ref != null;
  const expandable = item.kind === KIND_AGENT_RUN && item.ref != null;
  const isConversation = item.kind === KIND_CHAT_CAPTURE;

  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.22, delay: Math.min(index, 8) * 0.03, ease: 'easeOut' }}
      style={{
        padding: 14,
        marginLeft: isChild ? 18 : 0,
        borderRadius: 'var(--radius)',
        border: '1px solid var(--surface-border)',
        borderLeft: isChild ? '2px solid var(--accent)' : '1px solid var(--surface-border)',
        display: 'grid',
        gap: 8,
      }}
    >
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, flexWrap: 'wrap' }}>
        {isChild && <span style={{ color: 'var(--muted)', fontSize: 12 }}>↳ step</span>}
        <span style={{ fontSize: 14, fontWeight: 700 }}>
          {item.kind === KIND_REVIEW_VERDICT
            ? `Reviewed: ${item.title ?? 'item'}`
            : item.title ?? (isConversation ? 'Recorded from a conversation' : 'Run')}
        </span>
        <span style={{ marginLeft: 'auto', fontSize: 12, color: 'var(--muted)' }}>
          {relativeTime(item.ts)}
        </span>
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
        {isConversation && (
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
        {open && expandable && item.ref && (
          <motion.div
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: 'auto' }}
            exit={{ opacity: 0, height: 0 }}
            style={{ overflow: 'hidden', paddingTop: 4 }}
          >
            <RunDetail runId={item.ref} />
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

export function FeedView() {
  const [category, setCategory] = useState<ActivityCategory>('agents_jobs');
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
