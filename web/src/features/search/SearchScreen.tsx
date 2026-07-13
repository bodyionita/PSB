import { AnimatePresence, motion } from 'framer-motion';
import { useState, type FormEvent } from 'react';
import { ApiError } from '../../api/client';
import type { RelatedNoteItem, SearchResultItem } from '../../api/types';
import { Surface } from '../../ui/Surface';
import { useNote, usePlanes, useSearch, type Submitted } from './useSearch';

// Search tab (06 §5): semantic search over the whole vault — no LLM. Query box + plane-filter
// chips (scope on notes.planes membership), note cards, and a read-only preview on expand.

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

// The read-only preview shown when a card is expanded (GET /notes/{id}): body read live from the
// vault + the note's semantic neighbours from the relatedness graph (ADR-023).
function NotePreview({ noteId }: { noteId: string }) {
  const { data, isLoading, isError } = useNote(noteId);

  if (isLoading) {
    return <p style={{ margin: '12px 0 0', fontSize: 13, color: 'var(--muted)' }}>Loading note…</p>;
  }
  if (isError || !data) {
    return (
      <p style={{ margin: '12px 0 0', fontSize: 13, color: FAIL_COLOR }}>Couldn’t load this note.</p>
    );
  }

  return (
    <motion.div
      initial={{ opacity: 0, height: 0 }}
      animate={{ opacity: 1, height: 'auto' }}
      exit={{ opacity: 0, height: 0 }}
      transition={{ duration: 0.22, ease: 'easeOut' }}
      style={{ overflow: 'hidden' }}
    >
      <div style={{ marginTop: 14, paddingTop: 14, borderTop: '1px solid var(--surface-border)' }}>
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
          {data.body.trim() || '(empty note)'}
        </pre>

        {data.related.length > 0 && (
          <div style={{ marginTop: 14 }}>
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
              Related notes
            </p>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
              {data.related.map((r: RelatedNoteItem) => (
                <span
                  key={r.note_id}
                  title={`${r.vault_path} · ${r.score.toFixed(3)}`}
                  style={{
                    fontSize: 12,
                    color: 'var(--accent)',
                    background: 'var(--surface)',
                    border: '1px solid var(--surface-border)',
                    borderRadius: 999,
                    padding: '4px 10px',
                    maxWidth: '100%',
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                    whiteSpace: 'nowrap',
                  }}
                >
                  {r.title ?? baseName(r.vault_path)}
                </span>
              ))}
            </div>
          </div>
        )}
      </div>
    </motion.div>
  );
}

function NoteCard({ hit }: { hit: SearchResultItem }) {
  const [open, setOpen] = useState(false);
  const title = hit.title ?? baseName(hit.vault_path);

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
            <span style={{ fontSize: 16, fontWeight: 700, letterSpacing: -0.2, minWidth: 0 }}>
              {title}
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
          {open && <NotePreview noteId={hit.note_id} />}
        </AnimatePresence>
      </Surface>
    </motion.div>
  );
}

function PlaneChips({
  planes,
  selected,
  onToggle,
}: {
  planes: string[];
  selected: ReadonlySet<string>;
  onToggle: (plane: string) => void;
}) {
  return (
    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
      {planes.map((p) => {
        const on = selected.has(p);
        return (
          <motion.button
            key={p}
            onClick={() => onToggle(p)}
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
            {p}
          </motion.button>
        );
      })}
    </div>
  );
}

export function SearchScreen() {
  const [query, setQuery] = useState('');
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set());
  const [submitted, setSubmitted] = useState<Submitted | null>(null);

  const planesQuery = usePlanes();
  const results = useSearch(submitted);

  const allPlanes = planesQuery.data
    ? [...planesQuery.data.planes, planesQuery.data.inbox]
    : [];

  const run = (planeSet: ReadonlySet<string>) => {
    const q = query.trim();
    if (q === '') return;
    setSubmitted({ query: q, planes: [...planeSet].sort() });
  };

  const submit = (e: FormEvent) => {
    e.preventDefault();
    run(selected);
  };

  // Toggling a chip re-runs the current search immediately (once one has been run), so the filter
  // feels live; before the first search it just stages the selection.
  const togglePlane = (plane: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(plane)) next.delete(plane);
      else next.add(plane);
      if (submitted) setSubmitted({ query: submitted.query, planes: [...next].sort() });
      return next;
    });
  };

  const embedDown = results.error instanceof ApiError && results.error.status === 503;

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
        <PlaneChips planes={allPlanes} selected={selected} onToggle={togglePlane} />
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
              No matches. Try different words{selected.size > 0 ? ' or clear the plane filter' : ''}.
            </p>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
              {results.data.map((hit) => (
                <NoteCard key={hit.note_id} hit={hit} />
              ))}
            </div>
          )}
        </section>
      )}
    </div>
  );
}
