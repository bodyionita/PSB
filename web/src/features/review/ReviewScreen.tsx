import { AnimatePresence, motion, useReducedMotion } from 'framer-motion';
import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import type {
  EntityCandidate,
  ReviewItemResponse,
  Salience,
  StanceVerdict,
} from '../../api/types';
import { Button } from '../../ui/Button';
import { NodeChip } from '../../ui/NodeChip';
import { Surface } from '../../ui/Surface';
import { TimeAgo } from '../../ui/TimeAgo';
import { baseName, useNode } from '../../ui/nodeDetail';
import { typeIcon } from '../../ui/nodeTypes';
import { useBatchResolve, useResolveReview, useReview, useReviewMaybe } from './useReview';

// Review tab (06 §3b): the queue of decisions the pipeline couldn't make on its own — an
// `entity-ambiguity` (M3), a `vocab-proposal` (M3), a chat-distilled `stance-candidate` (M6,
// ADR-048), or a `dedup-proposal` (M6, ADR-049). Each is decidable in place. M6 adds salience
// ordering, multi-select batch actions, and a parked-`maybe` section with an aging indicator.

const FAIL_COLOR = '#ff6b6b';
const KIND_ENTITY = 'entity-ambiguity';
const KIND_VOCAB = 'vocab-proposal';
const KIND_STANCE = 'stance-candidate';
const KIND_DEDUP = 'dedup-proposal';

// Only the M6 kinds (stance-candidate / dedup-proposal) show a batch select box; entity/vocab keep
// their single-item flows (ADR-048 §8 frames batch around stance triage + dedup dismissal).

// --- payload readers (payload is kind-specific JSON; read it defensively) -------------------

interface MentionInfo {
  name: string;
  type: string | null;
}

function asRecord(v: unknown): Record<string, unknown> {
  return v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : {};
}

function str(v: unknown): string | null {
  return typeof v === 'string' && v.trim() ? v : null;
}

function strList(v: unknown): string[] {
  return Array.isArray(v) ? v.filter((s): s is string => typeof s === 'string') : [];
}

function mentionOf(item: ReviewItemResponse): MentionInfo {
  const m = asRecord(item.payload.mention);
  return {
    name: typeof m.name === 'string' ? m.name : '(unknown)',
    type: typeof m.type === 'string' ? m.type : null,
  };
}

function candidatesOf(item: ReviewItemResponse): EntityCandidate[] {
  const raw = item.payload.candidates;
  if (!Array.isArray(raw)) return [];
  return raw.map((c) => {
    const r = asRecord(c);
    return {
      id: typeof r.id === 'string' ? r.id : '',
      name: typeof r.name === 'string' ? r.name : null,
      disambig: typeof r.disambig === 'string' ? r.disambig : null,
      aliases: strList(r.aliases),
    };
  });
}

function vocabOf(item: ReviewItemResponse): { vocab: string; value: string } {
  return {
    vocab: typeof item.payload.vocab === 'string' ? item.payload.vocab : 'type',
    value: typeof item.payload.value === 'string' ? item.payload.value : '(unknown)',
  };
}

interface StanceInfo {
  text: string;
  entities: string[];
  salience: Salience | null;
  whyUnclear: string | null;
}

function salienceOf(v: unknown): Salience | null {
  return v === 'high' || v === 'med' || v === 'low' ? v : null;
}

function stanceOf(item: ReviewItemResponse): StanceInfo {
  return {
    text: str(item.payload.candidate_text) ?? '(unknown)',
    entities: strList(item.payload.referenced_entity_names),
    salience: salienceOf(item.payload.salience),
    whyUnclear: str(item.payload.why_unclear),
  };
}

interface DedupInfo {
  nodeA: string;
  nodeB: string;
  defaultSurvivor: string;
  cosine: number | null;
  sharedEntities: string[];
  occurredOverlap: boolean;
}

function dedupOf(item: ReviewItemResponse): DedupInfo {
  const signals = asRecord(item.payload.signals);
  return {
    nodeA: str(item.payload.node_a) ?? '',
    nodeB: str(item.payload.node_b) ?? '',
    defaultSurvivor: str(item.payload.default_survivor) ?? '',
    cosine: typeof signals.cosine === 'number' ? signals.cosine : null,
    sharedEntities: strList(signals.shared_entity_titles),
    occurredOverlap: signals.occurred_overlap === true,
  };
}

// Salience → sort rank (high first). Items without a salience (dedup-proposal, entity/vocab) sort as
// medium so they interleave sensibly rather than sinking below every low candidate.
const SALIENCE_RANK: Record<Salience, number> = { high: 0, med: 1, low: 2 };
function salienceRank(item: ReviewItemResponse): number {
  const s = salienceOf(item.payload.salience);
  return s ? SALIENCE_RANK[s] : 1;
}

// --- shared bits ----------------------------------------------------------------------------

function KindBadge({ label }: { label: string }) {
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
      {label}
    </span>
  );
}

// Coarse triage weight (ADR-048 §8) — high reads as accent-strong, low as faint.
function SalienceBadge({ salience }: { salience: Salience }) {
  const strong = salience === 'high';
  return (
    <span
      title={`salience: ${salience}`}
      style={{
        fontSize: 10,
        fontWeight: 700,
        letterSpacing: 0.5,
        textTransform: 'uppercase',
        color: strong ? 'var(--on-accent)' : 'var(--muted)',
        background: strong ? 'var(--accent)' : 'transparent',
        border: strong ? '1px solid var(--accent)' : '1px solid var(--surface-border)',
        borderRadius: 999,
        padding: '2px 7px',
        opacity: salience === 'low' ? 0.7 : 1,
        whiteSpace: 'nowrap',
      }}
    >
      {salience}
    </span>
  );
}

// "parked Nd ago" — the aging indicator on a re-openable maybe (ADR-048 §8), so a stale pile reads
// as stale at a glance.
function AgingTag({ item }: { item: ReviewItemResponse }) {
  if (!item.created_at) return null;
  return (
    <span style={{ fontSize: 11, color: 'var(--muted)', whiteSpace: 'nowrap' }}>
      parked <TimeAgo iso={item.created_at} />
    </span>
  );
}

// Wraps a review card so a graph-health aging-review deep-link can scroll to + transiently ring it
// (ADR-054 §5 replan). `data-review-id` is the scroll target; the ring is reduced-motion-safe.
function HighlightWrap({
  id,
  highlighted,
  children,
}: {
  id: string;
  highlighted: boolean;
  children: ReactNode;
}) {
  const reduce = useReducedMotion();
  return (
    <div
      data-review-id={id}
      style={{
        borderRadius: 'var(--radius)',
        transition: reduce ? 'none' : 'box-shadow 0.4s ease',
        boxShadow: highlighted ? '0 0 0 2px var(--accent)' : '0 0 0 0 transparent',
      }}
    >
      {children}
    </div>
  );
}

function Excerpt({ text }: { text: string | null }) {
  if (!text) return null;
  return (
    <p
      style={{
        margin: '10px 0 0',
        fontSize: 13,
        lineHeight: 1.5,
        color: 'var(--muted)',
        fontStyle: 'italic',
        borderLeft: '2px solid var(--surface-border)',
        paddingLeft: 10,
      }}
    >
      “{text.trim()}”
    </p>
  );
}

// The select checkbox shown on batch-eligible cards (stance-candidate / dedup-proposal).
function SelectBox({ checked, onChange }: { checked: boolean; onChange: () => void }) {
  return (
    <input
      type="checkbox"
      checked={checked}
      onChange={onChange}
      aria-label="Select for batch action"
      style={{ width: 18, height: 18, accentColor: 'var(--accent)', cursor: 'pointer', flexShrink: 0 }}
    />
  );
}

function ResolveError({ show }: { show: boolean }) {
  if (!show) return null;
  return (
    <p style={{ margin: '12px 0 0', fontSize: 13, color: FAIL_COLOR }}>
      Couldn’t resolve that — try again.
    </p>
  );
}

// Props shared by every card so the list can wire selection uniformly.
interface CardProps {
  item: ReviewItemResponse;
  selected: boolean;
  onToggleSelect: () => void;
}

// --- entity-ambiguity -----------------------------------------------------------------------

function EntityAmbiguityCard({ item }: { item: ReviewItemResponse }) {
  const resolve = useResolveReview();
  const mention = mentionOf(item);
  const candidates = candidatesOf(item);
  const busy = resolve.isPending;
  const parked = item.status === 'maybe';

  const pick = (choice: string) => resolve.mutate({ id: item.id, body: { choice } });

  return (
    <Surface>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 10 }}>
        <span style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 16, fontWeight: 700, minWidth: 0 }}>
          <span aria-hidden style={{ flexShrink: 0 }}>{typeIcon(mention.type)}</span>
          <span style={{ minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis' }}>
            {mention.name}
          </span>
        </span>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexShrink: 0 }}>
          {parked && <AgingTag item={item} />}
          <KindBadge label="Which one?" />
        </div>
      </div>
      <p style={{ margin: '6px 0 0', fontSize: 13, color: 'var(--muted)' }}>
        A {mention.type ?? 'node'} mentioned here matches more than one existing entity — pick the
        right one, or create a new one.
      </p>
      <Excerpt text={item.excerpt} />

      {/* Each candidate is a node reference (an existing entity): a NodeChip peeks at it in the
          shared NodePreview drawer (ADR-054 §5), while an explicit "Use this" makes the pick — the
          preview affordance never commits the decision. */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8, marginTop: 14 }}>
        {candidates.map((c) => (
          <div
            key={c.id}
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 10,
              padding: 12,
              borderRadius: 'var(--radius)',
              border: '1px solid var(--surface-border)',
              opacity: busy ? 0.6 : 1,
            }}
          >
            <div style={{ display: 'flex', flexDirection: 'column', gap: 4, minWidth: 0, flex: 1 }}>
              <NodeChip nodeId={c.id} type={mention.type} title={c.name ?? c.id} />
              {c.disambig && (
                <span style={{ fontSize: 12, color: 'var(--muted)' }}>{c.disambig}</span>
              )}
              {c.aliases.length > 0 && (
                <span style={{ fontSize: 12, color: 'var(--muted)' }}>
                  also known as {c.aliases.join(', ')}
                </span>
              )}
            </div>
            <Button onClick={() => pick(c.id)} disabled={busy} style={{ flexShrink: 0 }}>
              Use this
            </Button>
          </div>
        ))}
      </div>

      <div style={{ display: 'flex', gap: 10, marginTop: 14, flexWrap: 'wrap' }}>
        <Button onClick={() => pick('new')} disabled={busy}>
          New entity
        </Button>
        {!parked && (
          <Button variant="ghost" onClick={() => pick('maybe')} disabled={busy}>
            Not sure — later
          </Button>
        )}
      </div>
      <ResolveError show={resolve.isError} />
    </Surface>
  );
}

// --- vocab-proposal -------------------------------------------------------------------------

function VocabProposalCard({ item }: { item: ReviewItemResponse }) {
  const resolve = useResolveReview();
  const { vocab, value } = vocabOf(item);
  const busy = resolve.isPending;
  const axis = vocab.replace(/_/g, ' ');

  return (
    <Surface>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 10 }}>
        <span style={{ fontSize: 16, fontWeight: 700, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis' }}>
          {value}
        </span>
        <KindBadge label="New type" />
      </div>
      <p style={{ margin: '6px 0 0', fontSize: 13, color: 'var(--muted)' }}>
        The organizer proposed <strong>{value}</strong> as a new {axis}. Approve to add it to your
        vocabulary (it goes live everywhere and triggers a consolidation pass), or reject to discard.
      </p>
      <Excerpt text={item.excerpt} />

      <div style={{ display: 'flex', gap: 10, marginTop: 14 }}>
        <Button onClick={() => resolve.mutate({ id: item.id, body: { verdict: 'approve' } })} disabled={busy}>
          Approve
        </Button>
        <Button variant="ghost" onClick={() => resolve.mutate({ id: item.id, body: { verdict: 'reject' } })} disabled={busy}>
          Reject
        </Button>
      </div>
      <ResolveError show={resolve.isError} />
    </Surface>
  );
}

// --- stance-candidate (M6, ADR-048 §7) ------------------------------------------------------

function StanceCandidateCard({ item, selected, onToggleSelect }: CardProps) {
  const resolve = useResolveReview();
  const { text, entities, salience, whyUnclear } = stanceOf(item);
  const busy = resolve.isPending;
  const parked = item.status === 'maybe';

  const decide = (verdict: StanceVerdict) => resolve.mutate({ id: item.id, body: { verdict } });

  return (
    <Surface>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 10 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, minWidth: 0 }}>
          <SelectBox checked={selected} onChange={onToggleSelect} />
          <span aria-hidden style={{ flexShrink: 0 }}>{typeIcon('insight')}</span>
          <span style={{ fontSize: 13, color: 'var(--muted)' }}>Should I remember this?</span>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexShrink: 0 }}>
          {parked && <AgingTag item={item} />}
          {salience && <SalienceBadge salience={salience} />}
        </div>
      </div>

      <p style={{ margin: '12px 0 0', fontSize: 15.5, lineHeight: 1.5, color: 'var(--text)', fontWeight: 600 }}>
        {text}
      </p>

      {entities.length > 0 && (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginTop: 10 }}>
          {entities.map((e) => (
            <span
              key={e}
              style={{
                fontSize: 12,
                color: 'var(--text)',
                background: 'var(--surface)',
                border: '1px solid var(--surface-border)',
                borderRadius: 999,
                padding: '3px 9px',
              }}
            >
              {e}
            </span>
          ))}
        </div>
      )}

      {whyUnclear && (
        <p style={{ margin: '10px 0 0', fontSize: 12.5, color: 'var(--muted)' }}>
          Unclear because: {whyUnclear}
        </p>
      )}

      <Excerpt text={item.excerpt} />

      <div style={{ display: 'flex', gap: 10, marginTop: 14, flexWrap: 'wrap' }}>
        <Button onClick={() => decide('agree')} disabled={busy}>
          Remember it
        </Button>
        <Button variant="ghost" onClick={() => decide('disagree')} disabled={busy}>
          Discard
        </Button>
        {!parked && (
          <Button variant="ghost" onClick={() => decide('maybe')} disabled={busy}>
            Maybe later
          </Button>
        )}
      </div>
      <ResolveError show={resolve.isError} />
    </Surface>
  );
}

// --- dedup-proposal (M6, ADR-049 §6) --------------------------------------------------------

// One side of a possible-duplicate pair, fetched lazily for its title/type/snippet, with a radio to
// pick it as the merge survivor (the other is folded into it).
function DedupNodeRow({
  nodeId,
  isSurvivor,
  onPick,
  busy,
}: {
  nodeId: string;
  isSurvivor: boolean;
  onPick: () => void;
  busy: boolean;
}) {
  const { data, isLoading } = useNode(nodeId);
  const title = data?.title ?? baseName(data?.store_path ?? nodeId);
  const body = (data?.body ?? '').trim().replace(/\s+/g, ' ');
  const snippet = body.length > 160 ? `${body.slice(0, 160)}…` : body;

  return (
    <label
      style={{
        display: 'flex',
        gap: 10,
        alignItems: 'flex-start',
        padding: 12,
        borderRadius: 'var(--radius)',
        border: isSurvivor ? '1px solid var(--accent)' : '1px solid var(--surface-border)',
        background: 'var(--surface)',
        cursor: busy ? 'default' : 'pointer',
        opacity: busy ? 0.6 : 1,
      }}
    >
      <input
        type="radio"
        name={`survivor-${nodeId.slice(0, 8)}`}
        checked={isSurvivor}
        onChange={onPick}
        disabled={busy}
        aria-label={`Keep "${title}" as the surviving node`}
        style={{ marginTop: 3, accentColor: 'var(--accent)', flexShrink: 0 }}
      />
      <span style={{ minWidth: 0 }}>
        {/* The node's title is a NodeChip (ADR-054 §5) — tap it to peek at the node in the shared
            NodePreview drawer; the radio/snippet still pick this side as the merge survivor. */}
        <NodeChip nodeId={nodeId} type={data?.type ?? null} title={title} />
        {isLoading ? (
          <span style={{ display: 'block', marginTop: 4, fontSize: 12.5, color: 'var(--muted)' }}>
            Loading node…
          </span>
        ) : (
          snippet && (
            <span style={{ display: 'block', marginTop: 4, fontSize: 12.5, lineHeight: 1.5, color: 'var(--muted)' }}>
              {snippet}
            </span>
          )
        )}
      </span>
    </label>
  );
}

function DedupProposalCard({ item, selected, onToggleSelect }: CardProps) {
  const resolve = useResolveReview();
  const info = dedupOf(item);
  const busy = resolve.isPending;
  const parked = item.status === 'maybe';
  const [survivor, setSurvivor] = useState(info.defaultSurvivor || info.nodeA);

  const act = (action: 'merge' | 'keep' | 'link') =>
    resolve.mutate({
      id: item.id,
      body: action === 'merge' ? { action, survivor } : { action },
    });

  return (
    <Surface>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 10 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, minWidth: 0 }}>
          <SelectBox checked={selected} onChange={onToggleSelect} />
          <span style={{ fontSize: 13, color: 'var(--muted)' }}>Possible duplicate</span>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexShrink: 0 }}>
          {parked && <AgingTag item={item} />}
          <KindBadge label="Duplicate?" />
        </div>
      </div>

      <p style={{ margin: '6px 0 12px', fontSize: 12.5, color: 'var(--muted)' }}>
        These two look like the same thing
        {info.cosine != null && <> · {(info.cosine * 100).toFixed(0)}% similar</>}
        {info.sharedEntities.length > 0 && <> · shares {info.sharedEntities.join(', ')}</>}
        {info.occurredOverlap && <> · overlapping dates</>}. Merge keeps the selected one and folds
        the other into it; keep them separate, or link them as related.
      </p>

      <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
        <DedupNodeRow
          nodeId={info.nodeA}
          isSurvivor={survivor === info.nodeA}
          onPick={() => setSurvivor(info.nodeA)}
          busy={busy}
        />
        <DedupNodeRow
          nodeId={info.nodeB}
          isSurvivor={survivor === info.nodeB}
          onPick={() => setSurvivor(info.nodeB)}
          busy={busy}
        />
      </div>

      <div style={{ display: 'flex', gap: 10, marginTop: 14, flexWrap: 'wrap' }}>
        <Button onClick={() => act('merge')} disabled={busy}>
          Merge
        </Button>
        <Button variant="ghost" onClick={() => act('keep')} disabled={busy}>
          Keep both
        </Button>
        <Button variant="ghost" onClick={() => act('link')} disabled={busy}>
          Link as related
        </Button>
      </div>
      <ResolveError show={resolve.isError} />
    </Surface>
  );
}

// --- card dispatch --------------------------------------------------------------------------

function ReviewCard({ item, selected, onToggleSelect }: CardProps) {
  return (
    <motion.div
      layout
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: -6 }}
      transition={{ type: 'spring', stiffness: 420, damping: 34 }}
    >
      {item.kind === KIND_ENTITY ? (
        <EntityAmbiguityCard item={item} />
      ) : item.kind === KIND_VOCAB ? (
        <VocabProposalCard item={item} />
      ) : item.kind === KIND_STANCE ? (
        <StanceCandidateCard item={item} selected={selected} onToggleSelect={onToggleSelect} />
      ) : item.kind === KIND_DEDUP ? (
        <DedupProposalCard item={item} selected={selected} onToggleSelect={onToggleSelect} />
      ) : (
        // Unknown/future kind: show it, don't hide it.
        <Surface>
          <KindBadge label={item.kind} />
          <Excerpt text={item.excerpt} />
        </Surface>
      )}
    </motion.div>
  );
}

// --- batch action bar -----------------------------------------------------------------------

// The actions offered depend on the selected kind: stance triage (agree/disagree/maybe) or a dedup
// dismissal (keep). Selection is single-kind (the screen resets it when a different kind is picked),
// so the bar is unambiguous. A batch merge is deliberately not offered — a survivor is per-item.
const BATCH_ACTIONS: Record<string, { action: string; label: string; primary?: boolean }[]> = {
  [KIND_STANCE]: [
    { action: 'agree', label: 'Remember all', primary: true },
    { action: 'disagree', label: 'Discard all' },
    { action: 'maybe', label: 'Maybe later' },
  ],
  [KIND_DEDUP]: [{ action: 'keep', label: 'Keep both (dismiss)' }],
};

function BatchBar({
  kind,
  count,
  onAction,
  onClear,
  busy,
}: {
  kind: string;
  count: number;
  onAction: (action: string) => void;
  onClear: () => void;
  busy: boolean;
}) {
  const actions = BATCH_ACTIONS[kind] ?? [];
  return (
    <motion.div
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: 12 }}
      transition={{ type: 'spring', stiffness: 460, damping: 34 }}
      style={{ position: 'sticky', bottom: 88, zIndex: 3 }}
    >
      <Surface padding={12} style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
        <span style={{ fontSize: 13, fontWeight: 600 }}>{count} selected</span>
        <div style={{ flex: 1 }} />
        {actions.map((a) => (
          <Button
            key={a.action}
            variant={a.primary ? 'primary' : 'ghost'}
            onClick={() => onAction(a.action)}
            disabled={busy}
            style={{ padding: '8px 14px', fontSize: 13 }}
          >
            {a.label}
          </Button>
        ))}
        <Button variant="ghost" onClick={onClear} disabled={busy} style={{ padding: '8px 14px', fontSize: 13 }}>
          Clear
        </Button>
      </Surface>
    </motion.div>
  );
}

// --- screen ---------------------------------------------------------------------------------

function CountBadge({ n }: { n: number }) {
  if (n <= 0) return null;
  return (
    <span
      style={{
        fontSize: 13,
        fontWeight: 700,
        color: 'var(--on-accent)',
        background: 'var(--accent)',
        borderRadius: 999,
        padding: '2px 10px',
        minWidth: 24,
        textAlign: 'center',
      }}
    >
      {n}
    </span>
  );
}

export function ReviewScreen({ seed = null }: { seed?: string | null } = {}) {
  const { data, isLoading, isError } = useReview();
  const maybeQuery = useReviewMaybe();
  const batch = useBatchResolve();
  const reduce = useReducedMotion();

  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set());
  const [selectedKind, setSelectedKind] = useState<string | null>(null);
  const [batchNote, setBatchNote] = useState<string | null>(null);
  const [showParked, setShowParked] = useState(false);
  // Deep-link highlight target (a graph-health aging-review offender jumped here, ADR-054 §5 replan).
  const [highlightId, setHighlightId] = useState<string | null>(null);
  const handledSeedRef = useRef<string | null>(null);

  // Salience-ordered active queue (high first), stable within a rank (server order = newest-first).
  const items = useMemo(() => {
    const list = data ?? [];
    return [...list]
      .map((item, i) => ({ item, i }))
      .sort((a, b) => salienceRank(a.item) - salienceRank(b.item) || a.i - b.i)
      .map(({ item }) => item);
  }, [data]);

  const parked = useMemo(() => maybeQuery.data ?? [], [maybeQuery.data]);

  // React to a deep-link seed once both lists have loaded: expand Parked if the item lives there,
  // scroll it into view, and ring it transiently (ADR-054 §5 replan). A stale/resolved id (gone
  // since the nightly graph-health snapshot) → silent land, no highlight. handledSeedRef makes it
  // fire once per mount (React Query refetches change items/parked). The seed is NOT consumed here —
  // consuming during the effect breaks under React 18 StrictMode's double-mount (the throwaway mount
  // would clear it before the real mount reads it); AppShell instead clears reviewSeed on manual
  // bottom-nav navigation, and reaching the offender chip always remounts this screen anyway.
  useEffect(() => {
    if (!seed) {
      handledSeedRef.current = null;
      return;
    }
    if (handledSeedRef.current === seed) return;
    if (isLoading || maybeQuery.isLoading) return; // wait for data; effect re-runs when it lands
    handledSeedRef.current = seed;

    const inParked = parked.some((i) => i.id === seed);
    const found = inParked || items.some((i) => i.id === seed);
    if (!found) return; // stale — silent land on the tab
    if (inParked) setShowParked(true);
    setHighlightId(seed);
    window.setTimeout(() => {
      document
        .querySelector(`[data-review-id="${seed}"]`)
        ?.scrollIntoView({ behavior: reduce ? 'auto' : 'smooth', block: 'center' });
    }, 260); // let a just-expanded Parked section lay out first
    window.setTimeout(() => setHighlightId((cur) => (cur === seed ? null : cur)), 2800);
  }, [seed, isLoading, maybeQuery.isLoading, items, parked, reduce]);

  const toggleSelect = (item: ReviewItemResponse) => {
    setBatchNote(null);
    // Selection is single-kind: picking a different kind starts a fresh selection (the batch bar's
    // actions are kind-specific, so a mixed selection would be ambiguous). Both setters run in the
    // same event, so React batches them into one render.
    if (item.kind !== selectedKind) {
      setSelected(new Set([item.id]));
      setSelectedKind(item.kind);
      return;
    }
    const next = new Set(selected);
    if (next.has(item.id)) next.delete(item.id);
    else next.add(item.id);
    setSelected(next);
    setSelectedKind(next.size === 0 ? null : item.kind);
  };

  const clearSelection = () => {
    setSelected(new Set());
    setSelectedKind(null);
  };

  const runBatch = async (action: string) => {
    const ids = [...selected];
    if (ids.length === 0) return;
    setBatchNote(null);
    try {
      const res = await batch.mutateAsync({ ids, action });
      const ok = res.results.filter((r) => r.ok).length;
      const failed = res.results.length - ok;
      setBatchNote(failed === 0 ? `${ok} resolved.` : `${ok} resolved, ${failed} couldn’t be applied.`);
    } catch {
      setBatchNote('Batch action failed — try again.');
    } finally {
      clearSelection();
    }
  };

  return (
    <div style={{ display: 'grid', gap: 16 }}>
      <header style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
        <h1 style={{ margin: 0, fontSize: 24, fontWeight: 700, letterSpacing: -0.4 }}>Review</h1>
        <CountBadge n={items.length} />
      </header>
      <p style={{ margin: '-6px 0 0', fontSize: 14, color: 'var(--muted)', lineHeight: 1.5 }}>
        Decisions your brain couldn’t make on its own — ambiguous entities, proposed types, memories
        it wasn’t sure to keep, and possible duplicates.
      </p>

      {batchNote && (
        <p style={{ margin: 0, fontSize: 13, color: 'var(--muted)' }}>{batchNote}</p>
      )}

      {isLoading ? (
        <p style={{ margin: 0, fontSize: 14, color: 'var(--muted)' }}>Loading…</p>
      ) : isError ? (
        <p style={{ margin: 0, fontSize: 14, color: FAIL_COLOR }}>Couldn’t load the review queue.</p>
      ) : items.length === 0 ? (
        <Surface>
          <p style={{ margin: 0, fontSize: 14, color: 'var(--muted)', lineHeight: 1.6 }}>
            Nothing to review. When the organizer isn’t sure about a mention, a new type, a memory
            from a conversation, or a possible duplicate, it’ll wait for you here.
          </p>
        </Surface>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          <AnimatePresence initial={false}>
            {items.map((item) => (
              <HighlightWrap key={item.id} id={item.id} highlighted={highlightId === item.id}>
                <ReviewCard
                  item={item}
                  selected={selected.has(item.id)}
                  onToggleSelect={() => toggleSelect(item)}
                />
              </HighlightWrap>
            ))}
          </AnimatePresence>
        </div>
      )}

      {/* Parked (maybe) section — re-openable items with an aging indicator (ADR-048 §8). */}
      {parked.length > 0 && (
        <div style={{ display: 'grid', gap: 12 }}>
          <button
            onClick={() => setShowParked((v) => !v)}
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 8,
              padding: '4px 2px',
              background: 'transparent',
              border: 'none',
              color: 'var(--muted)',
              cursor: 'pointer',
              fontSize: 14,
              fontWeight: 600,
            }}
          >
            <span aria-hidden>{showParked ? '▾' : '▸'}</span>
            Parked · {parked.length}
          </button>
          <AnimatePresence initial={false}>
            {showParked && (
              <motion.div
                initial={{ opacity: 0, height: 0 }}
                animate={{ opacity: 1, height: 'auto' }}
                exit={{ opacity: 0, height: 0 }}
                transition={{ duration: 0.2, ease: 'easeOut' }}
                style={{ overflow: 'hidden' }}
              >
                <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
                  {parked.map((item) => (
                    <HighlightWrap key={item.id} id={item.id} highlighted={highlightId === item.id}>
                      <ReviewCard
                        item={item}
                        selected={selected.has(item.id)}
                        onToggleSelect={() => toggleSelect(item)}
                      />
                    </HighlightWrap>
                  ))}
                </div>
              </motion.div>
            )}
          </AnimatePresence>
        </div>
      )}

      <AnimatePresence>
        {selected.size > 0 && selectedKind && (
          <BatchBar
            kind={selectedKind}
            count={selected.size}
            onAction={(a) => void runBatch(a)}
            onClear={clearSelection}
            busy={batch.isPending}
          />
        )}
      </AnimatePresence>
    </div>
  );
}
