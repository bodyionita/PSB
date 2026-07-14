import { AnimatePresence, motion } from 'framer-motion';
import { useState, type FormEvent } from 'react';
import { ApiError } from '../../api/client';
import type { NodeEdgeItem, SearchResultItem } from '../../api/types';
import { Surface } from '../../ui/Surface';
import { edgeLabel, typeIcon, typeLabel } from '../../ui/nodeTypes';
import { useNode, usePlanes, useSearch, useTypes, type Submitted } from './useSearch';

// Search tab (06 §5): semantic search over the whole graph — no LLM. Query box + plane/type filter
// chips, node cards (type icon + plane badge + snippet), and a read-only node preview on expand
// (body + derived entity profile + canonical/derived edges).

const FAIL_COLOR = '#ff6b6b';

function baseName(path: string): string {
  const parts = path.split('/');
  return (parts[parts.length - 1] ?? path).replace(/\.md$/, '');
}

function PlaneBadge({ plane }: { plane: string | null }) {
  if (!plane) return null;
  return (
    <span
      style={{
        fontSize: 11,
        fontWeight: 700,
        letterSpacing: 0.4,
        textTransform: 'uppercase',
        color: 'var(--accent)',
        background: 'var(--surface)',
        border: '1px solid var(--surface-border)',
        borderRadius: 999,
        padding: '3px 9px',
        whiteSpace: 'nowrap',
      }}
    >
      {plane}
    </span>
  );
}

function ScorePill({ score }: { score: number }) {
  return (
    <span
      title={`relevance ${score.toFixed(3)}`}
      style={{ fontSize: 11, color: 'var(--muted)', fontVariantNumeric: 'tabular-nums' }}
    >
      {score.toFixed(2)}
    </span>
  );
}

function TagRow({ tags }: { tags: string[] }) {
  if (tags.length === 0) return null;
  return (
    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginTop: 10 }}>
      {tags.map((t) => (
        <span
          key={t}
          style={{
            fontSize: 11,
            color: 'var(--muted)',
            border: '1px solid var(--surface-border)',
            borderRadius: 999,
            padding: '2px 8px',
          }}
        >
          #{t}
        </span>
      ))}
    </div>
  );
}

function SectionLabel({ children }: { children: string }) {
  return (
    <p
      style={{
        margin: '0 0 8px',
        fontSize: 11,
        fontWeight: 700,
        letterSpacing: 0.6,
        textTransform: 'uppercase',
        color: 'var(--muted)',
      }}
    >
      {children}
    </p>
  );
}

// One edge rendered as a jump-off chip (the Map makes these navigable at M7). Canonical edges are
// solid + labelled by their `rel`; derived similarity edges are faint.
function EdgeChip({ edge }: { edge: NodeEdgeItem }) {
  const derived = edge.origin === 'derived';
  const arrow = edge.dir === 'in' ? '←' : '→';
  return (
    <span
      title={`${edge.dir === 'in' ? 'from' : 'to'} ${edge.title ?? edge.node_id}${
        edge.since ? ` · since ${edge.since}` : ''
      }`}
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 6,
        fontSize: 12,
        color: derived ? 'var(--muted)' : 'var(--text)',
        background: 'var(--surface)',
        border: derived ? '1px dashed var(--surface-border)' : '1px solid var(--surface-border)',
        borderRadius: 999,
        padding: '4px 10px',
        maxWidth: '100%',
        opacity: derived ? 0.75 : 1,
      }}
    >
      <span aria-hidden style={{ fontSize: 10, color: 'var(--muted)' }}>
        {arrow}
      </span>
      <span
        style={{
          fontSize: 10,
          fontWeight: 700,
          letterSpacing: 0.4,
          textTransform: 'uppercase',
          color: 'var(--accent)',
        }}
      >
        {edgeLabel(edge.rel, edge.origin)}
      </span>
      <span aria-hidden>{typeIcon(edge.type)}</span>
      <span
        style={{
          minWidth: 0,
          overflow: 'hidden',
          textOverflow: 'ellipsis',
          whiteSpace: 'nowrap',
        }}
      >
        {edge.title ?? baseName(edge.node_id)}
      </span>
    </span>
  );
}

// The read-only preview shown when a card is expanded (GET /nodes/{id}): body read live from the
// graph store, the derived entity profile (entity nodes only), and the node's edges — canonical
// (typed) + derived (similarity), both directions.
function NodePreview({ nodeId }: { nodeId: string }) {
  const { data, isLoading, isError } = useNode(nodeId);

  if (isLoading) {
    return <p style={{ margin: '12px 0 0', fontSize: 13, color: 'var(--muted)' }}>Loading node…</p>;
  }
  if (isError || !data) {
    return (
      <p style={{ margin: '12px 0 0', fontSize: 13, color: FAIL_COLOR }}>Couldn’t load this node.</p>
    );
  }

  const canonical = data.edges.filter((e) => e.origin === 'canonical');
  const derived = data.edges.filter((e) => e.origin === 'derived');

  return (
    <motion.div
      initial={{ opacity: 0, height: 0 }}
      animate={{ opacity: 1, height: 'auto' }}
      exit={{ opacity: 0, height: 0 }}
      transition={{ duration: 0.22, ease: 'easeOut' }}
      style={{ overflow: 'hidden' }}
    >
      <div style={{ marginTop: 14, paddingTop: 14, borderTop: '1px solid var(--surface-border)' }}>
        {/* Entity identity line — disambiguator + known aliases (entity nodes only, ADR-030). */}
        {(data.disambig || data.aliases.length > 0) && (
          <p style={{ margin: '0 0 12px', fontSize: 13, color: 'var(--muted)', lineHeight: 1.5 }}>
            {data.disambig && <span>{data.disambig}</span>}
            {data.disambig && data.aliases.length > 0 && <span> · </span>}
            {data.aliases.length > 0 && <span>also known as {data.aliases.join(', ')}</span>}
          </p>
        )}

        {/* Derived entity profile (ADR-030) — categorized observation lines, entity nodes only. */}
        {data.profile && (
          <div style={{ marginBottom: 14 }}>
            <SectionLabel>Profile</SectionLabel>
            <pre
              style={{
                margin: 0,
                whiteSpace: 'pre-wrap',
                wordBreak: 'break-word',
                fontFamily: 'inherit',
                fontSize: 13,
                lineHeight: 1.6,
                color: 'var(--text)',
              }}
            >
              {data.profile.trim()}
            </pre>
          </div>
        )}

        <pre
          style={{
            margin: 0,
            maxHeight: 320,
            overflow: 'auto',
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-word',
            fontFamily: 'inherit',
            fontSize: 13.5,
            lineHeight: 1.6,
            color: 'var(--text)',
          }}
        >
          {data.body.trim() || '(no body)'}
        </pre>

        {canonical.length > 0 && (
          <div style={{ marginTop: 14 }}>
            <SectionLabel>Connections</SectionLabel>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
              {canonical.map((e) => (
                <EdgeChip key={`${e.dir}:${e.rel}:${e.node_id}`} edge={e} />
              ))}
            </div>
          </div>
        )}

        {derived.length > 0 && (
          <div style={{ marginTop: 14 }}>
            <SectionLabel>Similar</SectionLabel>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
              {derived.map((e) => (
                <EdgeChip key={`${e.dir}:sim:${e.node_id}`} edge={e} />
              ))}
            </div>
          </div>
        )}
      </div>
    </motion.div>
  );
}

function NodeCard({ hit }: { hit: SearchResultItem }) {
  const [open, setOpen] = useState(false);
  const title = hit.title ?? baseName(hit.store_path);

  return (
    <motion.div layout initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }}>
      <Surface padding={16} style={{ borderRadius: 'var(--radius)' }}>
        <button
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          style={{
            display: 'block',
            width: '100%',
            textAlign: 'left',
            background: 'transparent',
            border: 'none',
            padding: 0,
            color: 'inherit',
          }}
        >
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
              gap: 10,
            }}
          >
            <span
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: 8,
                minWidth: 0,
                fontSize: 16,
                fontWeight: 700,
                letterSpacing: -0.2,
              }}
            >
              <span aria-hidden title={typeLabel(hit.type)} style={{ flexShrink: 0 }}>
                {typeIcon(hit.type)}
              </span>
              <span style={{ minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis' }}>
                {title}
              </span>
            </span>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexShrink: 0 }}>
              <PlaneBadge plane={hit.plane} />
              <ScorePill score={hit.score} />
            </div>
          </div>

          <p
            style={{
              margin: '10px 0 0',
              fontSize: 14,
              lineHeight: 1.5,
              color: 'var(--muted)',
              display: '-webkit-box',
              WebkitLineClamp: open ? 'unset' : 3,
              WebkitBoxOrient: 'vertical',
              overflow: 'hidden',
            }}
          >
            {hit.snippet}
          </p>

          <TagRow tags={hit.tags} />
        </button>

        <AnimatePresence initial={false}>
          {open && <NodePreview nodeId={hit.node_id} />}
        </AnimatePresence>
      </Surface>
    </motion.div>
  );
}

function FilterChips({
  values,
  selected,
  onToggle,
  label,
}: {
  values: string[];
  selected: ReadonlySet<string>;
  onToggle: (value: string) => void;
  label: string;
}) {
  return (
    <div
      role="group"
      aria-label={label}
      style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}
    >
      {values.map((v) => {
        const on = selected.has(v);
        return (
          <motion.button
            key={v}
            onClick={() => onToggle(v)}
            whileTap={{ scale: 0.94 }}
            aria-pressed={on}
            style={{
              fontSize: 12,
              fontWeight: 600,
              padding: '6px 12px',
              borderRadius: 999,
              border: on ? '1px solid var(--accent)' : '1px solid var(--surface-border)',
              background: on ? 'var(--accent)' : 'transparent',
              color: on ? 'var(--on-accent)' : 'var(--muted)',
            }}
          >
            {v}
          </motion.button>
        );
      })}
    </div>
  );
}

export function SearchScreen() {
  const [query, setQuery] = useState('');
  const [planes, setPlanes] = useState<ReadonlySet<string>>(new Set());
  const [types, setTypes] = useState<ReadonlySet<string>>(new Set());
  const [submitted, setSubmitted] = useState<Submitted | null>(null);

  const planesQuery = usePlanes();
  const typesQuery = useTypes();
  const results = useSearch(submitted);

  const allPlanes = planesQuery.data
    ? [...planesQuery.data.planes, planesQuery.data.inbox]
    : [];
  const allTypes = typesQuery.data?.node_types ?? [];

  const runWith = (planeSet: ReadonlySet<string>, typeSet: ReadonlySet<string>) => {
    const q = query.trim();
    if (q === '') return;
    setSubmitted({ query: q, planes: [...planeSet].sort(), types: [...typeSet].sort() });
  };

  const submit = (e: FormEvent) => {
    e.preventDefault();
    runWith(planes, types);
  };

  // Toggling a chip re-runs the current search immediately (once one has been run), so the filter
  // feels live; before the first search it just stages the selection.
  const toggle = (
    value: string,
    set: ReadonlySet<string>,
    setState: (s: ReadonlySet<string>) => void,
    other: ReadonlySet<string>,
    isPlane: boolean,
  ) => {
    const next = new Set(set);
    if (next.has(value)) next.delete(value);
    else next.add(value);
    setState(next);
    if (submitted) {
      setSubmitted({
        query: submitted.query,
        planes: [...(isPlane ? next : other)].sort(),
        types: [...(isPlane ? other : next)].sort(),
      });
    }
  };

  const embedDown = results.error instanceof ApiError && results.error.status === 503;
  const filtered = planes.size > 0 || types.size > 0;

  return (
    <div style={{ display: 'grid', gap: 16 }}>
      <h1 style={{ margin: 0, fontSize: 24, fontWeight: 700, letterSpacing: -0.4 }}>Search</h1>

      <form onSubmit={submit} style={{ display: 'flex', gap: 8 }}>
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search your whole brain…"
          aria-label="Search query"
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

      {allPlanes.length > 0 && (
        <FilterChips
          label="Filter by plane"
          values={allPlanes}
          selected={planes}
          onToggle={(p) => toggle(p, planes, setPlanes, types, true)}
        />
      )}
      {allTypes.length > 0 && (
        <FilterChips
          label="Filter by type"
          values={allTypes}
          selected={types}
          onToggle={(t) => toggle(t, types, setTypes, planes, false)}
        />
      )}

      {submitted && (
        <section>
          {results.isLoading ? (
            <p style={{ margin: 0, fontSize: 14, color: 'var(--muted)' }}>Searching…</p>
          ) : embedDown ? (
            <p style={{ margin: 0, fontSize: 14, color: FAIL_COLOR }}>
              Search is warming up (embeddings) — try again in a moment.
            </p>
          ) : results.isError ? (
            <p style={{ margin: 0, fontSize: 14, color: FAIL_COLOR }}>Search failed — try again.</p>
          ) : !results.data || results.data.length === 0 ? (
            <p style={{ margin: 0, fontSize: 14, color: 'var(--muted)' }}>
              No matches. Try different words{filtered ? ' or clear the filters' : ''}.
            </p>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
              {results.data.map((hit) => (
                <NodeCard key={hit.node_id} hit={hit} />
              ))}
            </div>
          )}
        </section>
      )}
    </div>
  );
}
