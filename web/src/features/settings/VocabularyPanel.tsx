import { AnimatePresence, motion } from 'framer-motion';
import type { VocabProposalItem } from '../../api/types';
import { Button } from '../../ui/Button';
import { Surface } from '../../ui/Surface';
import { typeIcon } from '../../ui/nodeTypes';
import { useResolveVocabulary, useVocabulary } from './useVocabulary';

// Settings → Vocabulary (06 §4, M3 / ADR-027): the governed node/edge type vocabularies with the
// organizer's pending proposals — approve (goes live everywhere + triggers a consolidation pass)
// or reject. Entity-like types are flagged since they carry the entity substrate (aliases/profile).

const FAIL_COLOR = '#ff6b6b';

function VocabList({
  title,
  values,
  entityLike,
  withIcons,
}: {
  title: string;
  values: string[];
  entityLike?: ReadonlySet<string>;
  withIcons?: boolean;
}) {
  return (
    <div>
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
        {title}
      </p>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
        {values.map((v) => {
          const isEntity = entityLike?.has(v);
          return (
            <span
              key={v}
              title={isEntity ? 'entity type — carries aliases + a derived profile' : undefined}
              style={{
                display: 'inline-flex',
                alignItems: 'center',
                gap: 5,
                fontSize: 12,
                fontWeight: 600,
                color: 'var(--text)',
                background: 'var(--surface)',
                border: isEntity ? '1px solid var(--accent)' : '1px solid var(--surface-border)',
                borderRadius: 999,
                padding: '4px 10px',
              }}
            >
              {withIcons && <span aria-hidden>{typeIcon(v)}</span>}
              {v}
            </span>
          );
        })}
      </div>
    </div>
  );
}

function ProposalRow({ proposal }: { proposal: VocabProposalItem }) {
  const resolve = useResolveVocabulary();
  const busy = resolve.isPending;
  const axis = (proposal.vocab ?? 'type').replace(/_/g, ' ');

  return (
    <motion.div
      layout
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: -6 }}
      style={{
        padding: 12,
        borderRadius: 'var(--radius)',
        border: '1px solid var(--surface-border)',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 10 }}>
        <span style={{ fontSize: 14, fontWeight: 700, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis' }}>
          {proposal.value ?? '(unnamed)'}
          <span style={{ fontWeight: 400, color: 'var(--muted)' }}> · {axis}</span>
        </span>
      </div>
      {proposal.excerpt && (
        <p
          style={{
            margin: '8px 0 0',
            fontSize: 12,
            lineHeight: 1.5,
            color: 'var(--muted)',
            fontStyle: 'italic',
          }}
        >
          “{proposal.excerpt.trim()}”
        </p>
      )}
      <div style={{ display: 'flex', gap: 8, marginTop: 12 }}>
        <Button
          onClick={() => resolve.mutate({ reviewId: proposal.id, verdict: 'approve' })}
          disabled={busy}
          style={{ padding: '8px 16px', fontSize: 13 }}
        >
          Approve
        </Button>
        <Button
          variant="ghost"
          onClick={() => resolve.mutate({ reviewId: proposal.id, verdict: 'reject' })}
          disabled={busy}
          style={{ padding: '8px 16px', fontSize: 13 }}
        >
          Reject
        </Button>
      </div>
      {resolve.isError && (
        <p style={{ margin: '10px 0 0', fontSize: 12, color: FAIL_COLOR }}>
          Couldn’t resolve that — try again.
        </p>
      )}
    </motion.div>
  );
}

export function VocabularyPanel() {
  const { data, isLoading, isError } = useVocabulary();
  const entityLike = new Set(data?.entity_like_types ?? []);

  return (
    <Surface>
      <h2 style={{ margin: '0 0 6px', fontSize: 16 }}>Vocabulary</h2>
      <p style={{ margin: '0 0 16px', fontSize: 13, color: 'var(--muted)', lineHeight: 1.5 }}>
        The node and edge types your brain uses. Entity types (outlined) carry aliases and a derived
        profile. Approve a proposal to add it — it goes live everywhere and re-walks existing edges.
      </p>

      {isLoading ? (
        <p style={{ margin: 0, fontSize: 13, color: 'var(--muted)' }}>Loading…</p>
      ) : isError || !data ? (
        <p style={{ margin: 0, fontSize: 13, color: FAIL_COLOR }}>Couldn’t load the vocabulary.</p>
      ) : (
        <div style={{ display: 'grid', gap: 16 }}>
          <VocabList
            title="Node types"
            values={data.node_types}
            entityLike={entityLike}
            withIcons
          />
          <VocabList title="Edge relations" values={data.edge_rels} />

          <div>
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
              Pending proposals
            </p>
            {data.proposals.length === 0 ? (
              <p style={{ margin: 0, fontSize: 13, color: 'var(--muted)' }}>
                None — the organizer hasn’t proposed any new types.
              </p>
            ) : (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                <AnimatePresence initial={false}>
                  {data.proposals.map((p) => (
                    <ProposalRow key={p.id} proposal={p} />
                  ))}
                </AnimatePresence>
              </div>
            )}
          </div>
        </div>
      )}
    </Surface>
  );
}
