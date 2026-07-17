// The single app-level NodePreview drawer (ADR-054 §5) — a bottom sheet opened by any NodeChip. The
// drawer chrome owns the header (type icon + title + close) AND the "Explore in map" second hop, so
// the shared NodePreview primitive stays unchanged (its edge chips jump to the map via onOpenNode,
// identical to Search/Chat). `onExploreInMap` closes the drawer and lands on the canvas.
import { AnimatePresence, motion, useReducedMotion } from 'framer-motion';
import { useEffect } from 'react';
import { baseName } from './nodeDetail';
import { NodePreview } from './NodePreview';
import type { NodeHint } from './nodePreviewNav';
import { typeIcon } from './nodeTypes';

export interface PreviewTarget {
  id: string;
  hint: NodeHint | null;
}

export function NodePreviewDrawer({
  target,
  onClose,
  onExploreInMap,
}: {
  target: PreviewTarget | null;
  onClose: () => void;
  onExploreInMap: (nodeId: string) => void;
}) {
  const reduce = useReducedMotion();

  useEffect(() => {
    if (!target) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [target, onClose]);

  return (
    <AnimatePresence>
      {target && (
        <motion.div
          key="backdrop"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.2 }}
          onClick={onClose}
          style={{
            position: 'fixed',
            inset: 0,
            zIndex: 50,
            background: 'rgba(0, 0, 0, 0.5)',
            display: 'flex',
            flexDirection: 'column',
            justifyContent: 'flex-end',
          }}
        >
          <motion.div
            key="sheet"
            role="dialog"
            aria-modal="true"
            // A pixel rise (not `y: '100%'`) — percentage transforms depend on the sheet's measured
            // height and can stick at their initial before it's known, leaving the sheet off-screen.
            initial={reduce ? { opacity: 0 } : { opacity: 0, y: 40 }}
            animate={{ opacity: 1, y: 0 }}
            exit={reduce ? { opacity: 0 } : { opacity: 0, y: 40 }}
            transition={{ type: 'spring', stiffness: 420, damping: 38 }}
            onClick={(e) => e.stopPropagation()}
            style={{
              background: 'var(--bg)',
              borderTopLeftRadius: 20,
              borderTopRightRadius: 20,
              borderTop: '1px solid var(--surface-border)',
              maxHeight: '85dvh',
              overflow: 'auto',
              padding: '16px 20px calc(20px + env(safe-area-inset-bottom))',
              margin: '0 auto',
              width: '100%',
              maxWidth: 640,
            }}
          >
            <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
              <span aria-hidden style={{ fontSize: 20, flexShrink: 0 }}>
                {typeIcon(target.hint?.type)}
              </span>
              <span
                style={{
                  minWidth: 0,
                  flex: 1,
                  fontSize: 17,
                  fontWeight: 700,
                  letterSpacing: -0.2,
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                  whiteSpace: 'nowrap',
                }}
              >
                {target.hint?.title ?? baseName(target.id)}
              </span>
              <button
                type="button"
                aria-label="Close"
                onClick={onClose}
                style={{
                  flexShrink: 0,
                  width: 32,
                  height: 32,
                  borderRadius: 999,
                  border: '1px solid var(--surface-border)',
                  background: 'var(--surface)',
                  color: 'var(--muted)',
                  cursor: 'pointer',
                  fontSize: 14,
                }}
              >
                ✕
              </button>
            </div>

            <div style={{ marginTop: 12 }}>
              <button
                type="button"
                onClick={() => onExploreInMap(target.id)}
                style={{
                  display: 'inline-flex',
                  alignItems: 'center',
                  gap: 6,
                  fontSize: 12,
                  fontWeight: 600,
                  padding: '5px 12px',
                  borderRadius: 999,
                  border: '1px solid var(--surface-border)',
                  background: 'transparent',
                  color: 'var(--accent)',
                  cursor: 'pointer',
                }}
              >
                <span aria-hidden>✷</span> Explore in map
              </button>
            </div>

            <NodePreview nodeId={target.id} onOpenNode={onExploreInMap} />
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
