// The atomic media tiles the strip + capture surfaces compose (M9 T5, ADR-060 §7): a lazy photo
// thumbnail, a themed voice player, and the two non-content states — a breathing shimmer while a
// derivation is `pending` and an explicit broken-media tile when it went `unavailable`. "Never a
// silent gap" (ADR-060 §7): a still-processing or failed item always shows *something*.
import { motion, useReducedMotion } from 'framer-motion';
import { useState } from 'react';
import { mediaUrl } from '../../api/client';

export const TILE = 84; // thumbnail edge, px

const tileBase = {
  width: TILE,
  height: TILE,
  borderRadius: 12,
  border: '1px solid var(--surface-border)',
  background: 'var(--surface)',
  overflow: 'hidden',
  flexShrink: 0,
} as const;

// A media item still deriving — a breathing placeholder (autonomous pulse, so reduced-motion stills
// it). `voice` widens to the player's footprint so the strip doesn't jump when the player lands.
export function ShimmerTile({ variant = 'photo' }: { variant?: 'photo' | 'voice' }) {
  const reduce = useReducedMotion();
  const wide = variant === 'voice';
  return (
    <motion.div
      aria-label="Still processing"
      animate={reduce ? undefined : { opacity: [0.4, 0.85, 0.4] }}
      transition={reduce ? undefined : { duration: 1.4, repeat: Infinity, ease: 'easeInOut' }}
      style={{
        ...tileBase,
        width: wide ? '100%' : TILE,
        height: wide ? 52 : TILE,
        display: 'grid',
        placeItems: 'center',
        color: 'var(--muted)',
        fontSize: 20,
      }}
    >
      <span aria-hidden>{wide ? '🎙' : '◍'}</span>
    </motion.div>
  );
}

// A derivation that gave up (`unavailable`) — an explicit broken-media tile. The raw file may be
// un-renderable (e.g. a HEIC that never converted); this makes the gap visible, not silent.
export function BrokenTile({ label = 'Unavailable' }: { label?: string }) {
  return (
    <div
      title={label}
      aria-label={`Media ${label.toLowerCase()}`}
      style={{
        ...tileBase,
        display: 'grid',
        placeItems: 'center',
        color: 'var(--muted)',
        fontSize: 22,
        borderStyle: 'dashed',
      }}
    >
      <span aria-hidden>▨</span>
    </div>
  );
}

// A derived photo thumbnail — lazy-loaded, browser-scaled (no server thumbnailing, ADR-060 §7).
// Tapping opens the lightbox. A file that won't decode (a stored HEIC) falls back to a broken tile.
export function PhotoThumb({ id, onOpen }: { id: string; onOpen: () => void }) {
  const [broken, setBroken] = useState(false);
  if (broken) return <BrokenTile label="Couldn’t load" />;
  return (
    <motion.button
      type="button"
      onClick={onOpen}
      whileTap={{ scale: 0.96 }}
      aria-label="Open photo"
      style={{ ...tileBase, padding: 0, cursor: 'pointer' }}
    >
      <img
        src={mediaUrl(id)}
        loading="lazy"
        alt=""
        onError={() => setBroken(true)}
        style={{ width: '100%', height: '100%', objectFit: 'cover', display: 'block' }}
      />
    </motion.button>
  );
}

// A compact themed voice player (ADR-060 §7): native `<audio controls>` under a styled shell so
// Range/206 scrubbing (served by the API's FileResponse) works everywhere, no custom scrubber. When
// the transcript is `unavailable` the audio still plays (ADR-060 §6 — audio kept + playable); a small
// caption says so.
export function VoicePlayer({ id, unavailable = false }: { id: string; unavailable?: boolean }) {
  return (
    <div style={{ display: 'grid', gap: 4 }}>
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 10,
          padding: '8px 12px',
          borderRadius: 'var(--radius)',
          border: '1px solid var(--surface-border)',
          background: 'var(--surface)',
        }}
      >
        <span aria-hidden style={{ fontSize: 16, color: 'var(--accent)', flexShrink: 0 }}>
          🎙
        </span>
        <audio
          controls
          preload="none"
          src={mediaUrl(id)}
          style={{ width: '100%', height: 36 }}
        >
          Your browser can’t play this audio.
        </audio>
      </div>
      {unavailable && (
        <span style={{ fontSize: 11, color: 'var(--muted)' }}>Transcript unavailable — audio only.</span>
      )}
    </div>
  );
}
