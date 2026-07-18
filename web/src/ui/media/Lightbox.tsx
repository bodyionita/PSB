// Full-screen photo lightbox (M9 T5, ADR-060 §7): a framer-motion zoom over a dim backdrop, tap /
// swipe-down / Esc to dismiss, left/right (buttons, arrow keys, horizontal swipe) to page across the
// node's photos. Under prefers-reduced-motion the zoom + drag are dropped for a plain fade. A shared
// ui/ primitive so both the NodePreview media strip and the capture surfaces open the same viewer.
import { AnimatePresence, motion, useReducedMotion } from 'framer-motion';
import { useCallback, useEffect, useRef, useState } from 'react';
import { mediaUrl } from '../../api/client';

export interface LightboxTarget {
  // Photo media ids in strip order (the pageable set); `index` is the one tapped.
  ids: string[];
  index: number;
}

const navButton = (side: 'left' | 'right') =>
  ({
    position: 'absolute',
    top: '50%',
    [side]: 12,
    transform: 'translateY(-50%)',
    width: 44,
    height: 44,
    borderRadius: 999,
    border: '1px solid rgba(255,255,255,0.25)',
    background: 'rgba(0,0,0,0.4)',
    color: '#fff',
    fontSize: 20,
    cursor: 'pointer',
    display: 'grid',
    placeItems: 'center',
    zIndex: 2,
  }) as const;

export function Lightbox({
  target,
  onClose,
}: {
  target: LightboxTarget | null;
  onClose: () => void;
}) {
  const reduce = useReducedMotion();
  const [i, setI] = useState(0);
  const wasOpen = useRef(false);
  const count = target?.ids.length ?? 0;

  // Seed the index only on the closed→open transition. The parent recreates the `target` object on
  // every render, so keying off its identity would snap the viewer back to the tapped photo on any
  // ancestor re-render (a background refetch, a focus event) mid-navigation.
  useEffect(() => {
    const open = target != null;
    if (open && !wasOpen.current) setI(target.index);
    wasOpen.current = open;
  }, [target]);

  const go = useCallback(
    (delta: number) => {
      if (count === 0) return;
      setI((prev) => (prev + delta + count) % count);
    },
    [count],
  );

  useEffect(() => {
    if (!target) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
      else if (e.key === 'ArrowLeft') go(-1);
      else if (e.key === 'ArrowRight') go(1);
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [target, onClose, go]);

  const id = target?.ids[i];

  return (
    <AnimatePresence>
      {target && id && (
        <motion.div
          key="lightbox-backdrop"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.18 }}
          onClick={onClose}
          role="dialog"
          aria-modal="true"
          aria-label="Photo viewer"
          style={{
            position: 'fixed',
            inset: 0,
            zIndex: 60,
            background: 'rgba(0,0,0,0.9)',
            display: 'grid',
            placeItems: 'center',
          }}
        >
          <button
            type="button"
            aria-label="Close"
            onClick={onClose}
            style={{
              position: 'absolute',
              top: 'calc(12px + env(safe-area-inset-top))',
              right: 12,
              width: 40,
              height: 40,
              borderRadius: 999,
              border: '1px solid rgba(255,255,255,0.25)',
              background: 'rgba(0,0,0,0.4)',
              color: '#fff',
              fontSize: 16,
              cursor: 'pointer',
              zIndex: 2,
            }}
          >
            ✕
          </button>

          {count > 1 && (
            <>
              <button
                type="button"
                aria-label="Previous photo"
                onClick={(e) => {
                  e.stopPropagation();
                  go(-1);
                }}
                style={navButton('left')}
              >
                ‹
              </button>
              <button
                type="button"
                aria-label="Next photo"
                onClick={(e) => {
                  e.stopPropagation();
                  go(1);
                }}
                style={navButton('right')}
              >
                ›
              </button>
            </>
          )}

          <motion.img
            key={id}
            src={mediaUrl(id)}
            alt=""
            drag={!reduce}
            dragConstraints={{ left: 0, right: 0, top: 0, bottom: 0 }}
            dragElastic={0.5}
            onDragEnd={(_e, info) => {
              if (Math.abs(info.offset.y) > 120) onClose();
              else if (info.offset.x < -80) go(1);
              else if (info.offset.x > 80) go(-1);
            }}
            onClick={(e) => e.stopPropagation()}
            initial={reduce ? { opacity: 0 } : { opacity: 0, scale: 0.9 }}
            animate={{ opacity: 1, scale: 1 }}
            exit={reduce ? { opacity: 0 } : { opacity: 0, scale: 0.9 }}
            transition={{ type: 'spring', stiffness: 320, damping: 34 }}
            style={{
              maxWidth: '92vw',
              maxHeight: '88dvh',
              objectFit: 'contain',
              borderRadius: 8,
              cursor: 'grab',
              touchAction: 'none',
            }}
          />

          {count > 1 && (
            <span
              style={{
                position: 'absolute',
                bottom: 'calc(16px + env(safe-area-inset-bottom))',
                fontSize: 13,
                color: 'rgba(255,255,255,0.8)',
                fontVariantNumeric: 'tabular-nums',
                zIndex: 2,
              }}
            >
              {i + 1} / {count}
            </span>
          )}
        </motion.div>
      )}
    </AnimatePresence>
  );
}
