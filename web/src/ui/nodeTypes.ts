// Presentation helpers for node types (02-data-model — the 9 seed types) + edge rels. The
// vocabulary is *governed and extensible* (ADR-027): a governed addition the web has never seen
// still renders, via the `?? fallback` in each lookup. No hard-coded list is treated as complete.

// Type → glyph. Emoji reads as an at-a-glance content marker on a search/edge card; unknown
// (freshly-approved) types fall back to a neutral node glyph.
const TYPE_ICON: Record<string, string> = {
  memory: '🧠',
  person: '👤',
  idea: '💡',
  conversation: '💬',
  insight: '✨',
  place: '📍',
  event: '📅',
  project: '🗂️',
  topic: '🏷️',
};

const TYPE_ICON_FALLBACK = '◆';

export function typeIcon(type: string | null | undefined): string {
  if (!type) return TYPE_ICON_FALLBACK;
  return TYPE_ICON[type] ?? TYPE_ICON_FALLBACK;
}

// Human label for a type — just the type itself, title-cased (the vocabulary strings are already
// human words). Kept as a function so callers don't re-implement the casing.
export function typeLabel(type: string | null | undefined): string {
  if (!type) return 'node';
  return type.charAt(0).toUpperCase() + type.slice(1);
}

// A canonical edge is labelled by its `rel`; a derived edge is similarity ("similar to"). Turns
// an edge into a short human phrase for the preview.
export function edgeLabel(rel: string, origin: 'canonical' | 'derived'): string {
  if (origin === 'derived') return 'similar';
  return rel.replace(/_/g, ' ');
}
