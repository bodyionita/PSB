// <TimeAgo iso> — the coarse relative phrase (relativeTime, unchanged: "just now / Nm / Nh / Nd ago",
// even "400d ago") plus a CUSTOM tooltip that works on BOTH hover (desktop) and tap (mobile) —
// ADR-054 §1 — showing the exact local time (`17 Jul 2026, 08:36`). Native `title` was rejected
// (invisible on touch). The exact time is also in `aria-label` for screen readers. The tooltip
// dismisses on outside-tap / scroll / Esc. `onClick` stops propagation so tapping a timestamp inside
// a clickable row toggles the tooltip without also triggering the row.
import { AnimatePresence, motion, useReducedMotion } from 'framer-motion';
import { useEffect, useRef, useState, type CSSProperties } from 'react';
import { exactTime, relativeTime } from './relativeTime';

export function TimeAgo({ iso, style }: { iso: string | null; style?: CSSProperties }) {
  const [open, setOpen] = useState(false);
  const reduce = useReducedMotion();
  const ref = useRef<HTMLSpanElement>(null);

  useEffect(() => {
    if (!open) return;
    const dismiss = (e: Event) => {
      if (e.type === 'pointerdown' && ref.current?.contains(e.target as Node)) return;
      setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false);
    };
    document.addEventListener('pointerdown', dismiss, true);
    document.addEventListener('scroll', dismiss, true);
    document.addEventListener('keydown', onKey, true);
    return () => {
      document.removeEventListener('pointerdown', dismiss, true);
      document.removeEventListener('scroll', dismiss, true);
      document.removeEventListener('keydown', onKey, true);
    };
  }, [open]);

  if (!iso) return null;
  const exact = exactTime(iso);

  return (
    <span ref={ref} style={{ position: 'relative', display: 'inline-flex', ...style }}>
      <span
        role="button"
        tabIndex={0}
        aria-label={exact}
        // Mouse enter/leave drive the desktop hover; they don't fire on a touch tap, so mobile relies
        // on the click toggle below (no double-fire).
        onMouseEnter={() => setOpen(true)}
        onMouseLeave={() => setOpen(false)}
        onClick={(e) => {
          e.stopPropagation();
          setOpen((v) => !v);
        }}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            e.stopPropagation();
            setOpen((v) => !v);
          }
        }}
        style={{ cursor: 'help' }}
      >
        {relativeTime(iso)}
      </span>
      <AnimatePresence>
        {open && (
          // Static outer span owns the centering transform (so framer's y-animation can't fight it);
          // the inner motion span animates.
          <span
            style={{
              position: 'absolute',
              bottom: 'calc(100% + 6px)',
              left: '50%',
              transform: 'translateX(-50%)',
              zIndex: 30,
              pointerEvents: 'none',
            }}
          >
            <motion.span
              role="tooltip"
              initial={reduce ? { opacity: 0 } : { opacity: 0, y: 4 }}
              animate={{ opacity: 1, y: 0 }}
              exit={reduce ? { opacity: 0 } : { opacity: 0, y: 4 }}
              transition={{ duration: 0.14, ease: 'easeOut' }}
              style={{
                display: 'block',
                whiteSpace: 'nowrap',
                fontSize: 12,
                fontWeight: 500,
                color: 'var(--text)',
                background: 'var(--bg)',
                border: '1px solid var(--surface-border)',
                borderRadius: 8,
                padding: '5px 9px',
                boxShadow: '0 8px 28px rgba(0, 0, 0, 0.32)',
              }}
            >
              {exact}
            </motion.span>
          </span>
        )}
      </AnimatePresence>
    </span>
  );
}
