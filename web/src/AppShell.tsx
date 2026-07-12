import { motion } from 'framer-motion';
import { useState, type ReactNode } from 'react';
import { CaptureScreen } from './features/capture/CaptureScreen';
import { ChatScreen } from './features/chat/ChatScreen';
import { ActivityScreen } from './features/activity/ActivityScreen';
import { SettingsScreen } from './features/settings/SettingsScreen';

type TabId = 'capture' | 'chat' | 'activity' | 'settings';

const TABS: { id: TabId; label: string; icon: string; render: () => ReactNode }[] = [
  { id: 'capture', label: 'Capture', icon: '◉', render: () => <CaptureScreen /> },
  { id: 'chat', label: 'Chat', icon: '✦', render: () => <ChatScreen /> },
  { id: 'activity', label: 'Activity', icon: '≋', render: () => <ActivityScreen /> },
  { id: 'settings', label: 'Settings', icon: '⚙', render: () => <SettingsScreen /> },
];

export function AppShell() {
  const [tab, setTab] = useState<TabId>('capture');
  // `tab` is always a valid TabId, so find never misses; assert to satisfy strict indexing.
  const active = TABS.find((t) => t.id === tab) ?? TABS[0]!;

  return (
    <div
      style={{
        position: 'relative',
        zIndex: 1,
        minHeight: '100dvh',
        display: 'flex',
        flexDirection: 'column',
        maxWidth: 640,
        margin: '0 auto',
      }}
    >
      <main style={{ flex: 1, padding: '28px 20px 96px' }}>
        {/* Enter-only, keyed by tab: changing tab remounts and plays the entrance. We avoid
            AnimatePresence exit here on purpose — screens contain infinite (repeat) animations,
            and an exit animation would wait forever on them, hanging the transition. */}
        <motion.div
          key={active.id}
          initial={{ opacity: 0, y: 12 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.22, ease: 'easeOut' }}
        >
          {active.render()}
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
              <span style={{ position: 'relative', fontSize: 18 }}>{t.icon}</span>
              <span style={{ position: 'relative', fontSize: 11, fontWeight: 600 }}>
                {t.label}
              </span>
            </button>
          );
        })}
      </nav>
    </div>
  );
}
