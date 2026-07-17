import { motion } from 'framer-motion';
import { useState } from 'react';
import type { ActivityCategory } from '../../api/types';
import { FeedView } from './FeedView';
import { OpsView } from './OpsView';

// Activity (06 §3, ADR-053): one top-level tab with a Feed / Ops segmented control. Feed = "what did
// my brain do" (three categorized tabs over the merged GET /activity); Ops = the console (pipelines,
// the runnable job roster with live log tails, graph-health, and the rehomed admin ops). The M2
// Admin tab is absorbed here.

type SubView = 'feed' | 'ops';

const SUBVIEWS: { id: SubView; label: string }[] = [
  { id: 'feed', label: 'Feed' },
  { id: 'ops', label: 'Ops' },
];

// `initialCategory` (M8.1, ADR-054 §4): a cross-tab "see all" deep-link (activityNav's
// `openCaptures`) lands here already wanting the Captures feed sub-tab open. Read once via the
// FeedView's `useState` initializer — this screen remounts fresh on every tab visit (AppShell keys
// the active tab's subtree), so no seed-consumption dance is needed (unlike ReviewScreen's
// scroll-to-highlight, this is just a starting selection).
export function ActivityScreen({
  initialCategory,
}: { initialCategory?: ActivityCategory } = {}) {
  const [view, setView] = useState<SubView>('feed');

  return (
    <div style={{ display: 'grid', gap: 14 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 14, flexWrap: 'wrap' }}>
        <h1 style={{ margin: 0, fontSize: 24, fontWeight: 700, letterSpacing: -0.4 }}>Activity</h1>
        {/* No wrapping container — the active pill alone marks the selection (minimal chrome). */}
        <div style={{ marginLeft: 'auto', display: 'flex', gap: 4 }}>
          {SUBVIEWS.map((s) => {
            const selected = s.id === view;
            return (
              <button
                key={s.id}
                onClick={() => setView(s.id)}
                aria-pressed={selected}
                style={{
                  position: 'relative',
                  padding: '7px 18px',
                  fontSize: 13,
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
                    layoutId="activity-subview-active"
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
                <span style={{ position: 'relative' }}>{s.label}</span>
              </button>
            );
          })}
        </div>
      </div>

      {view === 'feed' ? <FeedView initialCategory={initialCategory} /> : <OpsView />}
    </div>
  );
}
