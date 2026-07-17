// Transform a center + its rel-keyed zones (ADR-052) into force-graph nodes + links, assigning each
// zone its own angular sector so a custom d3 force can settle "people one side, topics another"
// (ADR-051 §3). The client only ever holds one node's neighborhood (re-center, never accumulate).
import type { Interiority, MapNeighborItem, NeighborCenter } from '../../api/types';

export interface MapNode {
  id: string;
  kind: 'center' | 'neighbor' | 'more';
  title: string | null;
  type: string | null;
  plane: string | null;
  // Inner-voice dimension (M8.2 T3.5, ADR-055 §3c) — drives the canvas inner-voice ring; undefined
  // on `more` affordance nodes.
  interiority?: Interiority;
  // Neighbor edge metadata (drives link styling); undefined on center + more nodes.
  neighbor?: MapNeighborItem;
  // "show more" affordance: which zone + how many remain + the cursor to fetch the next page.
  rel?: string;
  remaining?: number;
  cursor?: string | null;
  // Force target = the node's zone sector (read by the custom 'zone' force in MapCanvas).
  tx?: number;
  ty?: number;
  // d3-force mutates these in place; the center is pinned via fx/fy.
  x?: number;
  y?: number;
  vx?: number;
  vy?: number;
  fx?: number;
  fy?: number;
}

export interface MapLink {
  source: string;
  target: string;
  origin: 'canonical' | 'derived';
  rel: string;
  dir: 'out' | 'in';
  since: string | null;
  until: string | null;
  more: boolean;
}

// One effective zone (query zone + any locally appended "show more" neighbors).
export interface EffectiveZone {
  rel: string;
  neighbors: MapNeighborItem[];
  total: number;
  next_cursor: string | null;
}

const BASE_RADIUS = 150;
const RING_STEP = 26;

function clamp(v: number, lo: number, hi: number): number {
  return Math.max(lo, Math.min(hi, v));
}

export interface GraphData {
  nodes: MapNode[];
  links: MapLink[];
}

export function buildGraph(center: NeighborCenter, zones: EffectiveZone[]): GraphData {
  const nodes: MapNode[] = [
    {
      id: center.node_id,
      kind: 'center',
      title: center.title,
      type: center.type,
      plane: center.plane,
      interiority: center.interiority,
      x: 0,
      y: 0,
      fx: 0,
      fy: 0,
    },
  ];
  const links: MapLink[] = [];
  const seen = new Set<string>([center.node_id]);

  const zoneCount = Math.max(zones.length, 1);
  zones.forEach((zone, zi) => {
    const baseAngle = -Math.PI / 2 + (2 * Math.PI * zi) / zoneCount;
    const halfWidth = clamp((Math.PI / zoneCount) * 0.85, 0.15, 0.7);
    const k = zone.neighbors.length;

    zone.neighbors.forEach((n, j) => {
      const angle = k <= 1 ? baseAngle : baseAngle + (j / (k - 1) - 0.5) * 2 * halfWidth;
      const radius = BASE_RADIUS + (j % 3) * RING_STEP;
      const tx = radius * Math.cos(angle);
      const ty = radius * Math.sin(angle);
      // A node can be reached by more than one edge; keep the first, but still draw every edge.
      if (!seen.has(n.node_id)) {
        seen.add(n.node_id);
        nodes.push({
          id: n.node_id,
          kind: 'neighbor',
          title: n.title,
          type: n.type,
          plane: n.plane,
          interiority: n.interiority,
          neighbor: n,
          rel: zone.rel,
          tx,
          ty,
          // Start near the center so the sim fans them outward (plex "fans in and settles").
          x: tx * 0.35,
          y: ty * 0.35,
        });
      }
      // Edge direction decides the arrow: out = center→neighbor, in = neighbor→center.
      links.push(
        n.dir === 'in'
          ? {
              source: n.node_id,
              target: center.node_id,
              origin: n.origin,
              rel: n.rel,
              dir: n.dir,
              since: n.since,
              until: n.until,
              more: false,
            }
          : {
              source: center.node_id,
              target: n.node_id,
              origin: n.origin,
              rel: n.rel,
              dir: n.dir,
              since: n.since,
              until: n.until,
              more: false,
            },
      );
    });

    // A per-zone "show more" node when the zone has overflow (ADR-051 §2 per-zone paging).
    const remaining = zone.total - k;
    if (remaining > 0) {
      const outer = BASE_RADIUS + Math.ceil(k / 3) * RING_STEP + 78;
      const moreId = `more:${zone.rel}`;
      nodes.push({
        id: moreId,
        kind: 'more',
        title: null,
        type: null,
        plane: null,
        rel: zone.rel,
        remaining,
        cursor: zone.next_cursor,
        tx: outer * Math.cos(baseAngle),
        ty: outer * Math.sin(baseAngle),
        x: outer * 0.35 * Math.cos(baseAngle),
        y: outer * 0.35 * Math.sin(baseAngle),
      });
      links.push({
        source: center.node_id,
        target: moreId,
        origin: 'canonical',
        rel: zone.rel,
        dir: 'out',
        since: null,
        until: null,
        more: true,
      });
    }
  });

  return { nodes, links };
}
