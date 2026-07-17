"""Rendering — turning tokens into human/LLM-facing text (ADR-056 §4).

Tokens are **never shown raw**. Three audiences, three renderings, all deterministic:

- **Web / display** — :func:`render_relative` gives the live phrase ("10 days ago", "last month",
  "summer 2025"), always current because it is computed at render against *now*; the exact date
  (:func:`render_absolute`, + time) is the tooltip. The web mirrors this module.
- **Indexing** — :func:`expand_for_index` gives stable **absolute** language (no relative phrase),
  so embeddings see "7 July 2026", not token noise (ADR-056 §4).
- **LLM-bound paths** — :func:`expand_for_llm` gives absolute **plus** a freshly-computed relative
  hint ("7 July 2026 (10 days ago)"), for the grounded-prompt/MCP/capsule contract (ADR-056 §4).

Body-level helpers replace every ``[[t:…]]`` in a string with the chosen rendering.

**Relative humanization spec (mirror this exactly in the web).** For a day-granular point, with
``d = (target - now).days`` and ``a = abs(d)``: 0→"today", ∓1→"yesterday"/"tomorrow", ``a ≤ 27``→
"N days", ``a < 330``→"N months" (N = round(a/30), 1→"a month"), ``a < 400``→"a year",
else→"N years" (round(a/365)). Month-granular points humanize by whole-month delta (0→"this month",
∓1→"last"/"next month", <12→"N months", ≥12→"N years" via round(m/12)). Year-granular by year delta
(0→"this year", ∓1→"last"/"next year", else "N years"). Ranges and labelled points render
absolute (a season is natural as "summer 2025", not a relative phrase). Past uses "N … ago",
future "in N …"; N==1 uses the article ("a month ago", "in a year").

All rounding is **round-half-up** on the (non-negative) magnitude via :func:`_round` — *not*
Python's default round-half-to-even — so a web mirror using ``Math.round`` produces byte-identical
phrases at the ``.5`` boundaries (e.g. 75 days → "3 months", 30 months → "3 years").
"""

from __future__ import annotations

import math
from datetime import date

from .tokens import TOKEN_RE, PartialDate, ResolvedTime, parse_inner

_MONTH_NAMES = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


def _absolute_partial(pd: PartialDate) -> str:
    """One partial as absolute language: "2025" / "July 2025" / "7 July 2026" / "7 July 2026,
    22:00"."""
    g = pd.granularity
    if g == "year":
        return f"{pd.year}"
    if g == "month":
        return f"{_MONTH_NAMES[pd.month - 1]} {pd.year}"
    day = f"{pd.day} {_MONTH_NAMES[pd.month - 1]} {pd.year}"
    if g == "minute":
        return f"{day}, {pd.hour:02d}:{pd.minute:02d}"
    return day


def render_absolute(rt: ResolvedTime) -> str:
    """Absolute, always-unambiguous text. A label wins (e.g. "summer 2025"); a range renders both
    ends; otherwise the start partial."""
    if rt.label:
        return rt.label
    if rt.end is not None:
        return f"{_absolute_partial(rt.start)} – {_absolute_partial(rt.end)}"
    return _absolute_partial(rt.start)


def _round(x: float) -> int:
    """Round-half-**up** on a non-negative magnitude (``math.floor(x + 0.5)``), matching JS
    ``Math.round`` so the web mirror agrees at ``.5`` ties — not Python's default half-to-even."""
    return math.floor(x + 0.5)


def _ago(n: int, unit: str, past: bool) -> str:
    """ "a month ago" / "3 months ago" / "in a year" / "in 5 days"."""
    quantity = f"a {unit}" if n == 1 else f"{n} {unit}s"
    return f"{quantity} ago" if past else f"in {quantity}"


def _humanize_day(target: date, now: date) -> str:
    d = (target - now).days
    if d == 0:
        return "today"
    if d == -1:
        return "yesterday"
    if d == 1:
        return "tomorrow"
    a = abs(d)
    past = d < 0
    if a <= 27:
        return _ago(a, "day", past)
    if a < 330:
        return _ago(max(1, _round(a / 30)), "month", past)
    if a < 400:
        return _ago(1, "year", past)
    return _ago(_round(a / 365), "year", past)


def _humanize_month(pd: PartialDate, now: date) -> str:
    md = (pd.year * 12 + pd.month) - (now.year * 12 + now.month)
    if md == 0:
        return "this month"
    if md == -1:
        return "last month"
    if md == 1:
        return "next month"
    a = abs(md)
    past = md < 0
    if a < 12:
        return _ago(a, "month", past)
    return _ago(_round(a / 12), "year", past)


def _humanize_year(year: int, now: date) -> str:
    d = year - now.year
    if d == 0:
        return "this year"
    if d == -1:
        return "last year"
    if d == 1:
        return "next year"
    return _ago(abs(d), "year", d < 0)


def render_relative(rt: ResolvedTime, now: date) -> str:
    """The live display phrase at ``now``. Ranges and labelled points render absolute (a season is
    naturally "summer 2025"); day/month/year points humanize per the module spec."""
    if rt.label or rt.is_range:
        return render_absolute(rt)
    g = rt.start.granularity
    if g in ("day", "minute"):
        return _humanize_day(rt.start.floor_date(), now)
    if g == "month":
        return _humanize_month(rt.start, now)
    return _humanize_year(rt.start.year, now)


def expand_for_index(rt: ResolvedTime) -> str:
    """Absolute text only — what the indexer substitutes before chunking/embedding (ADR-056 §4).
    No relative phrase: vectors must see stable language."""
    return render_absolute(rt)


def expand_for_llm(rt: ResolvedTime, now: date) -> str:
    """Absolute text plus a freshly-computed relative hint for a point ("7 July 2026 (10 days
    ago)"), per the LLM-bound rendering contract (ADR-056 §4). Ranges/labels render absolute
    only (no natural relative form)."""
    absolute = render_absolute(rt)
    if rt.label or rt.is_range:
        return absolute
    return f"{absolute} ({render_relative(rt, now)})"


def _replace_tokens(body: str, fn) -> str:
    """Replace every ``[[t:…]]`` in ``body`` using ``fn(resolved, inner)``. A malformed token
    (unparseable inner) degrades to its label if present, else its raw inner text — never shown as
    raw brackets (ADR-056 §4)."""
    out: list[str] = []
    last = 0
    for m in TOKEN_RE.finditer(body):
        out.append(body[last : m.start()])
        inner = m.group(1)
        rt = parse_inner(inner)
        if rt is None:
            _, _, label = inner.partition("|")
            out.append(label.strip() or inner)
        else:
            out.append(fn(rt, inner))
        last = m.end()
    out.append(body[last:])
    return "".join(out)


def render_body(body: str, now: date) -> str:
    """Display rendering of a whole body: every token → its live relative phrase."""
    return _replace_tokens(body, lambda rt, _inner: render_relative(rt, now))


def expand_body_for_index(body: str) -> str:
    """Indexer rendering of a body: every token → absolute text (pre-embedding, ADR-056 §4)."""
    return _replace_tokens(body, lambda rt, _inner: expand_for_index(rt))


def expand_body_for_llm(body: str, now: date) -> str:
    """LLM-bound rendering of a whole body: every token → absolute + relative hint (ADR-056 §4)."""
    return _replace_tokens(body, lambda rt, _inner: expand_for_llm(rt, now))
