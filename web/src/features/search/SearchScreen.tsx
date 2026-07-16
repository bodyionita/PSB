import { AnimatePresence, motion } from 'framer-motion';
import { useState, type FormEvent } from 'react';
import { ApiError } from '../../api/client';
import type { SearchResultItem } from '../../api/types';
import { NodePreview, PlaneBadge } from '../../ui/NodePreview';
import { baseName } from '../../ui/nodeDetail';
import { Surface } from '../../ui/Surface';
import { typeIcon, typeLabel } from '../../ui/nodeTypes';
import { useMapNav } from '../map/mapNav';
import { usePlanes, useSearch, useTypes, type Submitted } from './useSearch';

// Search tab (06 §5): semantic search over the whole graph — no LLM. Query box + plane/type filter
// chips, node cards (type icon + plane badge + snippet), and a read-only node preview on expand
// (body + derived entity profile + canonical/derived edges — the shared ui/NodePreview primitive).

const FAIL_COLOR = '#ff6b6b';

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

function NodeCard({ hit }: { hit: SearchResultItem }) {
  const [open, setOpen] = useState(false);
  const mapNav = useMapNav();
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

        {mapNav && (
          <div style={{ marginTop: 12 }}>
            <button
              onClick={() => mapNav.openInMap(hit.node_id)}
              style={{
                display: 'inline-flex',
                alignItems: 'center',
                gap: 6,
                fontSize: 12,
                fontWeight: 600,
                padding: '5px 12px',
                borderRadius: 999,
                border: '1px solid var(--surface-border)',
                background: 'transparent',
                color: 'var(--accent)',
              }}
            >
              <span aria-hidden>✷</span> Explore in map
            </button>
          </div>
        )}

        <AnimatePresence initial={false}>
          {open && (
            <NodePreview nodeId={hit.node_id} onOpenNode={mapNav ? mapNav.openInMap : undefined} />
          )}
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
