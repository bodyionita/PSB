import { AnimatePresence, motion } from 'framer-motion';
import { useState, type CSSProperties } from 'react';
import { ApiError } from '../../api/client';
import type {
  AgentRunResponse,
  EntityBrowseItem,
  EntityMergeProposeResponse,
  ReprocessPreview,
  TagMergeItem,
  VocabConsolidateProposeResponse,
} from '../../api/types';
import { Button } from '../../ui/Button';
import { EntityPicker } from '../../ui/EntityPicker';
import { Surface } from '../../ui/Surface';
import { RunLogTail } from './RunLogTail';
import { StatusBadge } from './runStatus';
import { FAIL_COLOR, WARN_COLOR } from './statusColors';
import {
  useApplyTags,
  useConsolidateVocabApply,
  useConsolidateVocabPropose,
  useMergeEntitiesApply,
  useMergeEntitiesPropose,
  useProposeTags,
  useReprocessConfirm,
  useReprocessPreview,
  useRun,
} from './useActivity';

// The parameterized admin ops (06 §3, ADR-053 §8), rehomed from the M2 Admin panel into the ops
// console: they carry input controls / confirm gates, so they **cannot** collapse to a bare Run (a
// merge is meaningless without its two nodes) — unlike the zero-arg reindex/backup jobs, which are
// the roster's plain Run buttons above. Each returns a background run_id we poll (GET
// /activity/runs/{id}) + tail live (GET …/logs).

function errorText(err: unknown, fallback: string): string {
  return err instanceof ApiError ? err.message : fallback;
}

const pillStyle: CSSProperties = {
  fontSize: 11,
  fontWeight: 600,
  color: 'var(--muted)',
  border: '1px solid var(--surface-border)',
  borderRadius: 999,
  padding: '3px 9px',
  fontVariantNumeric: 'tabular-nums',
};

const inputStyle: CSSProperties = {
  width: '100%',
  // Inputs carry an intrinsic min-width from their default size; without these they can push a grid
  // track (and the whole page) wider than a phone viewport.
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

// Compact numeric/flag readout of a run's `details` (shape varies per agent), rendered generically.
function DetailPills({ details }: { details: Record<string, unknown> }) {
  const pills = Object.entries(details).filter(([, v]) => typeof v === 'number' || v === true);
  if (pills.length === 0) return null;
  return (
    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginTop: 10 }}>
      {pills.map(([k, v]) => (
        <span key={k} style={pillStyle}>
          {k} {v === true ? '✓' : (v as number)}
        </span>
      ))}
    </div>
  );
}

// Live status of a background run (status + fallback badge + summary + counts + the live log tail);
// polls until terminal, then drains the tail.
function RunPanel({ run }: { run: AgentRunResponse | undefined }) {
  if (!run) return null;
  return (
    <motion.div
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      style={{ marginTop: 14, paddingTop: 14, borderTop: '1px solid var(--surface-border)' }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
        <StatusBadge status={run.status} />
        {run.fallback_used && (
          <span style={{ ...pillStyle, color: WARN_COLOR, borderColor: WARN_COLOR }}>
            fallback{run.model_used ? ` · ${run.model_used}` : ''}
          </span>
        )}
      </div>
      {run.summary && (
        <p style={{ margin: '8px 0 0', fontSize: 13, color: 'var(--text)', lineHeight: 1.5 }}>
          {run.summary}
        </p>
      )}
      {run.error && (
        <p style={{ margin: '8px 0 0', fontSize: 13, color: FAIL_COLOR, lineHeight: 1.5 }}>
          {run.error}
        </p>
      )}
      <DetailPills details={run.details} />
      <RunLogTail runId={run.id} />
    </motion.div>
  );
}

function OpCard({
  title,
  description,
  children,
}: {
  title: string;
  description: string;
  children: React.ReactNode;
}) {
  return (
    <Surface>
      <h3 style={{ margin: '0 0 6px', fontSize: 15 }}>{title}</h3>
      <p style={{ margin: '0 0 16px', fontSize: 13, color: 'var(--muted)', lineHeight: 1.5 }}>
        {description}
      </p>
      {children}
    </Surface>
  );
}

// --- Tags consolidation (two-step propose → apply) ----------------------------------------------

function MergeReview({
  merges,
  onApply,
  onDiscard,
  applying,
}: {
  merges: TagMergeItem[];
  onApply: () => void;
  onDiscard: () => void;
  applying: boolean;
}) {
  if (merges.length === 0) {
    return (
      <p style={{ margin: '12px 0 0', fontSize: 13, color: 'var(--muted)' }}>
        Nothing to consolidate — your tags are already tidy.
      </p>
    );
  }
  return (
    <div style={{ marginTop: 14 }}>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
        {merges.map((m) => (
          <div
            key={m.canonical}
            style={{
              padding: 12,
              borderRadius: 'var(--radius)',
              border: '1px solid var(--surface-border)',
            }}
          >
            <span style={{ fontSize: 13, fontWeight: 700, color: 'var(--accent)' }}>
              #{m.canonical}
            </span>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginTop: 8 }}>
              {m.variants.map((v) => (
                <span
                  key={v}
                  style={{
                    fontSize: 11,
                    color: 'var(--muted)',
                    textDecoration: 'line-through',
                    border: '1px solid var(--surface-border)',
                    borderRadius: 999,
                    padding: '2px 8px',
                  }}
                >
                  #{v}
                </span>
              ))}
            </div>
          </div>
        ))}
      </div>
      <div style={{ display: 'flex', gap: 10, marginTop: 14 }}>
        <Button onClick={onApply} disabled={applying}>
          {applying ? 'Applying…' : `Apply ${merges.length} merge${merges.length > 1 ? 's' : ''}`}
        </Button>
        <Button variant="ghost" onClick={onDiscard} disabled={applying}>
          Discard
        </Button>
      </div>
    </div>
  );
}

function ConsolidateTagsCard() {
  const propose = useProposeTags();
  const apply = useApplyTags();
  const [merges, setMerges] = useState<TagMergeItem[] | null>(null);
  const [runId, setRunId] = useState<string | null>(null);
  const run = useRun(runId);

  const proposeUnavailable = propose.error instanceof ApiError && propose.error.status === 503;

  return (
    <OpCard
      title="Consolidate tags"
      description="Propose a merge plan that folds near-duplicate tags together, review it, then apply — rewriting the affected nodes."
    >
      <Button
        variant="ghost"
        onClick={() => {
          setRunId(null);
          propose.mutate(undefined, { onSuccess: (r) => setMerges(r.merges) });
        }}
        disabled={propose.isPending}
      >
        {propose.isPending ? 'Proposing…' : merges != null ? 'Re-propose' : 'Propose merges'}
      </Button>
      {propose.isError && (
        <p style={{ margin: '12px 0 0', fontSize: 13, color: FAIL_COLOR }}>
          {proposeUnavailable
            ? 'Tag consolidation is unavailable right now (model).'
            : errorText(propose.error, 'Couldn’t build a proposal.')}
        </p>
      )}
      {merges != null && (
        <MergeReview
          merges={merges}
          applying={apply.isPending}
          onDiscard={() => setMerges(null)}
          onApply={() =>
            apply.mutate(merges, {
              onSuccess: (r) => {
                setRunId(r.run_id);
                setMerges(null);
              },
            })
          }
        />
      )}
      {apply.isError && (
        <p style={{ margin: '12px 0 0', fontSize: 13, color: FAIL_COLOR }}>
          {errorText(apply.error, 'Couldn’t apply the merges.')}
        </p>
      )}
      <AnimatePresence>{runId && <RunPanel run={run.data} />}</AnimatePresence>
    </OpCard>
  );
}

// --- Reprocess everything (confirm-gated, P10) --------------------------------------------------

function ReprocessCard() {
  const preview = useReprocessPreview();
  const confirm = useReprocessConfirm();
  const [plan, setPlan] = useState<ReprocessPreview | null>(null);
  const [runId, setRunId] = useState<string | null>(null);
  const run = useRun(runId);

  return (
    <OpCard
      title="Reprocess everything"
      description="Replay every stored capture's raw text through the current pipeline — the data-survival pass (P10). Raw inputs and approved vocabulary are preserved; derived nodes, edges, and profiles are rebuilt."
    >
      <Button
        variant="ghost"
        onClick={() => {
          setRunId(null);
          preview.mutate(undefined, { onSuccess: (p) => setPlan(p) });
        }}
        disabled={preview.isPending || confirm.isPending}
      >
        {preview.isPending ? 'Previewing…' : plan != null ? 'Re-preview' : 'Preview reprocess'}
      </Button>
      {preview.isError && (
        <p style={{ margin: '12px 0 0', fontSize: 13, color: FAIL_COLOR }}>
          {errorText(preview.error, 'Couldn’t build the reprocess preview.')}
        </p>
      )}
      {plan != null && (
        <div style={{ marginTop: 14 }}>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
            {(['captures', 'nodes', 'merges'] as const).map((k) => (
              <span key={k} style={pillStyle}>
                {k} {plan[k]}
              </span>
            ))}
          </div>
          <p style={{ margin: '12px 0 0', fontSize: 12.5, color: WARN_COLOR, lineHeight: 1.5 }}>
            This rewrites all derived graph state and force-pushes. It runs in the background and can
            take a while.
          </p>
          <div style={{ display: 'flex', gap: 10, marginTop: 12 }}>
            <Button
              onClick={() =>
                confirm.mutate(undefined, {
                  onSuccess: (r) => {
                    setRunId(r.run_id);
                    setPlan(null);
                  },
                })
              }
              disabled={confirm.isPending}
            >
              {confirm.isPending ? 'Starting…' : `Reprocess ${plan.captures} captures`}
            </Button>
            <Button variant="ghost" onClick={() => setPlan(null)} disabled={confirm.isPending}>
              Cancel
            </Button>
          </div>
        </div>
      )}
      {confirm.isError && (
        <p style={{ margin: '12px 0 0', fontSize: 13, color: FAIL_COLOR }}>
          {confirm.error instanceof ApiError && confirm.error.status === 409
            ? 'A reprocess is already running.'
            : errorText(confirm.error, 'Couldn’t start the reprocess.')}
        </p>
      )}
      <AnimatePresence>{runId && <RunPanel run={run.data} />}</AnimatePresence>
    </OpCard>
  );
}

// --- Entity merge (two-step propose → apply; ADR-030 §5) ----------------------------------------

function MergeSideBox({ label, side }: { label: string; side: { title: string | null; id: string; type: string; aliases: string[] } }) {
  return (
    <div style={{ padding: 12, borderRadius: 'var(--radius)', border: '1px solid var(--surface-border)', display: 'grid', gap: 4 }}>
      <span style={{ fontSize: 11, fontWeight: 700, textTransform: 'uppercase', letterSpacing: 0.4, color: 'var(--muted)' }}>
        {label}
      </span>
      <span style={{ fontSize: 14, fontWeight: 600, overflowWrap: 'anywhere' }}>{side.title ?? side.id}</span>
      <span style={{ fontSize: 12, color: 'var(--muted)', overflowWrap: 'anywhere' }}>
        {side.type}
        {side.aliases.length > 0 ? ` · aliases: ${side.aliases.join(', ')}` : ''}
      </span>
    </div>
  );
}

function PickerLabel({ children }: { children: string }) {
  return (
    <span
      style={{
        display: 'block',
        marginBottom: 6,
        fontSize: 11,
        fontWeight: 700,
        textTransform: 'uppercase',
        letterSpacing: 0.4,
        color: 'var(--muted)',
      }}
    >
      {children}
    </span>
  );
}

function EntityMergeCard() {
  const propose = useMergeEntitiesPropose();
  const apply = useMergeEntitiesApply();
  const [loser, setLoser] = useState<EntityBrowseItem | null>(null);
  const [survivor, setSurvivor] = useState<EntityBrowseItem | null>(null);
  const [plan, setPlan] = useState<EntityMergeProposeResponse | null>(null);
  const [runId, setRunId] = useState<string | null>(null);
  const run = useRun(runId);
  const canPropose = loser != null && survivor != null && loser.id !== survivor.id;

  return (
    <OpCard
      title="Merge entities"
      description="Fold one entity (the loser) into another (the survivor): retarget its inbound edges, union aliases, and tombstone it. Search each entity by name, review the inbound-edge inventory, then apply."
    >
      <div style={{ display: 'grid', gap: 12 }}>
        <div>
          <PickerLabel>Loser (folded away)</PickerLabel>
          <EntityPicker
            value={loser}
            onChange={(v) => {
              setLoser(v);
              setPlan(null);
            }}
            excludeId={survivor?.id}
            placeholder="Search the entity to fold away…"
          />
        </div>
        <div>
          <PickerLabel>Survivor (kept)</PickerLabel>
          <EntityPicker
            value={survivor}
            onChange={(v) => {
              setSurvivor(v);
              setPlan(null);
            }}
            excludeId={loser?.id}
            placeholder="Search the entity to keep…"
          />
        </div>
      </div>
      <div style={{ marginTop: 12 }}>
        <Button
          variant="ghost"
          onClick={() => {
            if (!canPropose) return;
            setRunId(null);
            propose.mutate(
              { loser: loser.id, survivor: survivor.id },
              { onSuccess: (p) => setPlan(p) },
            );
          }}
          disabled={!canPropose || propose.isPending}
        >
          {propose.isPending ? 'Checking…' : plan != null ? 'Re-check' : 'Preview merge'}
        </Button>
      </div>
      {propose.isError && (
        <p style={{ margin: '12px 0 0', fontSize: 13, color: FAIL_COLOR }}>
          {propose.error instanceof ApiError && propose.error.status === 404
            ? 'One of those entities was not found.'
            : errorText(propose.error, 'Couldn’t preview the merge.')}
        </p>
      )}
      {plan != null && (
        <div style={{ marginTop: 14, display: 'grid', gap: 10 }}>
          <MergeSideBox label="Loser" side={plan.loser} />
          <MergeSideBox label="Survivor" side={plan.survivor} />
          <span style={pillStyle}>{plan.inbound_count} inbound edge{plan.inbound_count === 1 ? '' : 's'} retargeted</span>
          <div style={{ display: 'flex', gap: 10, marginTop: 4 }}>
            <Button
              onClick={() =>
                apply.mutate(
                  { loser: plan.loser.id, survivor: plan.survivor.id },
                  { onSuccess: (r) => { setRunId(r.run_id); setPlan(null); } },
                )
              }
              disabled={apply.isPending}
            >
              {apply.isPending ? 'Merging…' : 'Merge'}
            </Button>
            <Button variant="ghost" onClick={() => setPlan(null)} disabled={apply.isPending}>
              Cancel
            </Button>
          </div>
        </div>
      )}
      {apply.isError && (
        <p style={{ margin: '12px 0 0', fontSize: 13, color: FAIL_COLOR }}>
          {errorText(apply.error, 'Couldn’t apply the merge.')}
        </p>
      )}
      <AnimatePresence>{runId && <RunPanel run={run.data} />}</AnimatePresence>
    </OpCard>
  );
}

// --- Edge vocab consolidation (two-step propose → apply; ADR-036) -------------------------------

function VocabConsolidateCard() {
  const propose = useConsolidateVocabPropose();
  const apply = useConsolidateVocabApply();
  const [rel, setRel] = useState('');
  const [plan, setPlan] = useState<VocabConsolidateProposeResponse | null>(null);
  const [runId, setRunId] = useState<string | null>(null);
  const run = useRun(runId);
  const unavailable = propose.error instanceof ApiError && propose.error.status === 503;
  const badRel = propose.error instanceof ApiError && propose.error.status === 400;

  return (
    <OpCard
      title="Consolidate edge relations"
      description="Re-type existing edges onto a newly-approved relation. Enter the target relation, review the proposed re-typings, then apply — rewriting the edges' frontmatter and reindexing."
    >
      <input
        style={inputStyle}
        placeholder="Approved edge relation (e.g. mentors)"
        value={rel}
        onChange={(e) => setRel(e.target.value)}
      />
      <div style={{ marginTop: 12 }}>
        <Button
          variant="ghost"
          onClick={() => {
            setRunId(null);
            propose.mutate(rel.trim(), { onSuccess: (p) => setPlan(p) });
          }}
          disabled={rel.trim() === '' || propose.isPending}
        >
          {propose.isPending ? 'Proposing…' : plan != null ? 'Re-propose' : 'Propose re-typings'}
        </Button>
      </div>
      {propose.isError && (
        <p style={{ margin: '12px 0 0', fontSize: 13, color: FAIL_COLOR }}>
          {unavailable
            ? 'Edge consolidation is unavailable right now (model).'
            : badRel
              ? errorText(propose.error, 'That relation is unknown or empty.')
              : errorText(propose.error, 'Couldn’t build a proposal.')}
        </p>
      )}
      {plan != null && (
        <div style={{ marginTop: 14 }}>
          {plan.retypings.length === 0 ? (
            <p style={{ margin: 0, fontSize: 13, color: 'var(--muted)' }}>
              No edges need re-typing onto <b>{plan.rel}</b>.
            </p>
          ) : (
            <>
              <span style={pillStyle}>
                {plan.retypings.length} edge{plan.retypings.length === 1 ? '' : 's'} → {plan.rel}
              </span>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 6, marginTop: 10 }}>
                {plan.retypings.slice(0, 8).map((r, i) => (
                  <span key={`${r.src_id}:${r.to}:${i}`} style={{ fontSize: 12, color: 'var(--muted)', overflowWrap: 'anywhere' }}>
                    {r.from_rel} → <b style={{ color: 'var(--text)' }}>{r.to_rel}</b> (on {r.src_id})
                  </span>
                ))}
                {plan.retypings.length > 8 && (
                  <span style={{ fontSize: 12, color: 'var(--muted)' }}>
                    +{plan.retypings.length - 8} more
                  </span>
                )}
              </div>
              <div style={{ display: 'flex', gap: 10, marginTop: 14 }}>
                <Button
                  onClick={() =>
                    apply.mutate(
                      { rel: plan.rel, plan: plan.retypings },
                      { onSuccess: (r) => { setRunId(r.run_id); setPlan(null); } },
                    )
                  }
                  disabled={apply.isPending}
                >
                  {apply.isPending ? 'Applying…' : `Apply ${plan.retypings.length} re-typing${plan.retypings.length === 1 ? '' : 's'}`}
                </Button>
                <Button variant="ghost" onClick={() => setPlan(null)} disabled={apply.isPending}>
                  Discard
                </Button>
              </div>
            </>
          )}
        </div>
      )}
      {apply.isError && (
        <p style={{ margin: '12px 0 0', fontSize: 13, color: FAIL_COLOR }}>
          {errorText(apply.error, 'Couldn’t apply the re-typings.')}
        </p>
      )}
      <AnimatePresence>{runId && <RunPanel run={run.data} />}</AnimatePresence>
    </OpCard>
  );
}

export function AdminOps() {
  return (
    <div style={{ display: 'grid', gap: 12 }}>
      <ConsolidateTagsCard />
      <ReprocessCard />
      <EntityMergeCard />
      <VocabConsolidateCard />
    </div>
  );
}
