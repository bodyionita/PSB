// Theme-independent categorical plane colours (ADR-051 §4). A plane means the same thing in every
// theme, so these are deliberately NOT derived from the theme accent (unlike the single-accent
// PlaneBadge). The map's node halo is coloured by primary plane; the theme accent is reserved for
// the focal node + selection. Hues are chosen mid-luminance so they read on both the dark and light
// theme bases. The 6 seed planes get a fixed hue; a governed/unknown plane hashes to a stable
// distinct hue; inbox / none is a neutral grey.

const PLANE_COLOR: Record<string, string> = {
  professional: '#4C8DFF', // blue
  personal: '#C77DFF', // violet
  family: '#FF8A5C', // orange
  friends: '#37C7B8', // teal
  health: '#56C271', // green
  ideas: '#F4C13B', // gold
};

const NEUTRAL = '#8A8F9C';

// Stable string → hue so an unseen (governed) plane always gets the same distinct colour.
function hashHue(s: string): number {
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) % 360;
  return h;
}

// Primary-plane colour for a node mark. `inbox` and null/none fall back to neutral grey.
export function planeColor(plane: string | null | undefined): string {
  if (!plane || plane.toLowerCase() === 'inbox') return NEUTRAL;
  const known = PLANE_COLOR[plane.toLowerCase()];
  if (known) return known;
  return `hsl(${hashHue(plane)}, 62%, 60%)`;
}
