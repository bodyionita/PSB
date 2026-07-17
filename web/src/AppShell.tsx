import { motion } from 'framer-motion';
import { useCallback, useMemo, useState, type ReactNode } from 'react';
import { CaptureScreen } from './features/capture/CaptureScreen';
import { SearchScreen } from './features/search/SearchScreen';
import { ChatScreen } from './features/chat/ChatScreen';
import { ReviewScreen } from './features/review/ReviewScreen';
import { useReview } from './features/review/useReview';
import { ActivityScreen } from './features/activity/ActivityScreen';
import { SettingsScreen } from './features/settings/SettingsScreen';
import { MapScreen } from './features/map/MapScreen';
import { MapNavContext } from './features/map/mapNav';

type TabId = 'capture' | 'search' | 'chat' | 'map' | 'review' | 'activity' | 'settings';

// `wide` tabs opt out of the shell's 640px reading column to a full-viewport surface (ADR-051 §7 —
// the map is a hero canvas, dead on arrival inside a 640px column).
interface Tab {
  id: TabId;
  label: string;
  icon: string;
  wide?: boolean;
  render: (ctx: { seed: string | null; clearSeed: () => void }) => ReactNode;
}

const TABS: Tab[] = [
  { id: 'capture', label: 'Capture', icon: '◉', render: () => <CaptureScreen /> },
  { id: 'search', label: 'Search', icon: '⌕', render: () => <SearchScreen /> },
  { id: 'chat', label: 'Chat', icon: '✦', render: () => <ChatScreen /> },
  {
    id: 'map',
    label: 'Map',
    icon: '✷',
    wide: true,
    render: ({ seed, clearSeed }) => <MapScreen seed={seed} onSeedConsumed={clearSeed} />,
  },
  { id: 'review', label: 'Review', icon: '⚖', render: () => <ReviewScreen /> },
  { id: 'activity', label: 'Activity', icon: '≋', render: () => <ActivityScreen /> },
  { id: 'settings', label: 'Settings', icon: '⚙', render: () => <SettingsScreen /> },
];

export function AppShell() {
  const [tab, setTab] = useState<TabId>('capture');
  // One-shot seed for the Map tab: a Search card / NodePreview edge sets it via `openInMap`, the map
  // centers on it and clears it (ADR-051 §8).
  const [mapSeed, setMapSeed] = useState<string | null>(null);
  // `tab` is always a valid TabId, so find never misses; assert to satisfy strict indexing.
  const active = TABS.find((t) => t.id === tab) ?? TABS[0]!;
  // Pending-review count → the nav badge (06 §3b "badge-counted queue"). Shares the ['review',
  // 'pending'] cache with the Review screen, so opening the tab reuses it (no double fetch).
  const reviewCount = useReview().data?.length ?? 0;

  const openInMap = useCallback((nodeId: string) => {
    setMapSeed(nodeId);
    setTab('map');
  }, []);
  const mapNav = useMemo(() => ({ openInMap }), [openInMap]);

  return (
    <MapNavContext.Provider value={mapNav}>
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
          {active.render({ seed: mapSeed, clearSeed: () => setMapSeed(null) })}
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
              onClick={() => setTab(t.id)}
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
    </div>
    </MapNavContext.Provider>
  );
}
