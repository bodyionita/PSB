import { AnimatePresence, motion } from 'framer-motion';
import type { EntityCandidate, ReviewItemResponse } from '../../api/types';
import { Button } from '../../ui/Button';
import { Surface } from '../../ui/Surface';
import { typeIcon } from '../../ui/nodeTypes';
import { useResolveReview, useReview } from './useReview';

// Review tab (06 §3b): the M3 minimal, kind-generic queue of items the pipeline couldn't decide on
// its own — an `entity-ambiguity` (which existing entity did this mention refer to?) or a
// `vocab-proposal` (should this new type join the vocabulary?). Each is decidable in place; the
// polished, source-grouped UX lands at M6.

const FAIL_COLOR = '#ff6b6b';
const KIND_ENTITY = 'entity-ambiguity';
const KIND_VOCAB = 'vocab-proposal';

// --- payload readers (payload is kind-specific JSON; read it defensively) -------------------

interface MentionInfo {
  name: string;
  type: string | null;
}

function asRecord(v: unknown): Record<string, unknown> {
  return v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : {};
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
      aliases: Array.isArray(r.aliases) ? r.aliases.filter((a): a is string => typeof a === 'string') : [],
    };
  });
}

function vocabOf(item: ReviewItemResponse): { vocab: string; value: string } {
  return {
    vocab: typeof item.payload.vocab === 'string' ? item.payload.vocab : 'type',
    value: typeof item.payload.value === 'string' ? item.payload.value : '(unknown)',
  };
}

// --- cards ----------------------------------------------------------------------------------

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

function EntityAmbiguityCard({ item }: { item: ReviewItemResponse }) {
  const resolve = useResolveReview();
  const mention = mentionOf(item);
  const candidates = candidatesOf(item);
  const busy = resolve.isPending;

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
        <KindBadge label="Which one?" />
      </div>
      <p style={{ margin: '6px 0 0', fontSize: 13, color: 'var(--muted)' }}>
        A {mention.type ?? 'node'} mentioned here matches more than one existing entity — pick the
        right one, or create a new one.
      </p>
      <Excerpt text={item.excerpt} />

      <div style={{ display: 'flex', flexDirection: 'column', gap: 8, marginTop: 14 }}>
        {candidates.map((c) => (
          <button
            key={c.id}
            onClick={() => pick(c.id)}
            disabled={busy}
            style={{
              display: 'flex',
              flexDirection: 'column',
              gap: 4,
              textAlign: 'left',
              padding: 12,
              borderRadius: 'var(--radius)',
              border: '1px solid var(--surface-border)',
              background: 'transparent',
              color: 'var(--text)',
              cursor: busy ? 'default' : 'pointer',
              opacity: busy ? 0.6 : 1,
            }}
          >
            <span style={{ fontSize: 14, fontWeight: 600 }}>
              {c.name ?? c.id}
              {c.disambig && (
                <span style={{ fontWeight: 400, color: 'var(--muted)' }}> — {c.disambig}</span>
              )}
            </span>
            {c.aliases.length > 0 && (
              <span style={{ fontSize: 12, color: 'var(--muted)' }}>
                also known as {c.aliases.join(', ')}
              </span>
            )}
          </button>
        ))}
      </div>

      <div style={{ display: 'flex', gap: 10, marginTop: 14, flexWrap: 'wrap' }}>
        <Button onClick={() => pick('new')} disabled={busy}>
          New entity
        </Button>
        <Button variant="ghost" onClick={() => pick('maybe')} disabled={busy}>
          Not sure — later
        </Button>
      </div>
      {resolve.isError && (
        <p style={{ margin: '12px 0 0', fontSize: 13, color: FAIL_COLOR }}>
          Couldn’t resolve that — try again.
        </p>
      )}
    </Surface>
  );
}

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
      {resolve.isError && (
        <p style={{ margin: '12px 0 0', fontSize: 13, color: FAIL_COLOR }}>
          Couldn’t resolve that — try again.
        </p>
      )}
    </Surface>
  );
}

function ReviewCard({ item }: { item: ReviewItemResponse }) {
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
      ) : (
        // Unknown/future kind (stance-candidate, dedup-proposal — M6): show it, don't hide it.
        <Surface>
          <KindBadge label={item.kind} />
          <Excerpt text={item.excerpt} />
        </Surface>
      )}
    </motion.div>
  );
}

export function ReviewScreen() {
  const { data, isLoading, isError } = useReview();

  return (
    <div style={{ display: 'grid', gap: 16 }}>
      <h1 style={{ margin: 0, fontSize: 24, fontWeight: 700, letterSpacing: -0.4 }}>Review</h1>
      <p style={{ margin: '-6px 0 0', fontSize: 14, color: 'var(--muted)', lineHeight: 1.5 }}>
        Decisions your brain couldn’t make on its own — ambiguous entities and proposed new types.
      </p>

      {isLoading ? (
        <p style={{ margin: 0, fontSize: 14, color: 'var(--muted)' }}>Loading…</p>
      ) : isError ? (
        <p style={{ margin: 0, fontSize: 14, color: FAIL_COLOR }}>Couldn’t load the review queue.</p>
      ) : !data || data.length === 0 ? (
        <Surface>
          <p style={{ margin: 0, fontSize: 14, color: 'var(--muted)', lineHeight: 1.6 }}>
            Nothing to review. When the organizer isn’t sure which entity a mention refers to, or
            proposes a new type, it’ll wait for you here.
          </p>
        </Surface>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          <AnimatePresence initial={false}>
            {data.map((item) => (
              <ReviewCard key={item.id} item={item} />
            ))}
          </AnimatePresence>
        </div>
      )}
    </div>
  );
}
