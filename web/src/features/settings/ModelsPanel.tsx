import { useEffect, useRef, useState } from 'react';
import type { CSSProperties } from 'react';
import { AnimatePresence, motion, useReducedMotion } from 'framer-motion';
import type { GroupRoutingModel, ModelRoutingUpdate, RoutingModelItem } from '../../api/types';
import { Button } from '../../ui/Button';
import { Surface } from '../../ui/Surface';
import { useSaveModels, useSettings } from './useModels';

// Settings → Models (06 §4, M4 / ADR-025 + ADR-043): the 3 UI-editable routing groups. Each is an
// active-model dropdown + a fallback dropdown + an effort selector shown only for the selected
// models that support it (Claude yes, Nebius no). Choices + effort levels are registry-sourced from
// `GET /settings` — never hardcoded here — and saved one group at a time via `PUT /settings/models`,
// forward-live (no restart). This is where the model-and-effort control lives; the chat composer's
// picker is only a per-conversation override of the Chat group's active model.

const FAIL_COLOR = '#ff6b6b';

// Human labels + what each group actually routes (the 6 conspect call sites, the quick lane, chat).
const GROUP_META: Record<string, { label: string; blurb: string }> = {
  chat: { label: 'Chat', blurb: 'Answers in the Chat tab.' },
  conspect: { label: 'Conspect', blurb: 'Organizing and distilling your captures.' },
  quick: { label: 'Quick', blurb: 'Fast, cheap tasks like naming chat sessions.' },
};

const selectStyle: CSSProperties = {
  appearance: 'none',
  WebkitAppearance: 'none',
  padding: '9px 12px',
  borderRadius: 'var(--radius)',
  border: '1px solid var(--surface-border)',
  background: 'var(--surface)',
  color: 'var(--text)',
  fontSize: 14,
  width: '100%',
  cursor: 'pointer',
};

const labelStyle: CSSProperties = {
  display: 'block',
  margin: '0 0 6px',
  fontSize: 11,
  fontWeight: 700,
  letterSpacing: 0.6,
  textTransform: 'uppercase',
  color: 'var(--muted)',
};

// Order-preserving dedup — a group whose active == fallback shows one effort control, not two.
function dedup(values: string[]): string[] {
  const out: string[] = [];
  for (const v of values) if (v && !out.includes(v)) out.push(v);
  return out;
}

// A sensible level for a newly-selected effort model with nothing saved yet: keep the prior choice
// if still valid, else the config-ish default (`medium`), else the middle of the scale.
function defaultEffort(model: RoutingModelItem, prior?: string): string {
  const levels = model.effort_levels;
  if (prior && levels.includes(prior)) return prior;
  if (levels.includes('medium')) return 'medium';
  return levels[Math.floor(levels.length / 2)] ?? levels[0] ?? '';
}

// The effort payload the server expects: one valid level per effort-supporting model in
// {active, fallback}. Stale entries for no-longer-selected models are dropped (so dirty tracking
// and the PUT body both stay normalized to exactly what the server persists).
function effortPayload(
  active: string,
  fallback: string,
  effort: Record<string, string>,
  byId: Map<string, RoutingModelItem>,
): Record<string, string> {
  const out: Record<string, string> = {};
  for (const id of dedup([active, fallback])) {
    const m = byId.get(id);
    if (m?.supports_effort) out[id] = defaultEffort(m, effort[id]);
  }
  return out;
}

function recordsEqual(a: Record<string, string>, b: Record<string, string>): boolean {
  const ak = Object.keys(a);
  if (ak.length !== Object.keys(b).length) return false;
  return ak.every((k) => a[k] === b[k]);
}

function EffortControl({
  levels,
  value,
  onChange,
}: {
  levels: string[];
  value: string;
  onChange: (level: string) => void;
}) {
  return (
    <div
      role="radiogroup"
      style={{
        display: 'inline-flex',
        flexWrap: 'wrap',
        borderRadius: 'var(--radius)',
        border: '1px solid var(--surface-border)',
        overflow: 'hidden',
      }}
    >
      {levels.map((lv) => {
        const on = lv === value;
        return (
          <button
            key={lv}
            type="button"
            role="radio"
            aria-checked={on}
            onClick={() => onChange(lv)}
            style={{
              padding: '7px 12px',
              fontSize: 12,
              fontWeight: 600,
              textTransform: 'capitalize',
              background: on ? 'var(--accent)' : 'transparent',
              color: on ? 'var(--on-accent)' : 'var(--muted)',
              border: 'none',
              cursor: 'pointer',
            }}
          >
            {lv}
          </button>
        );
      })}
    </div>
  );
}

function GroupCard({ group }: { group: GroupRoutingModel }) {
  const save = useSaveModels();
  const reduceMotion = useReducedMotion();
  const byId = new Map(group.models.map((m) => [m.id, m] as const));
  const meta = GROUP_META[group.group] ?? { label: group.group, blurb: '' };

  const [active, setActive] = useState(group.active);
  const [fallback, setFallback] = useState(group.fallback);
  const [effort, setEffort] = useState<Record<string, string>>(group.effort_by_model);

  // Re-seed the draft when the server value actually changes (after a save, or an external
  // refetch) — but not on every background refetch, so in-progress edits are never clobbered.
  const signature = JSON.stringify([group.active, group.fallback, group.effort_by_model]);
  const lastSig = useRef<string | null>(null);
  useEffect(() => {
    if (lastSig.current === signature) return;
    lastSig.current = signature;
    setActive(group.active);
    setFallback(group.fallback);
    setEffort(group.effort_by_model);
  }, [signature, group]);

  const payload = effortPayload(active, fallback, effort, byId);
  const dirty =
    active !== group.active ||
    fallback !== group.fallback ||
    !recordsEqual(payload, group.effort_by_model);

  // The distinct selected models that carry an effort selector (usually just the active model;
  // the fallback appears too when it's a second effort-capable model like claude-sonnet-4-6).
  const effortModels = dedup([active, fallback])
    .map((id) => byId.get(id))
    .filter((m): m is RoutingModelItem => !!m && m.supports_effort);

  function onSave() {
    const update: ModelRoutingUpdate = {
      group: group.group as ModelRoutingUpdate['group'],
      active,
      fallback,
      effort_by_model: payload,
    };
    save.mutate(update);
  }

  return (
    <div
      style={{
        padding: 16,
        borderRadius: 'var(--radius)',
        border: '1px solid var(--surface-border)',
        display: 'grid',
        gap: 14,
      }}
    >
      <div>
        <h3 style={{ margin: '0 0 2px', fontSize: 15, fontWeight: 700 }}>{meta.label}</h3>
        {meta.blurb && (
          <p style={{ margin: 0, fontSize: 12.5, color: 'var(--muted)' }}>{meta.blurb}</p>
        )}
      </div>

      <div
        style={{
          display: 'grid',
          gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))',
          gap: 12,
        }}
      >
        <div>
          <label style={labelStyle} htmlFor={`${group.group}-active`}>
            Model
          </label>
          <select
            id={`${group.group}-active`}
            value={active}
            onChange={(e) => setActive(e.target.value)}
            style={selectStyle}
          >
            {group.models.map((m) => (
              <option key={m.id} value={m.id}>
                {m.label}
              </option>
            ))}
          </select>
        </div>

        <div>
          <label style={labelStyle} htmlFor={`${group.group}-fallback`}>
            Fallback
          </label>
          <select
            id={`${group.group}-fallback`}
            value={fallback}
            onChange={(e) => setFallback(e.target.value)}
            style={selectStyle}
          >
            <option value="">None</option>
            {group.models.map((m) => (
              <option key={m.id} value={m.id}>
                {m.label}
              </option>
            ))}
          </select>
        </div>
      </div>

      {effortModels.length > 0 && (
        <div style={{ display: 'grid', gap: 10 }}>
          <span style={labelStyle}>Reasoning effort</span>
          {effortModels.map((m) => (
            <div
              key={m.id}
              style={{
                display: 'flex',
                flexWrap: 'wrap',
                alignItems: 'center',
                justifyContent: 'space-between',
                gap: 10,
              }}
            >
              <span style={{ fontSize: 13, color: 'var(--text)' }}>{m.label}</span>
              <EffortControl
                levels={m.effort_levels}
                value={payload[m.id] ?? ''}
                onChange={(lv) => setEffort((prev) => ({ ...prev, [m.id]: lv }))}
              />
            </div>
          ))}
        </div>
      )}

      <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
        <Button onClick={onSave} disabled={!dirty || save.isPending} style={{ padding: '9px 18px', fontSize: 14 }}>
          {save.isPending ? 'Saving…' : 'Save'}
        </Button>
        <AnimatePresence>
          {save.isSuccess && !dirty && (
            <motion.span
              initial={{ opacity: 0, x: reduceMotion ? 0 : -4 }}
              animate={{ opacity: 1, x: 0 }}
              exit={{ opacity: 0 }}
              style={{ fontSize: 13, color: 'var(--muted)' }}
            >
              Saved
            </motion.span>
          )}
        </AnimatePresence>
        {save.isError && (
          <span style={{ fontSize: 13, color: FAIL_COLOR }}>
            {save.error instanceof Error ? save.error.message : 'Couldn’t save — try again.'}
          </span>
        )}
      </div>
    </div>
  );
}

export function ModelsPanel() {
  const { data, isLoading, isError } = useSettings();

  return (
    <Surface>
      <h2 style={{ margin: '0 0 6px', fontSize: 16 }}>Models</h2>
      <p style={{ margin: '0 0 16px', fontSize: 13, color: 'var(--muted)', lineHeight: 1.5 }}>
        Which model answers each kind of work, with a fallback and reasoning effort. Changes apply
        immediately — no restart.
      </p>

      {isLoading ? (
        <p style={{ margin: 0, fontSize: 13, color: 'var(--muted)' }}>Loading…</p>
      ) : isError || !data ? (
        <p style={{ margin: 0, fontSize: 13, color: FAIL_COLOR }}>Couldn’t load the model settings.</p>
      ) : (
        <div style={{ display: 'grid', gap: 14 }}>
          {data.groups.map((g) => (
            <GroupCard key={g.group} group={g} />
          ))}
        </div>
      )}
    </Surface>
  );
}
