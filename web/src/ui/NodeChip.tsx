// A clickable reference to a node — type icon + title in a pill — opening the shared NodePreview
// drawer (ADR-054 §5). Strictly node-uuid-only: `nodeId` is the frontmatter-uuid the drawer fetches
// (GET /nodes/{id}); references that lack one (a store path, a review-queue id) are NOT NodeChips.
// Degrades to a static pill when rendered outside a NodePreview provider.
import type { CSSProperties } from 'react';
import { useNodePreview } from './nodePreviewNav';
import { typeIcon } from './nodeTypes';

const PILL: CSSProperties = {
  display: 'inline-flex',
  alignItems: 'center',
  gap: 5,
  fontSize: 11,
  color: 'var(--accent)',
  background: 'var(--surface)',
  border: '1px solid var(--surface-border)',
  borderRadius: 999,
  padding: '3px 9px',
  maxWidth: '100%',
};

export function NodeChip({
  nodeId,
  type,
  title,
}: {
  nodeId: string;
  type: string | null;
  title: string | null;
}) {
  const nav = useNodePreview();
  const label = title ?? nodeId;
  const inner = (
    <>
      <span aria-hidden>{typeIcon(type)}</span>
      <span
        style={{ minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}
      >
        {label}
      </span>
    </>
  );

  if (!nav) {
    return (
      <span title={label} style={PILL}>
        {inner}
      </span>
    );
  }
  return (
    <button
      type="button"
      title={label}
      onClick={() => nav.openNode(nodeId, { type, title })}
      style={{ ...PILL, cursor: 'pointer', textAlign: 'left' }}
    >
      {inner}
    </button>
  );
}
