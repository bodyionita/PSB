// The media backing a single capture (M9 T5, ADR-060 §7) — a photo OR voice note carried by
// `CaptureView.media`. Rendered on the capture surfaces: the Capture-screen Recents strip and the
// shared "see raw capture" / Activity capture detail. A photo shows a lazy thumbnail that opens the
// lightbox; a voice note plays inline (Range/206). `pending`/`unavailable` degrade to the shimmer /
// broken tile, never a silent gap. `CaptureView.media` is a single item and carries no `capture_id`
// (it *is* this capture's media), so there is no "see raw capture" hop here.
import { useState } from 'react';
import type { CaptureMedia } from '../../api/types';
import { Lightbox } from './Lightbox';
import { BrokenTile, PhotoThumb, ShimmerTile, VoicePlayer } from './mediaTiles';

export function CaptureMediaBlock({ media }: { media: CaptureMedia }) {
  const [lightbox, setLightbox] = useState(false);

  if (media.kind === 'voice') {
    if (media.status === 'pending') return <ShimmerTile variant="voice" />;
    return <VoicePlayer id={media.id} unavailable={media.status === 'unavailable'} />;
  }

  // photo (or any image-like kind)
  return (
    <>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
        {media.status === 'pending' ? (
          <ShimmerTile />
        ) : media.status === 'unavailable' ? (
          <BrokenTile />
        ) : (
          <PhotoThumb id={media.id} onOpen={() => setLightbox(true)} />
        )}
      </div>
      <Lightbox
        target={lightbox ? { ids: [media.id], index: 0 } : null}
        onClose={() => setLightbox(false)}
      />
    </>
  );
}
