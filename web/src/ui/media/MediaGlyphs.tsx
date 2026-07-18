// A tiny per-kind media glyph for list cards (M9 T5, ADR-060 §7): search result cards + chat source
// cards get at most a 📷/🎙 off the node's `media_kinds` — lists stay lean, no thumbnails, nothing on
// the Map canvas. Renders nothing when the node has no media.
import type { MediaKind } from '../../api/types';

const GLYPH: Record<string, string> = { photo: '📷', voice: '🎙', video: '🎬' };
const LABEL: Record<string, string> = {
  photo: 'has a photo',
  voice: 'has a voice note',
  video: 'has a video',
};

export function MediaGlyphs({ kinds }: { kinds: MediaKind[] }) {
  const seen: string[] = [];
  for (const k of kinds) if (!seen.includes(k)) seen.push(k);
  if (seen.length === 0) return null;
  const label = seen.map((k) => LABEL[k] ?? 'has an attachment').join(', ');
  return (
    <span
      aria-label={label}
      title={label}
      style={{ display: 'inline-flex', gap: 2, fontSize: 12, flexShrink: 0, lineHeight: 1 }}
    >
      {seen.map((k) => (
        <span key={k} aria-hidden>
          {GLYPH[k] ?? '📎'}
        </span>
      ))}
    </span>
  );
}
