import { motion, useReducedMotion } from 'framer-motion';
import type { RunStatus } from '../../api/types';
import { isTerminal } from './useActivity';
import { statusColor } from './statusColors';

// The visual run-status components (a pulsing live dot + a status pill). Colors + statusColor live in
// statusColors.ts so this file exports only components (fast-refresh-clean).

// A small pulsing dot for a live run. The infinite pulse is gated on prefers-reduced-motion (framer
// does not auto-quiet JS-driven `animate` without a MotionConfig, and there is none — matches the
// RecentCaptures precedent), so a reduced-motion user gets a static dot.
export function RunningDot() {
  const reduced = useReducedMotion();
  return (
    <motion.span
      aria-hidden
      animate={reduced ? undefined : { opacity: [1, 0.25, 1] }}
      transition={reduced ? undefined : { duration: 1.1, repeat: Infinity, ease: 'easeInOut' }}
      style={{ width: 8, height: 8, borderRadius: '50%', background: 'var(--muted)', flex: 'none' }}
    />
  );
}

// Status pill: a live dot while running, else the terminal glyph + uppercase label.
export function StatusBadge({ status }: { status: RunStatus }) {
  const running = !isTerminal(status);
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8 }}>
      {running ? (
        <RunningDot />
      ) : (
        <span aria-hidden style={{ color: statusColor(status) }}>
          {status === 'succeeded' ? '✓' : status === 'failed' ? '✕' : '—'}
        </span>
      )}
      <span
        style={{
          fontSize: 12,
          fontWeight: 700,
          letterSpacing: 0.4,
          textTransform: 'uppercase',
          color: statusColor(status),
        }}
      >
        {status}
      </span>
    </span>
  );
}
