// The shared visual entity picker (M9.8 T2, ADR-064 §2) — the *only* way the user points at an
// entity to merge. A name-typeahead: type a name, pick from matches, and the component resolves it
// to an id internally (no UUID ever typed). One reusable control behind every merge surface — the
// profile "Merge into…", graph-health's one-click Merge, and the AdminOps card (T3 wires them).
//
// Controlled: `value` is the current pick (null = empty), `onChange` fires with the picked hub or
// null when cleared. `type` narrows to one entity-like hub kind; `excludeId` drops a hub from the
// results (e.g. don't offer to merge a node into itself). Backed by `GET /entities` (diacritic-
// folded name/alias match), debounced so it searches as you type without a request per keystroke.
import { AnimatePresence, motion } from 'framer-motion';
import { useEffect, useId, useRef, useState, type CSSProperties, type KeyboardEvent } from 'react';
import type { EntityBrowseItem } from '../api/types';
import { typeIcon, typeLabel } from './nodeTypes';
import { useEntitySearch } from './useEntitySearch';

// Debounce a fast-changing value (the typed query) so the network call trails the keystrokes.
function useDebounced<T>(value: T, delayMs: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const t = window.setTimeout(() => setDebounced(value), delayMs);
    return () => window.clearTimeout(t);
  }, [value, delayMs]);
  return debounced;
}

const fieldStyle: CSSProperties = {
  width: '100%',
  minWidth: 0,
  maxWidth: '100%',
  boxSizing: 'border-box',
  padding: '10px 12px',
  fontSize: 13,
  color: 'var(--text)',
  background: 'transparent',
  border: '1px solid var(--surface-border)',
  borderRadius: 'var(--radius)',
};

function SelectedChip({ item, onClear }: { item: EntityBrowseItem; onClear: () => void }) {
  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 8,
        padding: '9px 12px',
        border: '1px solid var(--accent)',
        borderRadius: 'var(--radius)',
        background: 'var(--surface)',
      }}
    >
      <span aria-hidden style={{ fontSize: 15 }}>
        {typeIcon(item.type)}
      </span>
      <span style={{ fontSize: 13, fontWeight: 600, color: 'var(--text)', overflowWrap: 'anywhere' }}>
        {item.title ?? item.id}
      </span>
      <span style={{ fontSize: 11, color: 'var(--muted)' }}>{typeLabel(item.type)}</span>
      <button
        type="button"
        aria-label="Clear selection"
        onClick={onClear}
        style={{
          marginLeft: 'auto',
          border: 'none',
          background: 'transparent',
          color: 'var(--muted)',
          cursor: 'pointer',
          fontSize: 16,
          lineHeight: 1,
          padding: 2,
        }}
      >
        ×
      </button>
    </div>
  );
}

export function EntityPicker({
  value,
  onChange,
  type,
  excludeId,
  placeholder = 'Search by name…',
  autoFocus = false,
}: {
  value: EntityBrowseItem | null;
  onChange: (item: EntityBrowseItem | null) => void;
  type?: string;
  excludeId?: string;
  placeholder?: string;
  autoFocus?: boolean;
}) {
  const [query, setQuery] = useState('');
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const debounced = useDebounced(query, 200);
  const listId = useId();
  const containerRef = useRef<HTMLDivElement>(null);

  const search = useEntitySearch(debounced, type, open && value == null);
  const results = (search.data ?? []).filter((r) => r.id !== excludeId);

  // Keep the highlighted row in range as the result set changes.
  useEffect(() => {
    setActive(0);
  }, [debounced, type]);

  // Close on an outside click so the dropdown doesn't linger over the rest of the form.
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', onDown);
    return () => document.removeEventListener('mousedown', onDown);
  }, [open]);

  const pick = (item: EntityBrowseItem) => {
    onChange(item);
    setQuery('');
    setOpen(false);
  };

  if (value) {
    return <SelectedChip item={value} onClear={() => onChange(null)} />;
  }

  const onKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setOpen(true);
      setActive((i) => Math.min(i + 1, results.length - 1));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setActive((i) => Math.max(i - 1, 0));
    } else if (e.key === 'Enter') {
      if (open && results[active]) {
        e.preventDefault();
        pick(results[active]);
      }
    } else if (e.key === 'Escape') {
      setOpen(false);
    }
  };

  const showDropdown = open && (results.length > 0 || debounced.trim() !== '');

  return (
    <div ref={containerRef} style={{ position: 'relative' }}>
      <input
        style={fieldStyle}
        role="combobox"
        aria-expanded={showDropdown}
        aria-controls={listId}
        aria-autocomplete="list"
        autoFocus={autoFocus}
        placeholder={placeholder}
        value={query}
        onChange={(e) => {
          setQuery(e.target.value);
          setOpen(true);
        }}
        onFocus={() => setOpen(true)}
        onKeyDown={onKeyDown}
      />
      <AnimatePresence>
        {showDropdown && (
          <motion.ul
            id={listId}
            role="listbox"
            initial={{ opacity: 0, y: -4 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -4 }}
            transition={{ duration: 0.12 }}
            style={{
              position: 'absolute',
              zIndex: 20,
              top: 'calc(100% + 4px)',
              left: 0,
              right: 0,
              margin: 0,
              padding: 4,
              listStyle: 'none',
              maxHeight: 260,
              overflowY: 'auto',
              background: 'var(--surface)',
              border: '1px solid var(--surface-border)',
              borderRadius: 'var(--radius)',
              boxShadow: '0 12px 30px -12px rgba(0,0,0,0.5)',
            }}
          >
            {results.length === 0 ? (
              <li
                style={{ padding: '10px 10px', fontSize: 13, color: 'var(--muted)' }}
                aria-disabled
              >
                {search.isFetching ? 'Searching…' : 'No matches.'}
              </li>
            ) : (
              results.map((item, i) => (
                <li
                  key={item.id}
                  role="option"
                  aria-selected={i === active}
                  onMouseEnter={() => setActive(i)}
                  onMouseDown={(e) => {
                    // mousedown (not click) so we select before the input's blur closes the list.
                    e.preventDefault();
                    pick(item);
                  }}
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: 8,
                    padding: '8px 10px',
                    borderRadius: 'calc(var(--radius) - 4px)',
                    cursor: 'pointer',
                    background: i === active ? 'var(--surface-border)' : 'transparent',
                  }}
                >
                  <span aria-hidden style={{ fontSize: 15 }}>
                    {typeIcon(item.type)}
                  </span>
                  <span style={{ minWidth: 0 }}>
                    <span
                      style={{
                        display: 'block',
                        fontSize: 13,
                        fontWeight: 600,
                        color: 'var(--text)',
                        overflowWrap: 'anywhere',
                      }}
                    >
                      {item.title ?? item.id}
                    </span>
                    {item.aliases.length > 0 && (
                      <span
                        style={{
                          display: 'block',
                          fontSize: 11,
                          color: 'var(--muted)',
                          overflowWrap: 'anywhere',
                        }}
                      >
                        {item.aliases.join(' · ')}
                      </span>
                    )}
                  </span>
                  <span style={{ marginLeft: 'auto', fontSize: 11, color: 'var(--muted)' }}>
                    {typeLabel(item.type)}
                  </span>
                </li>
              ))
            )}
          </motion.ul>
        )}
      </AnimatePresence>
    </div>
  );
}
