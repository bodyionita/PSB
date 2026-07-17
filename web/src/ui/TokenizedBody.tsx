// Inline date-token rendering + tap-to-edit (M8.2, ADR-056 §4/§5). A node body carries machine
// tokens `[[t:START[/END][|label]]]` that are NEVER shown raw: <TokenizedBody> splits the body and
// renders each token as a live relative phrase (recomputed at render) with a tap/hover exact-date
// tooltip (<HoverTip>). Where the body is editable (NodePreview passes `nodeId`), tapping a token
// opens the mechanical date editor (PUT /nodes/{id}/date-token). Plain text runs render verbatim;
// a structurally-malformed token degrades to its label or inner text, never raw brackets.
import { AnimatePresence, motion, useReducedMotion } from 'framer-motion';
import { createPortal } from 'react-dom';
import { Fragment, useMemo, useState } from 'react';
import { ApiError } from '../api/client';
import { Button } from './Button';
import { HoverTip } from './HoverTip';
import {
  daysInMonthOf,
  findTokens,
  parsePartial,
  partialFromFields,
  partialIso,
  renderAbsolute,
  renderRelative,
  todayCivil,
  type ResolvedTime,
  type TokenMatch,
} from './dateToken';
import { useEditNodeDateToken } from './nodeDetail';

const FAIL_COLOR = '#ff6b6b';

// One rendered token: the live phrase, underlined-accent so it reads as a date; hover previews the
// exact absolute date; tap opens the editor (editable) or toggles the tooltip (read-only).
function DateChip({
  rt,
  onEdit,
}: {
  rt: ResolvedTime;
  onEdit?: () => void;
}) {
  const now = useMemo(() => todayCivil(), []);
  const phrase = renderRelative(rt, now);
  const absolute = renderAbsolute(rt);
  return (
    <HoverTip
      tip={absolute}
      ariaLabel={onEdit ? `${phrase} (${absolute}) — edit date` : `${phrase}, ${absolute}`}
      cursor={onEdit ? 'pointer' : 'help'}
      onActivate={onEdit}
      style={{
        color: 'var(--accent)',
        fontWeight: 600,
        textDecoration: 'underline',
        textDecorationStyle: 'dotted',
        textUnderlineOffset: 2,
      }}
    >
      {phrase}
    </HoverTip>
  );
}

// Render a body string with its date tokens turned into live phrases. `nodeId` (optional) makes the
// tokens editable — omit it for a read-only render.
export function TokenizedBody({ body, nodeId }: { body: string; nodeId?: string }) {
  const [editing, setEditing] = useState<TokenMatch | null>(null);
  const matches = useMemo(() => findTokens(body), [body]);

  if (matches.length === 0) return <>{body}</>;

  const parts: React.ReactNode[] = [];
  let last = 0;
  matches.forEach((mtch, i) => {
    const [start, end] = mtch.span;
    if (start > last) parts.push(<Fragment key={`t${i}`}>{body.slice(last, start)}</Fragment>);
    if (mtch.resolved === null) {
      // Degrade a malformed token to its label / inner text — never raw brackets (ADR-056 §4).
      // `[[t:` is 4 chars and `]]` is 2, so the inner is slice(4, -2).
      const inner = mtch.raw.slice(4, -2);
      const bar = inner.indexOf('|');
      parts.push(<Fragment key={`m${i}`}>{bar === -1 ? inner : inner.slice(bar + 1).trim()}</Fragment>);
    } else {
      const resolved = mtch.resolved;
      parts.push(
        <DateChip
          key={`d${i}`}
          rt={resolved}
          onEdit={nodeId ? () => setEditing(mtch) : undefined}
        />,
      );
    }
    last = end;
  });
  if (last < body.length) parts.push(<Fragment key="tail">{body.slice(last)}</Fragment>);

  return (
    <>
      {parts}
      {nodeId && editing && editing.resolved && (
        <DateTokenEditor
          nodeId={nodeId}
          oldToken={editing.raw}
          initial={editing.resolved}
          onClose={() => setEditing(null)}
        />
      )}
    </>
  );
}

// --- the editor ------------------------------------------------------------------------------

// One partial-date's editable fields as strings (empty = "not set at this granularity"). Coarser
// fields gate finer ones (a day needs a month), matching the server's fail-closed validation.
interface PartialFields {
  year: string;
  month: string;
  day: string;
  time: string; // "HH:MM" or ""
}

function fieldsFromResolved(rt: ResolvedTime): { start: PartialFields; end: PartialFields | null; label: string } {
  const toFields = (pd: ResolvedTime['start']): PartialFields => ({
    year: String(pd.year),
    month: pd.month !== null ? String(pd.month) : '',
    day: pd.day !== null ? String(pd.day) : '',
    time: pd.hour !== null && pd.minute !== null ? `${pad(pd.hour)}:${pad(pd.minute)}` : '',
  });
  return {
    start: toFields(rt.start),
    end: rt.end ? toFields(rt.end) : null,
    label: rt.label ?? '',
  };
}

function pad(n: number): string {
  return String(n).padStart(2, '0');
}

// Build a partial-ISO string from a field set, or an error message (fail-closed, mirrors the server).
function toPartialIso(f: PartialFields, which: string): { iso: string } | { error: string } {
  const year = Number(f.year);
  if (!f.year.trim() || !Number.isInteger(year) || year < 1 || year > 9999)
    return { error: `${which}: enter a 4-digit year` };
  const month = f.month ? Number(f.month) : null;
  const day = f.day ? Number(f.day) : null;
  let hour: number | null = null;
  let minute: number | null = null;
  if (f.time) {
    const [h, m] = f.time.split(':');
    hour = Number(h);
    minute = Number(m);
  }
  const pd = partialFromFields(year, month, day, hour, minute);
  if (pd === null) return { error: `${which}: that isn't a real date` };
  return { iso: partialIso(pd) };
}

const MONTHS = [
  'Jan',
  'Feb',
  'Mar',
  'Apr',
  'May',
  'Jun',
  'Jul',
  'Aug',
  'Sep',
  'Oct',
  'Nov',
  'Dec',
];

const SELECT_STYLE: React.CSSProperties = {
  padding: '8px 10px',
  borderRadius: 'var(--radius)',
  border: '1px solid var(--surface-border)',
  background: 'var(--surface)',
  color: 'var(--text)',
  fontSize: 13,
  outline: 'none',
};

// A year/month/day (+ optional time) picker for one partial. Day is gated on a month; time on a day.
function PartialPicker({
  value,
  onChange,
}: {
  value: PartialFields;
  onChange: (v: PartialFields) => void;
}) {
  const year = Number(value.year);
  const month = value.month ? Number(value.month) : null;
  const nDays = month && Number.isInteger(year) ? daysInMonthOf(year, month) : 31;
  return (
    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, alignItems: 'center' }}>
      <input
        value={value.year}
        onChange={(e) => onChange({ ...value, year: e.target.value.replace(/[^\d]/g, '').slice(0, 4) })}
        inputMode="numeric"
        placeholder="Year"
        aria-label="Year"
        style={{ ...SELECT_STYLE, width: 74 }}
      />
      <select
        value={value.month}
        aria-label="Month"
        onChange={(e) =>
          // Clearing the month clears the finer fields (a day/time can't outlive its month).
          onChange(
            e.target.value
              ? { ...value, month: e.target.value }
              : { ...value, month: '', day: '', time: '' },
          )
        }
        style={SELECT_STYLE}
      >
        <option value="">— month</option>
        {MONTHS.map((m, i) => (
          <option key={m} value={i + 1}>
            {m}
          </option>
        ))}
      </select>
      <select
        value={value.day}
        aria-label="Day"
        disabled={!value.month}
        onChange={(e) =>
          onChange(e.target.value ? { ...value, day: e.target.value } : { ...value, day: '', time: '' })
        }
        style={{ ...SELECT_STYLE, opacity: value.month ? 1 : 0.5 }}
      >
        <option value="">— day</option>
        {Array.from({ length: nDays }, (_, i) => i + 1).map((d) => (
          <option key={d} value={d}>
            {d}
          </option>
        ))}
      </select>
      <input
        type="time"
        value={value.time}
        aria-label="Time of day (optional)"
        disabled={!value.day}
        onChange={(e) => onChange({ ...value, time: e.target.value })}
        style={{ ...SELECT_STYLE, opacity: value.day ? 1 : 0.5, width: 110 }}
      />
    </div>
  );
}

function DateTokenEditor({
  nodeId,
  oldToken,
  initial,
  onClose,
}: {
  nodeId: string;
  oldToken: string;
  initial: ResolvedTime;
  onClose: () => void;
}) {
  const reduce = useReducedMotion();
  const edit = useEditNodeDateToken(nodeId);
  const seed = useMemo(() => fieldsFromResolved(initial), [initial]);
  const [start, setStart] = useState<PartialFields>(seed.start);
  const [end, setEnd] = useState<PartialFields | null>(seed.end);
  const [label, setLabel] = useState(seed.label);
  const [localError, setLocalError] = useState<string | null>(null);
  const now = useMemo(() => todayCivil(), []);

  // A live preview of what the edited token will read as — built from the current fields, or null
  // while they don't form a valid date (the save button then explains why).
  const preview = useMemo<ResolvedTime | null>(() => {
    const s = toPartialIso(start, 'Start');
    if ('error' in s) return null;
    // s.iso came from partialIso() of a valid partial, so parsePartial round-trips it exactly.
    const sPd = parsePartial(s.iso);
    if (!sPd) return null;
    let ePd = null;
    if (end) {
      const e = toPartialIso(end, 'End');
      if ('error' in e) return null;
      ePd = parsePartial(e.iso);
      if (!ePd) return null;
    }
    return { start: sPd, end: ePd, label: label.trim() || null };
  }, [start, end, label]);

  const save = () => {
    setLocalError(null);
    const s = toPartialIso(start, 'Start');
    if ('error' in s) return setLocalError(s.error);
    let endIso: string | undefined;
    if (end) {
      const e = toPartialIso(end, 'End');
      if ('error' in e) return setLocalError(e.error);
      endIso = e.iso;
    }
    edit.mutate(
      { old: oldToken, start: s.iso, end: endIso ?? null, label: label.trim() || null },
      { onSuccess: onClose },
    );
  };

  const serverError = edit.isError
    ? edit.error instanceof ApiError
      ? edit.error.message
      : 'Couldn’t save that date — try again.'
    : null;

  return createPortal(
    <AnimatePresence>
      <motion.div
        key="backdrop"
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        transition={{ duration: 0.18 }}
        onClick={onClose}
        style={{
          position: 'fixed',
          inset: 0,
          zIndex: 60,
          background: 'rgba(0,0,0,0.5)',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          padding: 16,
        }}
      >
        <motion.div
          role="dialog"
          aria-modal="true"
          aria-label="Edit date"
          initial={reduce ? { opacity: 0 } : { opacity: 0, y: 20, scale: 0.98 }}
          animate={{ opacity: 1, y: 0, scale: 1 }}
          exit={reduce ? { opacity: 0 } : { opacity: 0, y: 20, scale: 0.98 }}
          transition={{ type: 'spring', stiffness: 420, damping: 34 }}
          onClick={(e) => e.stopPropagation()}
          style={{
            background: 'var(--bg)',
            border: '1px solid var(--surface-border)',
            borderRadius: 16,
            padding: 20,
            width: '100%',
            maxWidth: 420,
            maxHeight: '85dvh',
            overflow: 'auto',
            display: 'grid',
            gap: 14,
          }}
        >
          <h2 style={{ margin: 0, fontSize: 17, fontWeight: 700, letterSpacing: -0.2 }}>Edit date</h2>

          <div style={{ display: 'grid', gap: 6 }}>
            <label style={LABEL_STYLE}>Date</label>
            <PartialPicker value={start} onChange={setStart} />
          </div>

          <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 13, color: 'var(--text)' }}>
            <input
              type="checkbox"
              checked={end !== null}
              onChange={(e) => setEnd(e.target.checked ? seed.end ?? { ...start } : null)}
              style={{ width: 16, height: 16, accentColor: 'var(--accent)' }}
            />
            This is a range
          </label>

          {end !== null && (
            <div style={{ display: 'grid', gap: 6 }}>
              <label style={LABEL_STYLE}>End</label>
              <PartialPicker value={end} onChange={setEnd} />
            </div>
          )}

          <div style={{ display: 'grid', gap: 6 }}>
            <label style={LABEL_STYLE}>Label (optional)</label>
            <input
              value={label}
              onChange={(e) => setLabel(e.target.value)}
              placeholder="e.g. summer 2025"
              style={{ ...SELECT_STYLE, width: '100%' }}
            />
          </div>

          <p style={{ margin: 0, fontSize: 13, color: 'var(--muted)' }}>
            Reads as:{' '}
            <span style={{ color: 'var(--accent)', fontWeight: 600 }}>
              {preview ? renderRelative(preview, now) : '—'}
            </span>
          </p>

          {(localError || serverError) && (
            <p style={{ margin: 0, fontSize: 13, color: FAIL_COLOR }}>{localError ?? serverError}</p>
          )}

          <div style={{ display: 'flex', gap: 10, justifyContent: 'flex-end' }}>
            <Button variant="ghost" onClick={onClose} disabled={edit.isPending}>
              Cancel
            </Button>
            <Button onClick={save} disabled={edit.isPending || !preview}>
              {edit.isPending ? 'Saving…' : 'Save'}
            </Button>
          </div>
        </motion.div>
      </motion.div>
    </AnimatePresence>,
    document.body,
  );
}

const LABEL_STYLE: React.CSSProperties = {
  fontSize: 11,
  fontWeight: 700,
  letterSpacing: 0.5,
  textTransform: 'uppercase',
  color: 'var(--muted)',
};
