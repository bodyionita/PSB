// <TimeAgo iso> — the coarse relative phrase (relativeTime, unchanged: "just now / Nm / Nh / Nd ago",
// even "400d ago") plus a CUSTOM tooltip that works on BOTH hover (desktop) and tap (mobile) —
// ADR-054 §1 — showing the exact local time (`17 Jul 2026, 08:36`). Native `title` was rejected
// (invisible on touch). The exact time is also in `aria-label` for screen readers. The tooltip
// dismisses on outside-tap / scroll / resize / Esc. `onClick` stops propagation so tapping a
// timestamp inside a clickable row toggles the tooltip without also triggering the row.
import { AnimatePresence, motion, useReducedMotion } from 'framer-motion';
import { useEffect, useLayoutEffect, useRef, useState, type CSSProperties } from 'react';
import { createPortal } from 'react-dom';
import { exactTime, relativeTime } from './relativeTime';

// The tooltip is rendered in a document.body PORTAL and positioned `fixed` to the viewport. An
// absolutely-positioned bubble was clipped by any `overflow: hidden` ancestor (the Activity
// run-detail expand animation, a line-clamped snippet, a scroll container) — cutting the time off
// the `date, HH:MM` string. A portal escapes every such ancestor; we then measure the bubble and
// place it above the timestamp (flipping below when there's no room), clamped into an 8px viewport
// gutter so neither end is ever cut off.
interface TipPos {
  left: number;
  top: number;
  ready: boolean;
}

const GUTTER = 8;
const GAP = 6;

export function TimeAgo({ iso, style }: { iso: string | null; style?: CSSProperties }) {
  const [open, setOpen] = useState(false);
  const reduce = useReducedMotion();
  const ref = useRef<HTMLSpanElement>(null);
  const tipRef = useRef<HTMLSpanElement>(null);
  const [pos, setPos] = useState<TipPos>({ left: 0, top: 0, ready: false });

  useEffect(() => {
    if (!open) return;
    const dismiss = (e: Event) => {
      if (e.type === 'pointerdown' && ref.current?.contains(e.target as Node)) return;
      setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false);
    };
    // Fixed tooltip can't follow a scrolling/resizing viewport, so dismiss on either.
    document.addEventListener('pointerdown', dismiss, true);
    document.addEventListener('scroll', dismiss, true);
    document.addEventListener('keydown', onKey, true);
    window.addEventListener('resize', dismiss);
    return () => {
      document.removeEventListener('pointerdown', dismiss, true);
      document.removeEventListener('scroll', dismiss, true);
      document.removeEventListener('keydown', onKey, true);
      window.removeEventListener('resize', dismiss);
    };
  }, [open]);

  // Measure once the bubble is in the DOM, then place + clamp it. useLayoutEffect so the corrected
  // position lands before paint (no flash at the provisional 0,0). Not reset on close, so the exit
  // fade plays at the last position; the next open re-measures pre-paint before it can be seen.
  useLayoutEffect(() => {
    if (!open) return;
    const trigger = ref.current;
    const tip = tipRef.current;
    if (!trigger || !tip) return;
    const t = trigger.getBoundingClientRect();
    const b = tip.getBoundingClientRect();
    const above = t.top - GAP - b.height >= GUTTER;
    const top = above ? t.top - GAP - b.height : t.bottom + GAP;
    const centered = t.left + t.width / 2 - b.width / 2;
    const left = Math.max(GUTTER, Math.min(centered, window.innerWidth - GUTTER - b.width));
    setPos({ left, top, ready: true });
  }, [open]);

  if (!iso) return null;
  const phrase = relativeTime(iso);
  const exact = exactTime(iso);

  return (
    <span ref={ref} style={{ display: 'inline-flex', ...style }}>
      <span
        role="button"
        tabIndex={0}
        aria-label={`${phrase}, ${exact}`}
        // Hover (desktop) via pointer events GATED to a mouse pointer: on touch, the synthetic
        // pointerenter that precedes a tap's click would otherwise open-then-close on the first tap
        // (iOS Safari) and show nothing — the exact failure ADR-054 §1's custom tooltip must avoid.
        // Touch/pen taps fall through to the click toggle only.
        onPointerEnter={(e) => {
          if (e.pointerType === 'mouse') setOpen(true);
        }}
        onPointerLeave={(e) => {
          if (e.pointerType === 'mouse') setOpen(false);
        }}
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
        {phrase}
      </span>
      {createPortal(
        <AnimatePresence>
          {open && (
            <motion.span
              ref={tipRef}
              role="tooltip"
              initial={reduce ? { opacity: 0 } : { opacity: 0, y: 4 }}
              animate={{ opacity: pos.ready ? 1 : 0, y: 0 }}
              exit={reduce ? { opacity: 0 } : { opacity: 0, y: 4 }}
              transition={{ duration: 0.14, ease: 'easeOut' }}
              style={{
                position: 'fixed',
                left: pos.left,
                top: pos.top,
                visibility: pos.ready ? 'visible' : 'hidden',
                zIndex: 1000,
                pointerEvents: 'none',
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
          )}
        </AnimatePresence>,
        document.body,
      )}
    </span>
  );
}
