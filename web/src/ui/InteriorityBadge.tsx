// The inner-voice marker pill (M8.2, ADR-055 §3c) shown on NodePreview / the drawer header. Subtle
// by design: `internal` renders a soft filled pill, `mixed` a fainter outline-only one; `external`/
// `null` render nothing. Uses the gradient-partner accent (`--accent-2`) so it reads as a distinct
// dimension without competing with the primary accent.
import type { Interiority } from '../api/types';
import { INTERIORITY_GLYPH, interiorityMark } from './interiority';

export function InteriorityBadge({ interiority }: { interiority: Interiority }) {
  const mark = interiorityMark(interiority);
  if (!mark) return null;
  return (
    <span
      title={mark.label}
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 5,
        fontSize: 11,
        fontWeight: 700,
        letterSpacing: 0.3,
        whiteSpace: 'nowrap',
        color: 'var(--accent-2)',
        background: mark.full ? 'color-mix(in srgb, var(--accent-2) 16%, transparent)' : 'transparent',
        border: `1px solid ${mark.full ? 'transparent' : 'var(--surface-border)'}`,
        borderRadius: 999,
        padding: '3px 9px',
        opacity: mark.full ? 1 : 0.85,
      }}
    >
      <span aria-hidden>{INTERIORITY_GLYPH}</span>
      {mark.label}
    </span>
  );
}
