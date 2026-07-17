import type { CSSProperties } from 'react';
import type { CaptureNodeRef } from '../../api/types';
import { NodeChip } from '../../ui/NodeChip';
import { typeIcon } from '../../ui/nodeTypes';

// Renders a capture's resulting nodes as clickable `NodeChip`s (M8.1, ADR-054 §5 replan): `refs` is
// the server's id-resolved projection of `paths` (a store path alone can't open `NodePreview` —
// uuid-keyed, 02-data-model §Identity: "paths are projections"). A path with no resolved ref yet
// (not indexed, or tombstoned) degrades to the old static pill rather than silently disappearing.
// Shared by the Capture-tab Recents strip and the Activity Captures-tab row detail.

const STATIC_PILL: CSSProperties = {
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
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap',
};

// A store path is `<type>/<slug>--<shortid>.md` (an inbox-fallback node lives under `inbox/`). The
// folder is the node type (for the fallback pill's icon); the display name is the slug sans short-id.
function pathType(path: string): string {
  return path.split('/')[0] ?? '';
}

function nodeName(path: string): string {
  const parts = path.split('/');
  const file = (parts[parts.length - 1] ?? path).replace(/\.md$/, '');
  return file.replace(/--[0-9a-f]+$/i, '');
}

export function NodeRefChips({ paths, refs }: { paths: string[]; refs: CaptureNodeRef[] }) {
  if (paths.length === 0) return null;
  const byPath = new Map(refs.map((r) => [r.store_path, r]));
  return (
    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
      {paths.map((p) => {
        const ref = byPath.get(p);
        if (ref) {
          return <NodeChip key={p} nodeId={ref.id} type={ref.type} title={ref.title ?? nodeName(p)} />;
        }
        return (
          <span key={p} title={p} style={STATIC_PILL}>
            <span aria-hidden>{typeIcon(pathType(p))}</span>
            {nodeName(p)}
          </span>
        );
      })}
    </div>
  );
}
