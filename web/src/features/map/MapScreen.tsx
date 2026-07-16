// The Map tab (06 §3c, ADR-051/052): a re-center neighborhood explorer. One focal node at a time
// with its 1-hop neighbors in rel-based zones; single-click a neighbor re-centers, breadcrumbs track
// the path, clicking the focal opens the shared NodePreview drawer, and each zone's overflow pages
// via a "+N" node. Empty state = an embedded search to start + restore-the-last-centered node.
import { AnimatePresence, motion, useReducedMotion } from 'framer-motion';
import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from 'react';
import { api } from '../../api/client';
import type { MapNeighborItem, NeighborCenter, SearchResultItem } from '../../api/types';
import { NodePreview, PlaneBadge } from '../../ui/NodePreview';
import { baseName } from '../../ui/nodeDetail';
import { typeIcon, typeLabel } from '../../ui/nodeTypes';
import { useSearch, useTypes, type Submitted } from '../search/useSearch';
import { MapCanvas } from './MapCanvas';
import { MapList } from './MapList';
import { buildGraph, type EffectiveZone, type GraphData, type MapNode } from './graphModel';
import { useNeighbors } from './useMap';

type ViewMode = 'canvas' | 'list';

const LAST_KEY = 'braindan.map.lastCenter';
const FAIL_COLOR = '#ff6b6b';

interface Crumb {
  id: string;
  title: string | null;
  type: string | null;
}

interface LastCenter {
  id: string;
  title: string | null;
  type: string | null;
}

function readLast(): LastCenter | null {
  try {
    const raw = localStorage.getItem(LAST_KEY);
    return raw ? (JSON.parse(raw) as LastCenter) : null;
  } catch {
    return null;
  }
}

// Measure the canvas host so react-force-graph gets explicit pixel dimensions. A callback ref (not
// a mount effect) so the observer attaches the moment the host actually mounts — the host lives
// behind the loading state, so a `[]`-deps effect would run while the ref is still null.
function useSize() {
  const [size, setSize] = useState({ w: 0, h: 0 });
  const roRef = useRef<ResizeObserver | null>(null);
  const ref = useCallback((el: HTMLDivElement | null) => {
    roRef.current?.disconnect();
    if (!el) return;
    const measure = () => setSize({ w: el.clientWidth, h: el.clientHeight });
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    roRef.current = ro;
  }, []);
  return { ref, size };
}

export function MapScreen({
  seed,
  onSeedConsumed,
}: {
  seed: string | null;
  onSeedConsumed: () => void;
}) {
  const [focalId, setFocalId] = useState<string | null>(null);
  const [trail, setTrail] = useState<Crumb[]>([]);
  // Per-zone "show more" appends, reset whenever the focal node changes.
  const [extra, setExtra] = useState<Record<string, { neighbors: MapNeighborItem[]; cursor: string | null }>>({});
  const [drawerOpen, setDrawerOpen] = useState(false);
  // The node we're re-centering to, known from the carried neighbor data before its own neighbors
  // fetch resolves (ADR-051 §5). Drives the caption chip + current breadcrumb during the placeholder
  // window so they show the destination immediately (the canvas keeps the old neighborhood until the
  // swap) instead of the old node appearing twice.
  const [navTarget, setNavTarget] = useState<Crumb | null>(null);

  const neighbors = useNeighbors(focalId);
  const center = neighbors.data?.center ?? null;
  const inTransit = neighbors.isPlaceholderData;
  const typesQuery = useTypes();
  const entityTypes = useMemo(
    () => new Set(typesQuery.data?.entity_like_types ?? []),
    [typesQuery.data],
  );

  // View mode: canvas (the force sim) or the tappable list (ADR-051 §7). The list is both the
  // `prefers-reduced-motion` fallback and a manual toggle — so the default follows the OS setting
  // (the sim doesn't run under reduced motion) until the user explicitly overrides it, after which
  // their choice sticks. `useReducedMotion` starts null pre-measure, so resolve it here, not in
  // initial state.
  const reducedMotion = useReducedMotion();
  const [viewOverride, setViewOverride] = useState<ViewMode | null>(null);
  const view: ViewMode = viewOverride ?? (reducedMotion ? 'list' : 'canvas');

  // A new seed (from a Search card / NodePreview edge) centers the map and starts a fresh trail.
  useEffect(() => {
    if (seed && seed !== focalId) {
      setFocalId(seed);
      setTrail([]);
      setExtra({});
      setDrawerOpen(false);
      setNavTarget({ id: seed, title: null, type: null });
    }
    if (seed) onSeedConsumed();
  }, [seed, focalId, onSeedConsumed]);

  // Drop the transit target once its own neighborhood has loaded (the swap is complete).
  useEffect(() => {
    if (center && navTarget && center.node_id === navTarget.id) setNavTarget(null);
  }, [center, navTarget]);

  // Persist the last-centered node so an empty map can offer to restore it.
  useEffect(() => {
    if (center) {
      const last: LastCenter = { id: center.node_id, title: center.title, type: center.type };
      try {
        localStorage.setItem(LAST_KEY, JSON.stringify(last));
      } catch {
        // storage unavailable — the restore affordance just won't appear
      }
    }
  }, [center]);

  // Current graph nodes, for looking up a clicked neighbor's known title/type/plane at re-center.
  const graphNodesRef = useRef<MapNode[]>([]);

  const recenter = useCallback(
    (nodeId: string) => {
      if (nodeId === focalId) return;
      // Push the current focal onto the trail (its title/type come from the loaded center header).
      if (center) {
        setTrail((t) => [...t, { id: center.node_id, title: center.title, type: center.type }]);
      }
      const n = graphNodesRef.current.find((g) => g.id === nodeId);
      setNavTarget({ id: nodeId, title: n?.title ?? null, type: n?.type ?? null });
      setFocalId(nodeId);
      setExtra({});
      setDrawerOpen(false);
    },
    [focalId, center],
  );

  // A crumb click re-centers back and truncates the trail there (forward history drops).
  const goCrumb = (index: number) => {
    const target = trail[index];
    if (!target) return;
    setTrail((t) => t.slice(0, index));
    setNavTarget(target);
    setFocalId(target.id);
    setExtra({});
    setDrawerOpen(false);
  };

  // In-flight guard: a fast double-click on a "+N" node/button must not fetch + append the same page
  // twice. The ref is the synchronous guard; `busyRels` mirrors it into render state so the list's
  // "Show N more" button can show a loading label (the canvas "+N" node ignores it).
  const showMoreBusy = useRef<Set<string>>(new Set());
  const [busyRels, setBusyRels] = useState<ReadonlySet<string>>(new Set());
  const showMore = useCallback(
    async (rel: string, cursor: string | null) => {
      if (!focalId || showMoreBusy.current.has(rel)) return;
      showMoreBusy.current.add(rel);
      setBusyRels(new Set(showMoreBusy.current));
      try {
        const page = await api.nodeNeighborPage(focalId, rel, cursor);
        setExtra((prev) => {
          const cur = prev[rel] ?? { neighbors: [], cursor: null };
          return {
            ...prev,
            [rel]: {
              neighbors: [...cur.neighbors, ...page.neighbors],
              cursor: page.next_cursor,
            },
          };
        });
      } finally {
        showMoreBusy.current.delete(rel);
        setBusyRels(new Set(showMoreBusy.current));
      }
    },
    [focalId],
  );

  // Build the force graph, reusing prior node objects for the same focal so a "show more" append
  // doesn't reset the neighborhood's positions (ADR-051 §5 — pages without refetching the layout).
  const prevRef = useRef<{ focalId: string | null; byId: Map<string, MapNode> }>({
    focalId: null,
    byId: new Map(),
  });
  // Query zones with any locally-appended "show more" neighbors merged in — the single source both
  // the canvas (buildGraph) and the list renderer read, so the two views stay in lockstep.
  const effectiveZones: EffectiveZone[] = useMemo(
    () =>
      (neighbors.data?.zones ?? []).map((z) => {
        const ex = extra[z.rel];
        return ex
          ? { rel: z.rel, neighbors: [...z.neighbors, ...ex.neighbors], total: z.total, next_cursor: ex.cursor }
          : { rel: z.rel, neighbors: z.neighbors, total: z.total, next_cursor: z.next_cursor };
      }),
    [neighbors.data, extra],
  );

  const graph: GraphData = useMemo(() => {
    if (!center) return { nodes: [], links: [] };
    const desired = buildGraph(center, effectiveZones);
    if (prevRef.current.focalId !== center.node_id) {
      prevRef.current = { focalId: center.node_id, byId: new Map() };
    }
    const byId = prevRef.current.byId;
    const nodes = desired.nodes.map((nd) => {
      const prev = byId.get(nd.id);
      if (prev) {
        prev.title = nd.title;
        prev.type = nd.type;
        prev.plane = nd.plane;
        prev.neighbor = nd.neighbor;
        prev.rel = nd.rel;
        prev.remaining = nd.remaining;
        prev.cursor = nd.cursor;
        prev.tx = nd.tx;
        prev.ty = nd.ty;
        if (nd.kind === 'center') {
          prev.fx = 0;
          prev.fy = 0;
        }
        return prev;
      }
      byId.set(nd.id, nd);
      return nd;
    });
    const wanted = new Set(desired.nodes.map((n) => n.id));
    for (const id of [...byId.keys()]) if (!wanted.has(id)) byId.delete(id);
    return { nodes, links: desired.links };
  }, [center, effectiveZones]);
  graphNodesRef.current = graph.nodes;

  const { ref: hostRef, size } = useSize();

  // While a re-center is in flight the canvas keeps the old neighborhood, but the caption + current
  // breadcrumb show the destination (from the carried neighbor data) so they never lag or double up.
  const displayCenter: NeighborCenter | null =
    inTransit && navTarget && navTarget.id === focalId
      ? {
          node_id: navTarget.id,
          type: navTarget.type ?? '',
          title: navTarget.title,
          plane: null,
          planes: [],
        }
      : center;

  const crumbs: Crumb[] = displayCenter
    ? [...trail, { id: displayCenter.node_id, title: displayCenter.title, type: displayCenter.type }]
    : trail;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: 'calc(100dvh - 150px)' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 12 }}>
        <h1 style={{ margin: 0, fontSize: 24, fontWeight: 700, letterSpacing: -0.4 }}>Map</h1>
        {focalId && (
          <Breadcrumbs crumbs={crumbs} onGo={goCrumb} onHome={() => setFocalId(null)} />
        )}
        {center && <ViewToggle view={view} onChange={setViewOverride} />}
      </div>

      {!focalId ? (
        <EmptyState onPick={(id) => setFocalId(id)} />
      ) : neighbors.isLoading ? (
        <p style={{ margin: 0, fontSize: 14, color: 'var(--muted)' }}>Loading neighborhood…</p>
      ) : neighbors.isError ? (
        <p style={{ margin: 0, fontSize: 14, color: FAIL_COLOR }}>Couldn’t load this node.</p>
      ) : !center ? (
        <p style={{ margin: 0, fontSize: 14, color: 'var(--muted)' }}>
          This node has no neighborhood.{' '}
          <button
            onClick={() => setFocalId(null)}
            style={{ background: 'none', border: 'none', color: 'var(--accent)', padding: 0 }}
          >
            Start over
          </button>
        </p>
      ) : (
        <div
          ref={hostRef}
          style={{
            position: 'relative',
            flex: 1,
            minHeight: 0,
            borderRadius: 'var(--radius-lg)',
            border: '1px solid var(--surface-border)',
            overflow: 'hidden',
          }}
        >
          {view === 'canvas' ? (
            <>
              <MapCanvas
                data={graph}
                focalId={center.node_id}
                width={size.w}
                height={size.h}
                entityTypes={entityTypes}
                onRecenter={recenter}
                onOpenCenter={() => setDrawerOpen(true)}
                onShowMore={showMore}
              />

              {/* Focal caption chip — renders immediately from the center header (no flash). */}
              <div
                style={{
                  position: 'absolute',
                  left: 14,
                  bottom: 14,
                  maxWidth: 'calc(100% - 28px)',
                  display: 'flex',
                  alignItems: 'center',
                  gap: 8,
                  padding: '8px 12px',
                  borderRadius: 'var(--radius)',
                  background: 'var(--surface)',
                  border: '1px solid var(--surface-border)',
                  backdropFilter: 'blur(18px)',
                  WebkitBackdropFilter: 'blur(18px)',
                }}
              >
                <span aria-hidden style={{ fontSize: 18 }}>
                  {typeIcon((displayCenter ?? center).type)}
                </span>
                <span style={{ minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', fontWeight: 700 }}>
                  {(displayCenter ?? center).title ?? baseName((displayCenter ?? center).node_id)}
                </span>
                <span style={{ fontSize: 12, color: 'var(--muted)' }}>
                  {typeLabel((displayCenter ?? center).type)}
                </span>
                <PlaneBadge plane={(displayCenter ?? center).plane} />
              </div>
            </>
          ) : (
            <MapList
              center={center}
              zones={effectiveZones}
              entityTypes={entityTypes}
              onRecenter={recenter}
              onOpenCenter={() => setDrawerOpen(true)}
              onShowMore={showMore}
              showMoreBusy={busyRels}
            />
          )}

          <AnimatePresence>
            {drawerOpen && (
              <MapDrawer
                nodeId={center.node_id}
                title={center.title ?? baseName(center.node_id)}
                type={center.type}
                onClose={() => setDrawerOpen(false)}
                onOpenNode={(id) => {
                  setDrawerOpen(false);
                  recenter(id);
                }}
              />
            )}
          </AnimatePresence>
        </div>
      )}
    </div>
  );
}

function Breadcrumbs({
  crumbs,
  onGo,
  onHome,
}: {
  crumbs: Crumb[];
  onGo: (index: number) => void;
  onHome: () => void;
}) {
  return (
    <nav
      aria-label="Map path"
      style={{ display: 'flex', alignItems: 'center', gap: 4, overflowX: 'auto', minWidth: 0, flex: 1 }}
    >
      <button
        onClick={onHome}
        title="Back to start"
        style={{ background: 'none', border: 'none', color: 'var(--muted)', padding: '2px 4px', flexShrink: 0 }}
      >
        ⌕
      </button>
      {crumbs.map((c, i) => {
        const last = i === crumbs.length - 1;
        return (
          <span key={`${c.id}:${i}`} style={{ display: 'flex', alignItems: 'center', gap: 4, flexShrink: 0 }}>
            <span aria-hidden style={{ color: 'var(--muted)', fontSize: 11 }}>
              ›
            </span>
            <button
              onClick={() => !last && onGo(i)}
              disabled={last}
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: 5,
                maxWidth: 180,
                background: 'none',
                border: 'none',
                padding: '2px 4px',
                color: last ? 'var(--text)' : 'var(--muted)',
                fontWeight: last ? 700 : 500,
                cursor: last ? 'default' : 'pointer',
              }}
            >
              <span aria-hidden>{typeIcon(c.type)}</span>
              <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {c.title ?? baseName(c.id)}
              </span>
            </button>
          </span>
        );
      })}
    </nav>
  );
}

// Canvas ⇄ list toggle (ADR-051 §7). Manual override on top of the reduced-motion default.
function ViewToggle({ view, onChange }: { view: ViewMode; onChange: (v: ViewMode) => void }) {
  const opts: { id: ViewMode; icon: string; label: string }[] = [
    { id: 'canvas', icon: '✷', label: 'Canvas' },
    { id: 'list', icon: '☰', label: 'List' },
  ];
  return (
    <div
      role="group"
      aria-label="Map view"
      style={{
        display: 'flex',
        flexShrink: 0,
        gap: 2,
        padding: 2,
        borderRadius: 999,
        border: '1px solid var(--surface-border)',
        background: 'var(--surface)',
      }}
    >
      {opts.map((o) => {
        const active = o.id === view;
        return (
          <button
            key={o.id}
            onClick={() => onChange(o.id)}
            aria-pressed={active}
            title={`${o.label} view`}
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 5,
              padding: '5px 10px',
              borderRadius: 999,
              border: 'none',
              background: active ? 'var(--accent)' : 'transparent',
              color: active ? 'var(--on-accent)' : 'var(--muted)',
              fontSize: 12,
              fontWeight: 600,
            }}
          >
            <span aria-hidden>{o.icon}</span>
            {o.label}
          </button>
        );
      })}
    </div>
  );
}

// Right-side drawer reusing the shared NodePreview (rule 10). Its edge rows re-center the map.
function MapDrawer({
  nodeId,
  title,
  type,
  onClose,
  onOpenNode,
}: {
  nodeId: string;
  title: string;
  type: string | null;
  onClose: () => void;
  onOpenNode: (nodeId: string) => void;
}) {
  return (
    <>
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        onClick={onClose}
        style={{ position: 'absolute', inset: 0, background: 'rgba(0,0,0,0.32)' }}
      />
      <motion.div
        initial={{ x: '100%' }}
        animate={{ x: 0 }}
        exit={{ x: '100%' }}
        transition={{ type: 'spring', stiffness: 320, damping: 34 }}
        style={{
          position: 'absolute',
          top: 0,
          right: 0,
          bottom: 0,
          width: 'min(440px, 92%)',
          overflowY: 'auto',
          padding: 20,
          background: 'var(--surface)',
          borderLeft: '1px solid var(--surface-border)',
          backdropFilter: 'blur(22px)',
          WebkitBackdropFilter: 'blur(22px)',
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <span aria-hidden style={{ fontSize: 20 }}>
            {typeIcon(type)}
          </span>
          <h2 style={{ margin: 0, flex: 1, fontSize: 18, fontWeight: 700, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis' }}>
            {title}
          </h2>
          <button
            onClick={onClose}
            aria-label="Close"
            style={{ background: 'none', border: 'none', color: 'var(--muted)', fontSize: 20, padding: 4 }}
          >
            ✕
          </button>
        </div>
        <NodePreview nodeId={nodeId} onOpenNode={onOpenNode} />
      </motion.div>
    </>
  );
}

// Empty state: an embedded search to pick a starting node, plus a restore-last-centered shortcut.
function EmptyState({ onPick }: { onPick: (nodeId: string) => void }) {
  const [query, setQuery] = useState('');
  const [submitted, setSubmitted] = useState<Submitted | null>(null);
  const results = useSearch(submitted);
  const last = useMemo(() => readLast(), []);

  const submit = (e: FormEvent) => {
    e.preventDefault();
    const q = query.trim();
    if (q) setSubmitted({ query: q, planes: [], types: [] });
  };

  return (
    <div style={{ display: 'grid', gap: 16, maxWidth: 640 }}>
      <p style={{ margin: 0, fontSize: 15, color: 'var(--muted)', lineHeight: 1.5 }}>
        Search your graph to start exploring — pick a result to drop into its constellation.
      </p>

      {last && (
        <button
          onClick={() => onPick(last.id)}
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 8,
            alignSelf: 'start',
            padding: '8px 14px',
            borderRadius: 999,
            border: '1px solid var(--surface-border)',
            background: 'var(--surface)',
            color: 'var(--text)',
          }}
        >
          <span aria-hidden>↩</span>
          <span style={{ color: 'var(--muted)', fontSize: 13 }}>Return to</span>
          <span aria-hidden>{typeIcon(last.type)}</span>
          <span style={{ fontWeight: 600 }}>{last.title ?? baseName(last.id)}</span>
        </button>
      )}

      <form onSubmit={submit} style={{ display: 'flex', gap: 8 }}>
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search your graph…"
          aria-label="Search to start the map"
          style={{
            flex: 1,
            padding: '12px 16px',
            borderRadius: 'var(--radius)',
            border: '1px solid var(--surface-border)',
            background: 'var(--surface)',
            color: 'var(--text)',
            fontSize: 15,
            outline: 'none',
          }}
        />
        <motion.button
          type="submit"
          whileTap={{ scale: 0.95 }}
          disabled={query.trim() === ''}
          style={{
            padding: '0 18px',
            borderRadius: 'var(--radius)',
            border: 'none',
            background: 'linear-gradient(135deg, var(--accent), var(--accent-2))',
            color: 'var(--on-accent)',
            fontSize: 15,
            fontWeight: 600,
            opacity: query.trim() === '' ? 0.5 : 1,
          }}
        >
          Search
        </motion.button>
      </form>

      {submitted && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          {results.isLoading ? (
            <p style={{ margin: 0, fontSize: 14, color: 'var(--muted)' }}>Searching…</p>
          ) : results.isError ? (
            <p style={{ margin: 0, fontSize: 14, color: FAIL_COLOR }}>Search failed — try again.</p>
          ) : !results.data || results.data.length === 0 ? (
            <p style={{ margin: 0, fontSize: 14, color: 'var(--muted)' }}>No matches.</p>
          ) : (
            results.data.map((hit: SearchResultItem) => (
              <button
                key={hit.node_id}
                onClick={() => onPick(hit.node_id)}
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 10,
                  textAlign: 'left',
                  padding: '10px 14px',
                  borderRadius: 'var(--radius)',
                  border: '1px solid var(--surface-border)',
                  background: 'var(--surface)',
                  color: 'var(--text)',
                }}
              >
                <span aria-hidden style={{ fontSize: 18 }}>
                  {typeIcon(hit.type)}
                </span>
                <span style={{ minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', fontWeight: 600 }}>
                  {hit.title ?? baseName(hit.store_path)}
                </span>
                <span style={{ marginLeft: 'auto', flexShrink: 0 }}>
                  <PlaneBadge plane={hit.plane} />
                </span>
              </button>
            ))
          )}
        </div>
      )}
    </div>
  );
}
