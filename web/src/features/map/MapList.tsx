// The Map tab's tappable-list renderer (ADR-051 §7): the same rel-keyed zones the canvas draws,
// rendered as calm, grouped tappable rows. It is (a) the `prefers-reduced-motion` fallback — no
// force sim, no motion, just DOM — and (b) a manual view toggle. Tap a neighbor row = re-center;
// tap the focal header = open the shared NodePreview drawer; each zone's overflow pages via a
// "Show N more" button. Edge styling is carried inline: derived rows read faint + "similar",
// superseded (`until`) rows read dashed + dimmed with the `until` date, canonical rows show a
// direction arrow.
import type { EffectiveZone } from './graphModel';
import type { MapNeighborItem, NeighborCenter } from '../../api/types';
import { PlaneBadge } from '../../ui/NodePreview';
import { baseName } from '../../ui/nodeDetail';
import { typeIcon, typeLabel } from '../../ui/nodeTypes';

// A rel-zone heading: humanised rel + true zone size ("similar · 12"). `similar` already reads as a
// word; other rels swap underscores for spaces.
function zoneHeading(rel: string): string {
  return rel.replace(/_/g, ' ');
}

export function MapList({
  center,
  zones,
  entityTypes,
  onRecenter,
  onOpenCenter,
  onShowMore,
  showMoreBusy,
}: {
  center: NeighborCenter;
  zones: EffectiveZone[];
  entityTypes: ReadonlySet<string>;
  onRecenter: (nodeId: string) => void;
  onOpenCenter: () => void;
  onShowMore: (rel: string, cursor: string | null) => void;
  showMoreBusy: ReadonlySet<string>;
}) {
  const isHub = (type: string | null): boolean => (type ? entityTypes.has(type) : false);

  return (
    <div style={{ height: '100%', overflowY: 'auto', padding: '4px 4px 12px' }}>
      {/* Focal header — the list's stand-in for the canvas caption chip; tap to read the full node. */}
      <button
        onClick={onOpenCenter}
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 10,
          width: '100%',
          textAlign: 'left',
          padding: '12px 14px',
          marginBottom: 14,
          borderRadius: 'var(--radius)',
          border: '1px solid var(--surface-border)',
          background: 'linear-gradient(135deg, color-mix(in srgb, var(--accent) 14%, var(--surface)), var(--surface))',
          color: 'var(--text)',
        }}
      >
        <span aria-hidden style={{ fontSize: 22 }}>
          {typeIcon(center.type)}
        </span>
        <span style={{ minWidth: 0, flex: 1 }}>
          <span
            style={{
              display: 'block',
              fontWeight: 700,
              fontSize: 16,
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
            }}
          >
            {center.title ?? baseName(center.node_id)}
          </span>
          <span style={{ fontSize: 12, color: 'var(--muted)' }}>
            {typeLabel(center.type)} · tap to open
          </span>
        </span>
        <PlaneBadge plane={center.plane} />
      </button>

      {zones.length === 0 ? (
        <p style={{ margin: 0, padding: '0 10px', fontSize: 14, color: 'var(--muted)' }}>
          No connections yet.
        </p>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 18 }}>
          {zones.map((zone) => {
            const remaining = zone.total - zone.neighbors.length;
            const busy = showMoreBusy.has(zone.rel);
            return (
              <section key={zone.rel}>
                <h2
                  style={{
                    display: 'flex',
                    alignItems: 'baseline',
                    gap: 8,
                    margin: '0 0 8px',
                    padding: '0 6px',
                    fontSize: 12,
                    fontWeight: 700,
                    letterSpacing: 0.6,
                    textTransform: 'uppercase',
                    color: 'var(--muted)',
                  }}
                >
                  {zoneHeading(zone.rel)}
                  <span style={{ fontWeight: 500 }}>{zone.total}</span>
                </h2>
                <ul style={{ listStyle: 'none', margin: 0, padding: 0, display: 'flex', flexDirection: 'column', gap: 6 }}>
                  {zone.neighbors.map((n, i) => (
                    <li key={`${n.node_id}:${i}`}>
                      <NeighborRow neighbor={n} hub={isHub(n.type)} onRecenter={onRecenter} />
                    </li>
                  ))}
                </ul>
                {remaining > 0 && (
                  <button
                    onClick={() => onShowMore(zone.rel, zone.next_cursor)}
                    disabled={busy}
                    style={{
                      marginTop: 8,
                      marginLeft: 6,
                      padding: '6px 14px',
                      borderRadius: 999,
                      border: '1px solid var(--surface-border)',
                      background: 'color-mix(in srgb, var(--accent) 12%, transparent)',
                      color: 'var(--accent)',
                      fontSize: 13,
                      fontWeight: 600,
                      opacity: busy ? 0.55 : 1,
                    }}
                  >
                    {busy ? 'Loading…' : `Show ${remaining} more`}
                  </button>
                )}
              </section>
            );
          })}
        </div>
      )}
    </div>
  );
}

// One tappable neighbor row. Origin/until drive the edge-style hint so the list carries the same
// canonical / derived / superseded distinction the canvas edges do (ADR-051 §6).
function NeighborRow({
  neighbor,
  hub,
  onRecenter,
}: {
  neighbor: MapNeighborItem;
  hub: boolean;
  onRecenter: (nodeId: string) => void;
}) {
  const derived = neighbor.origin === 'derived';
  const superseded = neighbor.until != null;
  const faint = derived || superseded;
  const arrow = neighbor.dir === 'in' ? '←' : '→';

  return (
    <button
      onClick={() => onRecenter(neighbor.node_id)}
      title={neighbor.title ?? baseName(neighbor.node_id)}
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 10,
        width: '100%',
        textAlign: 'left',
        padding: '10px 12px',
        borderRadius: 'var(--radius)',
        border: superseded ? '1px dashed var(--surface-border)' : '1px solid var(--surface-border)',
        background: 'var(--surface)',
        color: 'var(--text)',
        opacity: faint ? 0.7 : 1,
      }}
    >
      <span aria-hidden style={{ fontSize: 18 }}>
        {typeIcon(neighbor.type)}
      </span>
      <span style={{ minWidth: 0, flex: 1, display: 'flex', flexDirection: 'column', gap: 2 }}>
        <span
          style={{
            fontWeight: hub ? 700 : 600,
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            whiteSpace: 'nowrap',
          }}
        >
          {neighbor.title ?? baseName(neighbor.node_id)}
        </span>
        {(derived || superseded || neighbor.since) && (
          <span style={{ fontSize: 11, color: 'var(--muted)' }}>
            {derived && 'similar'}
            {superseded && `until ${neighbor.until}`}
            {!derived && !superseded && neighbor.since && `since ${neighbor.since}`}
          </span>
        )}
      </span>
      <PlaneBadge plane={neighbor.plane} />
      <span aria-hidden style={{ color: 'var(--muted)', fontSize: 14, flexShrink: 0 }}>
        {arrow}
      </span>
    </button>
  );
}
