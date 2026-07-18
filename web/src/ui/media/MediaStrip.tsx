// The NodePreview media strip (M9 T5, ADR-060 §7): a node's attached media (`GET /nodes/{id}.media[]`)
// between the header and the body. Photo thumbnails (lazy, tap → lightbox with left/right nav across
// the node's photos), voice notes as themed players, and — since media rides with `capture_id` — a
// "see raw capture" overflow that opens the shared capture detail sheet (the node → capture →
// sibling-nodes hop). `pending` shimmers, `unavailable` shows a broken tile — never a silent gap.
import { useState } from 'react';
import type { NodeMediaItem } from '../../api/types';
import { CaptureDetailSheet } from './CaptureDetail';
import { Lightbox } from './Lightbox';
import { BrokenTile, PhotoThumb, ShimmerTile, VoicePlayer } from './mediaTiles';

const SEE_RAW = '🗎 See raw capture';

export function MediaStrip({ media }: { media: NodeMediaItem[] }) {
  const [lightbox, setLightbox] = useState<number | null>(null);
  const [sheet, setSheet] = useState<string | null>(null);

  if (media.length === 0) return null;

  const photos = media.filter((m) => m.kind === 'photo');
  const voices = media.filter((m) => m.kind === 'voice');
  // The lightbox pages only over renderable (derived) photos — their ids in strip order.
  const derivedPhotoIds = photos.filter((p) => p.status === 'derived').map((p) => p.id);

  // Distinct source captures behind this node's media (a merged survivor can inherit media from more
  // than one capture — ADR-060 §4), each a "see raw capture" hop.
  const captureIds: string[] = [];
  for (const m of media) {
    if (m.capture_id && !captureIds.includes(m.capture_id)) captureIds.push(m.capture_id);
  }

  return (
    <div style={{ marginTop: 14, display: 'grid', gap: 10 }}>
      {photos.length > 0 && (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
          {photos.map((p) => {
            if (p.status === 'pending') return <ShimmerTile key={p.id} />;
            if (p.status === 'unavailable') return <BrokenTile key={p.id} />;
            const idx = derivedPhotoIds.indexOf(p.id);
            return <PhotoThumb key={p.id} id={p.id} onOpen={() => setLightbox(idx)} />;
          })}
        </div>
      )}

      {voices.map((v) =>
        v.status === 'pending' ? (
          <ShimmerTile key={v.id} variant="voice" />
        ) : (
          <VoicePlayer key={v.id} id={v.id} unavailable={v.status === 'unavailable'} />
        ),
      )}

      {captureIds.length > 0 && (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
          {captureIds.map((cid, i) => (
            <button
              key={cid}
              type="button"
              onClick={() => setSheet(cid)}
              style={{
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
              {SEE_RAW}
              {captureIds.length > 1 ? ` ${i + 1}` : ''}
            </button>
          ))}
        </div>
      )}

      <Lightbox
        target={lightbox != null ? { ids: derivedPhotoIds, index: lightbox } : null}
        onClose={() => setLightbox(null)}
      />
      <CaptureDetailSheet captureId={sheet} onClose={() => setSheet(null)} />
    </div>
  );
}
