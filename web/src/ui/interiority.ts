// The inner-voice marker semantics (M8.2, ADR-055 §3c), shared by the NodePreview badge and the map
// canvas. Pinned by T3.5: `internal` = full marker, `mixed` = subtle variant, `external`/`null` =
// no marker. Kept as pure data so both the DOM badge and the canvas draw agree on when to show it.
import type { Interiority } from '../api/types';

export interface InteriorityMark {
  // `internal` → the full marker; `mixed` → the subtle variant.
  full: boolean;
  label: string;
}

export function interiorityMark(i: Interiority): InteriorityMark | null {
  if (i === 'internal') return { full: true, label: 'Inner voice' };
  if (i === 'mixed') return { full: false, label: 'Partly inner voice' };
  return null; // external | null — the world-record default, no marker
}

// The inner-voice glyph, shared by the badge and the map's hover label.
export const INTERIORITY_GLYPH = '✦';
