// The zoned force canvas (ADR-051 §3/§4/§6, ADR-052). react-force-graph-2d (2D canvas only,
// ADR-032 #12) with a pinned focal node, a custom per-zone directional force ("people one side,
// topics another"), emoji=type marks, plane-coloured halos + hub rings, and canonical/derived/
// superseded edge styling. Single-click a neighbor = re-center; click the focal = open the drawer;
// click a "+N" node = show more of that zone. Hover peek uses the library's native tooltips.
import { useEffect, useMemo, useRef } from 'react';
import ForceGraph2D, { type ForceGraphMethods } from 'react-force-graph-2d';
import { useTheme } from '../../theme/theme-context';
import { THEMES } from '../../theme/themes';
import { edgeLabel, typeIcon } from '../../ui/nodeTypes';
import { planeColor } from '../../ui/planeColors';
import type { GraphData, MapLink, MapNode } from './graphModel';

function rgba(hex: string, a: number): string {
  const h = hex.replace('#', '');
  const full = h.length === 3
    ? h.split('').map((c) => c + c).join('')
    : h;
  const n = parseInt(full, 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${a})`;
}

// A d3-force with the optional configurators we call. The library doesn't export its ForceFn type,
// so we name the minimal surface we use (the custom 'zone' force + charge/link tuning).
interface D3Force {
  (alpha: number): void;
  initialize?: (nodes: MapNode[]) => void;
  strength?: (v: number) => unknown;
  distance?: (v: number) => unknown;
}

// Node radii (graph units — they scale with zoom together with the marks).
const R_CENTER = 15;
const R_HUB = 10;
const R_CONTENT = 7;

interface MapCanvasProps {
  data: GraphData;
  focalId: string;
  width: number;
  height: number;
  entityTypes: ReadonlySet<string>;
  onRecenter: (nodeId: string) => void;
  onOpenCenter: () => void;
  onShowMore: (rel: string, cursor: string | null) => void;
}

type FgRef = ForceGraphMethods<MapNode, MapLink>;

export function MapCanvas({
  data,
  focalId,
  width,
  height,
  entityTypes,
  onRecenter,
  onOpenCenter,
  onShowMore,
}: MapCanvasProps) {
  const fgRef = useRef<FgRef | undefined>(undefined);
  const { themeId } = useTheme();
  const tokens = THEMES[themeId].tokens;

  // Theme-dependent palette (edges, focal accent, label chrome). Plane colours are theme-independent
  // (ui/planeColors). Kept in a ref so the per-frame draw callbacks read fresh values without
  // rebuilding the graph on a theme switch.
  const palette = useMemo(
    () => ({
      accent: tokens.accent,
      text: tokens.text,
      canonical: rgba(tokens.text, 0.5),
      derived: rgba(tokens.text, 0.24),
      superseded: rgba(tokens.text, 0.3),
      more: rgba(tokens.accent, 0.5),
      labelBg: tokens.scheme === 'dark' ? 'rgba(12,10,20,0.72)' : 'rgba(255,255,255,0.82)',
    }),
    [tokens],
  );
  const paletteRef = useRef(palette);
  paletteRef.current = palette;
  const entityRef = useRef(entityTypes);
  entityRef.current = entityTypes;

  const isHub = (n: MapNode): boolean => (n.type ? entityRef.current.has(n.type) : false);

  // Wire the force layout once the kapsule has initialised: a custom 'zone' force pulls each
  // neighbor toward its sector target (tx/ty), keeping the stock charge + link forces for organic
  // spacing. The focal node is pinned (fx/fy=0) so it never drifts.
  useEffect(() => {
    const fg = fgRef.current;
    if (!fg) return;
    const store: { nodes: MapNode[] } = { nodes: [] };
    const zoneForce: D3Force = (alpha: number) => {
      for (const n of store.nodes) {
        if (n.fx != null) continue;
        if (n.tx == null || n.ty == null) continue;
        n.vx = (n.vx ?? 0) + (n.tx - (n.x ?? 0)) * 0.09 * alpha;
        n.vy = (n.vy ?? 0) + (n.ty - (n.y ?? 0)) * 0.09 * alpha;
      }
    };
    zoneForce.initialize = (nodes) => {
      store.nodes = nodes;
    };
    (fg.d3Force as (name: string, fn: D3Force) => void)('zone', zoneForce);
    (fg.d3Force('charge') as unknown as D3Force | undefined)?.strength?.(-170);
    (fg.d3Force('link') as unknown as D3Force | undefined)?.distance?.(150);
  }, []);

  // Re-center: reheat the sim and fit the fresh neighborhood into view once it settles.
  const fitPending = useRef(false);
  useEffect(() => {
    const fg = fgRef.current;
    if (!fg) return;
    fitPending.current = true;
    fg.d3ReheatSimulation();
  }, [focalId]);

  const drawNode = (node: MapNode, ctx: CanvasRenderingContext2D, scale: number) => {
    const pal = paletteRef.current;
    const x = node.x ?? 0;
    const y = node.y ?? 0;

    if (node.kind === 'more') {
      const label = `+${node.remaining ?? 0}`;
      ctx.font = '7px sans-serif';
      const w = ctx.measureText(label).width + 10;
      const h = 12;
      ctx.fillStyle = rgba(pal.accent, 0.16);
      ctx.strokeStyle = pal.more;
      ctx.lineWidth = 1;
      roundRect(ctx, x - w / 2, y - h / 2, w, h, 5);
      ctx.fill();
      ctx.stroke();
      ctx.fillStyle = pal.accent;
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText(label, x, y + 0.5);
      return;
    }

    const center = node.kind === 'center';
    const hub = isHub(node);
    const r = center ? R_CENTER : hub ? R_HUB : R_CONTENT;
    const col = center ? pal.accent : planeColor(node.plane);

    ctx.globalAlpha = center ? 0.16 : hub ? 0.28 : 0.2;
    ctx.fillStyle = col;
    ctx.beginPath();
    ctx.arc(x, y, r, 0, 2 * Math.PI);
    ctx.fill();
    ctx.globalAlpha = 1;

    ctx.lineWidth = center ? 3 : hub ? 2.4 : 1.4;
    ctx.strokeStyle = col;
    ctx.beginPath();
    ctx.arc(x, y, r, 0, 2 * Math.PI);
    ctx.stroke();

    ctx.font = `${r * 1.35}px sans-serif`;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(typeIcon(node.type), x, y + 0.5);

    // Zoom-gated title (the focal node carries its own HTML caption chip, so skip it here).
    if (!center && node.title && scale > 1.7) {
      const label = node.title.length > 26 ? `${node.title.slice(0, 25)}…` : node.title;
      ctx.font = '6px sans-serif';
      const tw = ctx.measureText(label).width;
      ctx.fillStyle = pal.labelBg;
      roundRect(ctx, x - tw / 2 - 3, y + r + 2, tw + 6, 9, 3);
      ctx.fill();
      ctx.fillStyle = pal.text;
      ctx.fillText(label, x, y + r + 7);
    }
  };

  const drawPointerArea = (node: MapNode, color: string, ctx: CanvasRenderingContext2D) => {
    const x = node.x ?? 0;
    const y = node.y ?? 0;
    ctx.fillStyle = color;
    if (node.kind === 'more') {
      roundRect(ctx, x - 14, y - 7, 28, 14, 5);
      ctx.fill();
      return;
    }
    const r = node.kind === 'center' ? R_CENTER : isHub(node) ? R_HUB : R_CONTENT;
    ctx.beginPath();
    ctx.arc(x, y, r + 2, 0, 2 * Math.PI);
    ctx.fill();
  };

  if (width === 0 || height === 0) return null;

  return (
    <ForceGraph2D<MapNode, MapLink>
      ref={fgRef}
      graphData={data}
      width={width}
      height={height}
      backgroundColor="rgba(0,0,0,0)"
      enableNodeDrag={false}
      nodeRelSize={R_CONTENT}
      nodeLabel={(n) =>
        n.kind === 'more'
          ? `Show ${n.remaining} more · ${(n.rel ?? '').replace(/_/g, ' ')}`
          : (n.title ?? '')
      }
      nodeCanvasObject={drawNode}
      nodePointerAreaPaint={drawPointerArea}
      linkColor={(l) =>
        l.more
          ? paletteRef.current.more
          : l.until
            ? paletteRef.current.superseded
            : l.origin === 'derived'
              ? paletteRef.current.derived
              : paletteRef.current.canonical
      }
      linkWidth={(l) => (l.more ? 1 : l.origin === 'derived' ? 0.8 : 1.3)}
      linkLineDash={(l) => (l.more ? [2, 3] : l.until ? [4, 3] : null)}
      linkDirectionalArrowLength={(l) => (l.more || l.origin === 'derived' ? 0 : 3.5)}
      linkDirectionalArrowRelPos={1}
      linkDirectionalArrowColor={() => paletteRef.current.canonical}
      linkLabel={(l) =>
        l.more
          ? ''
          : `${edgeLabel(l.rel, l.origin)}${l.until ? ` · until ${l.until}` : l.since ? ` · since ${l.since}` : ''}`
      }
      cooldownTicks={90}
      onEngineStop={() => {
        if (fitPending.current) {
          fitPending.current = false;
          fgRef.current?.zoomToFit(500, 70);
        }
      }}
      onNodeClick={(node) => {
        if (node.kind === 'more') onShowMore(node.rel ?? '', node.cursor ?? null);
        else if (node.kind === 'center') onOpenCenter();
        else onRecenter(node.id);
      }}
    />
  );
}

function roundRect(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  w: number,
  h: number,
  r: number,
) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}
