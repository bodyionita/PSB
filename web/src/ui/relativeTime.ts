// A compact "time ago" formatter shared across surfaces (capture list, provider status, …).
// Coarse by design — just now / Nm / Nh / Nd — since these are at-a-glance timestamps, not logs.
export function relativeTime(iso: string | null): string {
  if (!iso) return '';
  const ms = Date.now() - new Date(iso).getTime();
  const s = Math.round(ms / 1000);
  if (s < 60) return 'just now';
  const m = Math.round(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.round(h / 24)}d ago`;
}
