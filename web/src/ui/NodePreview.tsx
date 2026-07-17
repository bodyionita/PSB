// Shared read-only node preview (GET /nodes/{id}) — the expand target for both a Search result card
// (06 §5) and a Chat source card (06 §2). Body read live from the graph store, the derived entity
// profile (entity nodes only, ADR-030), and the node's edges — canonical (typed) + derived
// (similarity), both directions. A design-system primitive so neither feature clones it (rule 10).
import { motion } from 'framer-motion';
import { useMemo } from 'react';
import type { NodeEdgeItem } from '../api/types';
import { TokenizedBody } from './TokenizedBody';
import {
  parsePartial,
  renderAbsolute,
  renderRelative,
  todayCivil,
  type ResolvedTime,
} from './dateToken';
import { HoverTip } from './HoverTip';
import { InteriorityBadge } from './InteriorityBadge';
import { baseName, useNode } from './nodeDetail';
import { edgeLabel, typeIcon } from './nodeTypes';

const FAIL_COLOR = '#ff6b6b';

// The node's canonical event date (`occurred`[/`occurred_end`]) as a live phrase + exact-date
// tooltip — read-only here (the editable copy is the body token, ADR-056 §5). Day-granular
// partial-ISO strings; a range renders absolute. Renders nothing when the node has no `occurred`.
function OccurredLine({ occurred, occurredEnd }: { occurred: string | null; occurredEnd: string | null }) {
  const now = useMemo(() => todayCivil(), []);
  const rt = useMemo<ResolvedTime | null>(() => {
    if (!occurred) return null;
    const start = parsePartial(occurred);
    if (!start) return null;
    const end = occurredEnd ? parsePartial(occurredEnd) : null;
    return { start, end, label: null };
  }, [occurred, occurredEnd]);
  if (!rt) return null;
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 5, fontSize: 12, color: 'var(--muted)' }}>
      <span aria-hidden>◷</span>
      <HoverTip
        tip={renderAbsolute(rt)}
        ariaLabel={`occurred ${renderRelative(rt, now)}, ${renderAbsolute(rt)}`}
        style={{ color: 'var(--text)' }}
      >
        {renderRelative(rt, now)}
      </HoverTip>
    </span>
  );
}

export function PlaneBadge({ plane }: { plane: string | null }) {
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

// One edge rendered as a jump-off chip. When `onOpen` is provided (the Map, and any surface that
// wants the edges navigable — ADR-051 §8), the chip is a button that opens/re-centers on the other
// endpoint; otherwise it's a static label. Canonical edges are solid + labelled by their `rel`;
// derived similarity edges are faint.
function EdgeChip({
  edge,
  onOpen,
}: {
  edge: NodeEdgeItem;
  onOpen?: (nodeId: string) => void;
}) {
  const derived = edge.origin === 'derived';
  const arrow = edge.dir === 'in' ? '←' : '→';
  const style = {
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
    textAlign: 'left' as const,
  };
  const title = `${edge.dir === 'in' ? 'from' : 'to'} ${edge.title ?? edge.node_id}${
    edge.since ? ` · since ${edge.since}` : ''
  }`;
  const inner = (
    <>
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
    </>
  );

  if (onOpen) {
    return (
      <button
        type="button"
        title={title}
        onClick={() => onOpen(edge.node_id)}
        style={{ ...style, color: derived ? 'var(--muted)' : 'var(--text)', cursor: 'pointer' }}
      >
        {inner}
      </button>
    );
  }
  return (
    <span title={title} style={style}>
      {inner}
    </span>
  );
}

// The animated read-only preview shown when a card is expanded. Fetches its own node detail lazily.
// `onOpenNode` (optional) makes the edge chips navigable — Search/Chat pass it to jump into the Map,
// the Map passes it to re-center (ADR-051 §8). Omitted ⇒ static edge labels (unchanged behaviour).
export function NodePreview({
  nodeId,
  onOpenNode,
}: {
  nodeId: string;
  onOpenNode?: (nodeId: string) => void;
}) {
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
        {/* Meta row — the inner-voice marker (ADR-055 §3c) + the event date (ADR-056), when present. */}
        {(data.interiority === 'internal' ||
          data.interiority === 'mixed' ||
          data.occurred) && (
          <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 10, marginBottom: 12 }}>
            <InteriorityBadge interiority={data.interiority} />
            <OccurredLine occurred={data.occurred} occurredEnd={data.occurred_end} />
          </div>
        )}

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
          {data.body.trim() ? (
            <TokenizedBody body={data.body.trim()} nodeId={nodeId} />
          ) : (
            '(no body)'
          )}
        </pre>

        {canonical.length > 0 && (
          <div style={{ marginTop: 14 }}>
            <SectionLabel>Connections</SectionLabel>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
              {canonical.map((e) => (
                <EdgeChip key={`${e.dir}:${e.rel}:${e.node_id}`} edge={e} onOpen={onOpenNode} />
              ))}
            </div>
          </div>
        )}

        {derived.length > 0 && (
          <div style={{ marginTop: 14 }}>
            <SectionLabel>Similar</SectionLabel>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
              {derived.map((e) => (
                <EdgeChip key={`${e.dir}:sim:${e.node_id}`} edge={e} onOpen={onOpenNode} />
              ))}
            </div>
          </div>
        )}
      </div>
    </motion.div>
  );
}
