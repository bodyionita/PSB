// <TimeAgo iso> — the coarse relative phrase (relativeTime, unchanged: "just now / Nm / Nh / Nd ago",
// even "400d ago") plus a tap/hover exact-time tooltip (`17 Jul 2026, 08:36`) via the shared
// <HoverTip> primitive (ADR-054 §1). The exact time is also in `aria-label` for screen readers.
import { type CSSProperties } from 'react';
import { HoverTip } from './HoverTip';
import { exactTime, relativeTime } from './relativeTime';

export function TimeAgo({ iso, style }: { iso: string | null; style?: CSSProperties }) {
  if (!iso) return null;
  const phrase = relativeTime(iso);
  const exact = exactTime(iso);
  return (
    <HoverTip tip={exact} ariaLabel={`${phrase}, ${exact}`} style={style}>
      {phrase}
    </HoverTip>
  );
}
