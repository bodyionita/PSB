import { AnimatePresence, motion } from 'framer-motion';
import type { AutoRecordedItem, Salience } from '../../api/types';
import { Surface } from '../../ui/Surface';
import { baseName } from '../../ui/nodeDetail';
import { relativeTime } from '../../ui/relativeTime';
import { useAutoRecorded, useRemoveAutoRecorded } from './useChat';

// The chat-scoped "recently auto-recorded" audit list (06 §2, ADR-048 §12): memories the nightly
// distiller endorsed on its own — the visible half of ADR-029's trust loop. Each row previews the
// endorsed statement + its coarse salience and offers a one-tap "that's wrong — remove"
// (soft-delete: git-rm + DB-delete + capture tombstone). This is M6's home for the reversal; M8's
// Activity feed absorbs it later.

const FAIL_COLOR = '#ff6b6b';

function SaliencePill({ salience }: { salience: Salience }) {
  return (
    <span
      title={`salience: ${salience}`}
      style={{
        fontSize: 10,
        fontWeight: 700,
        letterSpacing: 0.5,
        textTransform: 'uppercase',
        color: 'var(--muted)',
        border: '1px solid var(--surface-border)',
        borderRadius: 999,
        padding: '2px 7px',
        whiteSpace: 'nowrap',
      }}
    >
      {salience}
    </span>
  );
}

function AutoRecordedRow({ item }: { item: AutoRecordedItem }) {
  const remove = useRemoveAutoRecorded();
  // Title lands with the background organize; until then fall back to the first node path's stem, or
  // a gentle "still organizing" note (node_paths empty ⇒ not yet materialized).
  const organizing = item.node_paths.length === 0;
  const title = item.title ?? (item.node_paths[0] ? baseName(item.node_paths[0]) : null);

  return (
    <motion.div
      layout
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, x: -12 }}
      transition={{ type: 'spring', stiffness: 420, damping: 34 }}
    >
      <Surface padding={14}>
        <div style={{ display: 'flex', alignItems: 'flex-start', gap: 10 }}>
          <div style={{ minWidth: 0, flex: 1 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
              {title && (
                <span
                  style={{
                    fontSize: 14,
                    fontWeight: 700,
                    letterSpacing: -0.2,
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                  }}
                >
                  {title}
                </span>
              )}
              {item.salience && <SaliencePill salience={item.salience} />}
              <span style={{ fontSize: 11, color: 'var(--muted)' }}>{relativeTime(item.created_at)}</span>
            </div>
            <p style={{ margin: '6px 0 0', fontSize: 13, lineHeight: 1.5, color: 'var(--muted)' }}>
              {item.snippet}
            </p>
            {organizing && (
              <p style={{ margin: '6px 0 0', fontSize: 11.5, color: 'var(--muted)', fontStyle: 'italic' }}>
                still organizing…
              </p>
            )}
          </div>
          <motion.button
            onClick={() => remove.mutate(item.capture_id)}
            disabled={remove.isPending}
            whileTap={{ scale: 0.95 }}
            title="That's wrong — remove this memory"
            style={{
              flexShrink: 0,
              fontSize: 12,
              fontWeight: 600,
              padding: '6px 11px',
              borderRadius: 999,
              border: '1px solid var(--surface-border)',
              background: 'transparent',
              color: 'var(--muted)',
              cursor: remove.isPending ? 'default' : 'pointer',
              opacity: remove.isPending ? 0.5 : 1,
            }}
          >
            Remove
          </motion.button>
        </div>
        {remove.isError && (
          <p style={{ margin: '10px 0 0', fontSize: 12.5, color: FAIL_COLOR }}>
            Couldn’t remove that — try again.
          </p>
        )}
      </Surface>
    </motion.div>
  );
}

export function AutoRecordedList() {
  const { data, isLoading, isError } = useAutoRecorded();

  if (isLoading) {
    return <p style={{ margin: '4px 2px', fontSize: 13, color: 'var(--muted)' }}>Loading…</p>;
  }
  if (isError) {
    return <p style={{ margin: '4px 2px', fontSize: 13, color: FAIL_COLOR }}>Couldn’t load the list.</p>;
  }
  if (!data || data.length === 0) {
    return (
      <p style={{ margin: '4px 2px', fontSize: 13, color: 'var(--muted)', lineHeight: 1.6 }}>
        Nothing auto-recorded yet. When your brain saves a memory from a conversation on its own,
        it’ll show up here — and you can remove anything it got wrong.
      </p>
    );
  }
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
      <AnimatePresence initial={false}>
        {data.map((item) => (
          <AutoRecordedRow key={item.capture_id} item={item} />
        ))}
      </AnimatePresence>
    </div>
  );
}
