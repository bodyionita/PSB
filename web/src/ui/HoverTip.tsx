// A tap-AND-hover tooltip primitive (ADR-054 §1) — the shared mechanism behind <TimeAgo> and the
// M8.2 inline date tokens. Native `title` was rejected (invisible on touch); this shows a small
// bubble on BOTH hover (desktop, gated to a mouse pointer) and tap (mobile). The bubble is rendered
// in a document.body PORTAL and positioned `fixed` so no `overflow: hidden` ancestor (a run-detail
// expand, a line-clamped snippet, a scroll container) can clip it; it is measured, placed above the
// trigger (flipping below when there's no room), and clamped into an 8px viewport gutter. Dismisses
// on outside-tap / scroll / resize / Esc.
//
// `onActivate` splits the two callers: when omitted, tap toggles the tooltip (the <TimeAgo> case);
// when provided, tap/Enter calls it instead (the editable-date-token case opens its editor) while
// hover still previews the exact date. `stopPropagation` keeps a tap inside a clickable row from
// also triggering the row.
import { AnimatePresence, motion, useReducedMotion } from 'framer-motion';
import {
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type CSSProperties,
  type ReactNode,
} from 'react';
import { createPortal } from 'react-dom';

interface TipPos {
  left: number;
  top: number;
  ready: boolean;
}

const GUTTER = 8;
const GAP = 6;

export function HoverTip({
  children,
  tip,
  ariaLabel,
  style,
  cursor = 'help',
  onActivate,
}: {
  children: ReactNode;
  tip: string;
  ariaLabel?: string;
  style?: CSSProperties;
  cursor?: CSSProperties['cursor'];
  onActivate?: () => void;
}) {
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
    const tipEl = tipRef.current;
    if (!trigger || !tipEl) return;
    const t = trigger.getBoundingClientRect();
    const b = tipEl.getBoundingClientRect();
    const above = t.top - GAP - b.height >= GUTTER;
    const top = above ? t.top - GAP - b.height : t.bottom + GAP;
    const centered = t.left + t.width / 2 - b.width / 2;
    const left = Math.max(GUTTER, Math.min(centered, window.innerWidth - GUTTER - b.width));
    setPos({ left, top, ready: true });
  }, [open]);

  const activate = () => {
    if (onActivate) onActivate();
    else setOpen((v) => !v);
  };

  return (
    <span ref={ref} style={{ display: 'inline-flex', ...style }}>
      <span
        role="button"
        tabIndex={0}
        aria-label={ariaLabel}
        // Hover (desktop) GATED to a mouse pointer: on touch, the synthetic pointerenter preceding a
        // tap's click would otherwise open-then-close on the first tap (iOS Safari) — ADR-054 §1's
        // exact failure. Touch/pen taps fall through to the click handler only.
        onPointerEnter={(e) => {
          if (e.pointerType === 'mouse') setOpen(true);
        }}
        onPointerLeave={(e) => {
          if (e.pointerType === 'mouse') setOpen(false);
        }}
        onClick={(e) => {
          e.stopPropagation();
          activate();
        }}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            e.stopPropagation();
            activate();
          }
        }}
        style={{ cursor }}
      >
        {children}
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
              {tip}
            </motion.span>
          )}
        </AnimatePresence>,
        document.body,
      )}
    </span>
  );
}
