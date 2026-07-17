// Web mirror of the server temporal token + render library (ADR-056 §3/§4; server
// `app/temporal/tokens.py` + `render.py`). Node bodies carry inline date tokens
// `[[t:START[/END][|label]]]` that are NEVER shown raw — this module parses them and renders a live
// relative phrase (recomputed at render against *now*) plus an absolute form for the tooltip/editor.
//
// It is a DELIBERATE BYTE-FOR-BYTE MIRROR of the server's stdlib-only logic so the two agree exactly:
//   - partial-ISO parse/serialize with honest granularity (year / month / day / minute),
//   - the round-HALF-UP humanizer (`Math.floor(x + 0.5)`, matching the server's `_round` — NOT
//     `Math.round`, which rounds .5 toward +Inf and would still agree here, but we pin the exact
//     expression so a future negative input can't diverge),
//   - calendar arithmetic via UTC-midnight day counts (no DST drift; real tokens are modern dates).
// Keep it in lock-step with the server module if either changes.

// --- partial dates --------------------------------------------------------------------------

export interface PartialDate {
  year: number;
  month: number | null;
  day: number | null;
  hour: number | null;
  minute: number | null;
}

// A civil (calendar) date with no time-of-day — the floor/ceil of a partial's span.
export interface CivilDate {
  year: number;
  month: number; // 1-12
  day: number; // 1-31
}

const PARTIAL_RE = /^(\d{4})(?:-(\d{2})(?:-(\d{2})(?:T(\d{2}):(\d{2}))?)?)?$/;

// One `[[t:…]]` token in a body. Global so we can iterate every occurrence for splitting.
export const TOKEN_RE = /\[\[t:(.*?)\]\]/g;

function daysInMonth(year: number, month: number): number {
  if (month === 2) {
    const leap = (year % 4 === 0 && year % 100 !== 0) || year % 400 === 0;
    return leap ? 29 : 28;
  }
  return [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1]!;
}

// Public alias for the date-token editor's day dropdown.
export function daysInMonthOf(year: number, month: number): number {
  return daysInMonth(year, month);
}

export function granularity(pd: PartialDate): 'year' | 'month' | 'day' | 'minute' {
  if (pd.minute !== null) return 'minute';
  if (pd.day !== null) return 'day';
  if (pd.month !== null) return 'month';
  return 'year';
}

function pad(n: number, width: number): string {
  return String(n).padStart(width, '0');
}

// The partial-ISO string inside a token: `2025` / `2025-07` / `2025-07-07` / `2025-07-07T22:00`.
export function partialIso(pd: PartialDate): string {
  const g = granularity(pd);
  if (g === 'year') return pad(pd.year, 4);
  if (g === 'month') return `${pad(pd.year, 4)}-${pad(pd.month!, 2)}`;
  if (g === 'day') return `${pad(pd.year, 4)}-${pad(pd.month!, 2)}-${pad(pd.day!, 2)}`;
  return `${pad(pd.year, 4)}-${pad(pd.month!, 2)}-${pad(pd.day!, 2)}T${pad(pd.hour!, 2)}:${pad(pd.minute!, 2)}`;
}

// Build a PartialDate, returning null on an impossible date (30 Feb), a skipped granularity level
// (a day without a month), or an out-of-range field — fail-closed, mirroring the server.
export function partialFromFields(
  year: number,
  month: number | null = null,
  day: number | null = null,
  hour: number | null = null,
  minute: number | null = null,
): PartialDate | null {
  if (day !== null && month === null) return null;
  if (month !== null && !(month >= 1 && month <= 12)) return null;
  if ((hour !== null || minute !== null) && day === null) return null;
  if (day !== null && !(day >= 1 && day <= daysInMonth(year, month!))) return null;
  if (hour !== null && !(hour >= 0 && hour <= 23)) return null;
  if (minute !== null && !(minute >= 0 && minute <= 59)) return null;
  // A time-of-day needs both components to serialize; a half-specified time is treated as absent.
  if ((hour === null) !== (minute === null)) {
    hour = null;
    minute = null;
  }
  return { year, month, day, hour, minute };
}

export function parsePartial(s: string): PartialDate | null {
  const m = PARTIAL_RE.exec(s.trim());
  if (!m) return null;
  const [, y, mo, d, h, mi] = m;
  return partialFromFields(
    Number(y),
    mo !== undefined ? Number(mo) : null,
    d !== undefined ? Number(d) : null,
    h !== undefined ? Number(h) : null,
    mi !== undefined ? Number(mi) : null,
  );
}

export function floorCivil(pd: PartialDate): CivilDate {
  return { year: pd.year, month: pd.month ?? 1, day: pd.day ?? 1 };
}

export function ceilCivil(pd: PartialDate): CivilDate {
  if (pd.day !== null) return { year: pd.year, month: pd.month!, day: pd.day };
  if (pd.month !== null)
    return { year: pd.year, month: pd.month, day: daysInMonth(pd.year, pd.month) };
  return { year: pd.year, month: 12, day: 31 };
}

// --- resolved times -------------------------------------------------------------------------

export interface ResolvedTime {
  start: PartialDate;
  end: PartialDate | null;
  label: string | null;
}

export function isRange(rt: ResolvedTime): boolean {
  return rt.end !== null;
}

// Serialize to `[[t:START[/END][|label]]]` — the edit anchor sent back as `old`.
export function serializeToken(rt: ResolvedTime): string {
  let inner = partialIso(rt.start);
  if (rt.end !== null) inner += '/' + partialIso(rt.end);
  if (rt.label) inner += '|' + rt.label;
  return `[[t:${inner}]]`;
}

// The start/end partials as DATE-granular partial-ISO (any time-of-day dropped) for the edit
// endpoint's `start`/`end` fields — `occurred_*` are day-granular (server tokens.py).
export function startDateIso(rt: ResolvedTime): string {
  return partialIso(rt.start).split('T', 1)[0]!;
}
export function endDateIso(rt: ResolvedTime): string | null {
  return rt.end !== null ? partialIso(rt.end).split('T', 1)[0]! : null;
}

export function parseInner(inner: string): ResolvedTime | null {
  const barIdx = inner.indexOf('|');
  const body = barIdx === -1 ? inner : inner.slice(0, barIdx);
  const labelRaw = barIdx === -1 ? '' : inner.slice(barIdx + 1);
  const label = labelRaw.trim() || null;
  const slashIdx = body.indexOf('/');
  const startS = slashIdx === -1 ? body : body.slice(0, slashIdx);
  const endS = slashIdx === -1 ? '' : body.slice(slashIdx + 1);
  const start = parsePartial(startS);
  if (start === null) return null;
  let end: PartialDate | null = null;
  if (slashIdx !== -1) {
    end = parsePartial(endS);
    if (end === null) return null;
  }
  return { start, end, label };
}

export interface TokenMatch {
  // Character span [start, end) of the raw token in the body.
  span: [number, number];
  raw: string;
  resolved: ResolvedTime | null;
}

export function findTokens(body: string): TokenMatch[] {
  const out: TokenMatch[] = [];
  TOKEN_RE.lastIndex = 0;
  let m: RegExpExecArray | null;
  while ((m = TOKEN_RE.exec(body)) !== null) {
    out.push({
      span: [m.index, m.index + m[0].length],
      raw: m[0],
      resolved: parseInner(m[1] ?? ''),
    });
  }
  return out;
}

// --- rendering ------------------------------------------------------------------------------

const MONTH_NAMES = [
  'January',
  'February',
  'March',
  'April',
  'May',
  'June',
  'July',
  'August',
  'September',
  'October',
  'November',
  'December',
];

function absolutePartial(pd: PartialDate): string {
  const g = granularity(pd);
  if (g === 'year') return `${pd.year}`;
  if (g === 'month') return `${MONTH_NAMES[pd.month! - 1]!} ${pd.year}`;
  const day = `${pd.day} ${MONTH_NAMES[pd.month! - 1]!} ${pd.year}`;
  if (g === 'minute') return `${day}, ${pad(pd.hour!, 2)}:${pad(pd.minute!, 2)}`;
  return day;
}

// Absolute, always-unambiguous text: a label wins ("summer 2025"); a range renders both ends.
export function renderAbsolute(rt: ResolvedTime): string {
  if (rt.label) return rt.label;
  if (rt.end !== null) return `${absolutePartial(rt.start)} – ${absolutePartial(rt.end)}`;
  return absolutePartial(rt.start);
}

// Round-half-UP on a non-negative magnitude — pinned to match the server's `_round`.
function roundHalfUp(x: number): number {
  return Math.floor(x + 0.5);
}

// Whole calendar-day count from `a` to `b` (b - a), via UTC midnights (no DST drift).
function dayDelta(from: CivilDate, to: CivilDate): number {
  const a = Date.UTC(from.year, from.month - 1, from.day);
  const b = Date.UTC(to.year, to.month - 1, to.day);
  return Math.round((b - a) / 86400000);
}

function ago(n: number, unit: string, past: boolean): string {
  const quantity = n === 1 ? `a ${unit}` : `${n} ${unit}s`;
  return past ? `${quantity} ago` : `in ${quantity}`;
}

function humanizeDay(target: CivilDate, now: CivilDate): string {
  const d = dayDelta(now, target);
  if (d === 0) return 'today';
  if (d === -1) return 'yesterday';
  if (d === 1) return 'tomorrow';
  const a = Math.abs(d);
  const past = d < 0;
  if (a <= 27) return ago(a, 'day', past);
  if (a < 330) return ago(Math.max(1, roundHalfUp(a / 30)), 'month', past);
  if (a < 400) return ago(1, 'year', past);
  return ago(roundHalfUp(a / 365), 'year', past);
}

function humanizeMonth(pd: PartialDate, now: CivilDate): string {
  const md = pd.year * 12 + pd.month! - (now.year * 12 + now.month);
  if (md === 0) return 'this month';
  if (md === -1) return 'last month';
  if (md === 1) return 'next month';
  const a = Math.abs(md);
  const past = md < 0;
  if (a < 12) return ago(a, 'month', past);
  return ago(roundHalfUp(a / 12), 'year', past);
}

function humanizeYear(year: number, now: CivilDate): string {
  const d = year - now.year;
  if (d === 0) return 'this year';
  if (d === -1) return 'last year';
  if (d === 1) return 'next year';
  return ago(Math.abs(d), 'year', d < 0);
}

// The live display phrase at `now`. Ranges and labelled points render absolute (a season is
// naturally "summer 2025"); day/month/year points humanize per the server spec.
export function renderRelative(rt: ResolvedTime, now: CivilDate): string {
  if (rt.label || isRange(rt)) return renderAbsolute(rt);
  const g = granularity(rt.start);
  if (g === 'day' || g === 'minute') return humanizeDay(floorCivil(rt.start), now);
  if (g === 'month') return humanizeMonth(rt.start, now);
  return humanizeYear(rt.start.year, now);
}

// Today's local calendar date — the `now` the renderers humanize against (matches the server's
// `date.today()` in the user's timezone; the web runs in that timezone).
export function todayCivil(): CivilDate {
  const d = new Date();
  return { year: d.getFullYear(), month: d.getMonth() + 1, day: d.getDate() };
}
