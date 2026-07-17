import { motion } from 'framer-motion';
import { useCallback, useMemo, useState, type ReactNode } from 'react';
import { CaptureScreen } from './features/capture/CaptureScreen';
import { ChatScreen } from './features/chat/ChatScreen';
import { ReviewScreen } from './features/review/ReviewScreen';
import { useReview } from './features/review/useReview';
import { ActivityScreen } from './features/activity/ActivityScreen';
import { SettingsScreen } from './features/settings/SettingsScreen';
import { ExploreScreen } from './features/map/ExploreScreen';
import { MapNavContext } from './features/map/mapNav';
import { ReviewNavContext } from './features/review/reviewNav';
import { NodePreviewNavContext, type NodeHint } from './ui/nodePreviewNav';
import { NodePreviewDrawer, type PreviewTarget } from './ui/NodePreviewDrawer';

type TabId = 'capture' | 'explore' | 'chat' | 'review' | 'activity' | 'settings';

// One-shot seeds carried into the tab that consumes them: `explore` centers on `seed`, `review`
// scrolls-to + highlights `reviewSeed` (ADR-054 §5). Each tab reads only what it needs.
interface TabCtx {
  seed: string | null;
  clearSeed: () => void;
  reviewSeed: string | null;
}

// `wide` tabs opt out of the shell's 640px reading column to a full-viewport surface (ADR-051 §7 —
// the map is a hero canvas, dead on arrival inside a 640px column).
interface Tab {
  id: TabId;
  label: string;
  icon: string;
  wide?: boolean;
  render: (ctx: TabCtx) => ReactNode;
}

// Search + Map merged into one "Explore" tab, 7→6 (ADR-054 §3): search-box landing, full result
// cards, picking a hit centers it as a map constellation; search stays reachable from anywhere in
// the explorer via its own internal search⇄map toggle.
const TABS: Tab[] = [
  { id: 'capture', label: 'Capture', icon: '◉', render: () => <CaptureScreen /> },
  {
    id: 'explore',
    label: 'Explore',
    icon: '✷',
    wide: true,
    render: ({ seed, clearSeed }) => <ExploreScreen seed={seed} onSeedConsumed={clearSeed} />,
  },
  { id: 'chat', label: 'Chat', icon: '✦', render: () => <ChatScreen /> },
  {
    id: 'review',
    label: 'Review',
    icon: '⚖',
    render: ({ reviewSeed }) => <ReviewScreen seed={reviewSeed} />,
  },
  { id: 'activity', label: 'Activity', icon: '≋', render: () => <ActivityScreen /> },
  { id: 'settings', label: 'Settings', icon: '⚙', render: () => <SettingsScreen /> },
];

export function AppShell() {
  const [tab, setTab] = useState<TabId>('capture');
  // One-shot seed for the Explore tab's map mode: a NodePreview edge / drawer sets it via
  // `openInMap`, Explore centers on it and clears it (ADR-051 §8, folded into Explore at ADR-054 §3).
  const [mapSeed, setMapSeed] = useState<string | null>(null);
  // One-shot seed for the Review tab: a graph-health aging-review offender sets it via
  // `openReviewItem`, the Review screen scrolls-to + highlights it and clears it (ADR-054 §5).
  const [reviewSeed, setReviewSeed] = useState<string | null>(null);
  // The single app-level NodePreview drawer's current target (ADR-054 §5): any NodeChip sets it via
  // `openNode`; the drawer renders it. Null = closed.
  const [previewTarget, setPreviewTarget] = useState<PreviewTarget | null>(null);
  // `tab` is always a valid TabId, so find never misses; assert to satisfy strict indexing.
  const active = TABS.find((t) => t.id === tab) ?? TABS[0]!;
  // Pending-review count → the nav badge (06 §3b "badge-counted queue"). Shares the ['review',
  // 'pending'] cache with the Review screen, so opening the tab reuses it (no double fetch).
  const reviewCount = useReview().data?.length ?? 0;

  const openInMap = useCallback((nodeId: string) => {
    setPreviewTarget(null); // a map hop closes the preview drawer if it was the entry point
    setMapSeed(nodeId);
    setTab('explore');
  }, []);
  const mapNav = useMemo(() => ({ openInMap }), [openInMap]);

  const openReviewItem = useCallback((reviewItemId: string) => {
    setReviewSeed(reviewItemId);
    setTab('review');
  }, []);
  const reviewNav = useMemo(() => ({ openReviewItem }), [openReviewItem]);

  const openNode = useCallback((nodeId: string, hint?: NodeHint) => {
    setPreviewTarget({ id: nodeId, hint: hint ?? null });
  }, []);
  const nodePreviewNav = useMemo(() => ({ openNode }), [openNode]);
  const closePreview = useCallback(() => setPreviewTarget(null), []);

  return (
    <MapNavContext.Provider value={mapNav}>
    <ReviewNavContext.Provider value={reviewNav}>
    <NodePreviewNavContext.Provider value={nodePreviewNav}>
    <div
      style={{
        position: 'relative',
        zIndex: 1,
        minHeight: '100dvh',
        display: 'flex',
        flexDirection: 'column',
        maxWidth: active.wide ? 'none' : 640,
        margin: '0 auto',
        width: '100%',
      }}
    >
      <main style={{ flex: 1, padding: active.wide ? '20px 16px 84px' : '28px 20px 96px' }}>
        {/* Enter-only, keyed by tab: changing tab remounts and plays the entrance. We avoid
            AnimatePresence exit here on purpose — screens contain infinite (repeat) animations,
            and an exit animation would wait forever on them, hanging the transition. */}
        <motion.div
          key={active.id}
          initial={{ opacity: 0, y: 12 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.22, ease: 'easeOut' }}
        >
          {active.render({
            seed: mapSeed,
            clearSeed: () => setMapSeed(null),
            reviewSeed,
          })}
        </motion.div>
      </main>

      <nav
        style={{
          position: 'fixed',
          bottom: 0,
          left: 0,
          right: 0,
          zIndex: 2,
          display: 'flex',
          justifyContent: 'center',
          gap: 4,
          padding: '10px 12px calc(10px + env(safe-area-inset-bottom))',
          background: 'var(--surface)',
          borderTop: '1px solid var(--surface-border)',
          backdropFilter: 'blur(20px)',
          WebkitBackdropFilter: 'blur(20px)',
        }}
      >
        {TABS.map((t) => {
          const selected = t.id === tab;
          return (
            <button
              key={t.id}
              // Manual navigation clears any pending review deep-link seed so opening Review directly
              // never re-highlights a stale item (openReviewItem sets it again via its own path).
              onClick={() => {
                setReviewSeed(null);
                setTab(t.id);
              }}
              aria-current={selected ? 'page' : undefined}
              style={{
                position: 'relative',
                flex: 1,
                maxWidth: 120,
                display: 'flex',
                flexDirection: 'column',
                alignItems: 'center',
                gap: 4,
                padding: '8px 0',
                background: 'transparent',
                border: 'none',
                color: selected ? 'var(--text)' : 'var(--muted)',
              }}
            >
              {selected && (
                <motion.span
                  layoutId="nav-active"
                  transition={{ type: 'spring', stiffness: 500, damping: 34 }}
                  style={{
                    position: 'absolute',
                    inset: 0,
                    borderRadius: 'var(--radius)',
                    background: 'var(--surface)',
                    border: '1px solid var(--surface-border)',
                  }}
                />
              )}
              <span style={{ position: 'relative', fontSize: 18 }}>
                {t.icon}
                {t.id === 'review' && reviewCount > 0 && (
                  <span
                    aria-label={`${reviewCount} to review`}
                    style={{
                      position: 'absolute',
                      top: -6,
                      left: 'calc(50% + 6px)',
                      minWidth: 16,
                      height: 16,
                      padding: '0 4px',
                      borderRadius: 999,
                      background: 'var(--accent)',
                      color: 'var(--on-accent)',
                      fontSize: 10,
                      fontWeight: 700,
                      lineHeight: '16px',
                      textAlign: 'center',
                    }}
                  >
                    {reviewCount > 99 ? '99+' : reviewCount}
                  </span>
                )}
              </span>
              <span style={{ position: 'relative', fontSize: 11, fontWeight: 600 }}>
                {t.label}
              </span>
            </button>
          );
        })}
      </nav>

      <NodePreviewDrawer target={previewTarget} onClose={closePreview} onExploreInMap={openInMap} />
    </div>
    </NodePreviewNavContext.Provider>
    </ReviewNavContext.Provider>
    </MapNavContext.Provider>
  );
}
