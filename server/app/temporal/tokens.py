"""Inline date tokens and partial-ISO dates (ADR-056 §3).

The resolver's output lands in derived bodies as a machine-readable token —
**``[[t:START[/END][|label]]]``** — parsed here into a :class:`ResolvedTime`. ``START``/``END`` are
**partial-ISO** with honest granularity (``2025`` / ``2025-07`` / ``2025-07-07`` /
``2025-07-07T22:00``); ``/END`` makes it a range; ``|label`` is an optional absolute display label
(e.g. ``summer 2025``). Tokens are never shown raw (ADR-056 §4) — :mod:`~app.temporal.render`
turns them into live phrases, the indexer/LLM paths expand them to absolute text.

This module is pure string/date logic: parse, serialize, locate tokens in a body, and derive the
day-granular ``occurred`` floor/ceil the DB stores (``occurred_*`` stay ``date`` — tokens own
sub-day precision, ADR-056 §6). All arithmetic is stdlib so the web can mirror it exactly.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date

# Granularity levels a PartialDate can carry, coarsest to finest. Kept as plain strings (returned
# by PartialDate.granularity) for lightweight web mirroring rather than an enum.
GRANULARITIES = ("year", "month", "day", "minute")

# A partial-ISO date: YYYY, YYYY-MM, YYYY-MM-DD, or YYYY-MM-DDThh:mm. Anchored, no timezone
# (the token is display/narrative; DB day-granularity is derived below).
_PARTIAL_RE = re.compile(r"^(\d{4})(?:-(\d{2})(?:-(\d{2})(?:T(\d{2}):(\d{2}))?)?)?$")

# One token in a body. Non-greedy inner so adjacent tokens don't merge; the inner is validated
# separately (a structurally-present but malformed token degrades gracefully in render).
TOKEN_RE = re.compile(r"\[\[t:(.*?)\]\]")


@dataclass(frozen=True)
class PartialDate:
    """A calendar date at honest granularity — coarser fields present, finer omitted. ``month``
    requires nothing finer to be set without it; validity of the day (e.g. not 30 Feb) is checked
    at construction via :meth:`parse` / :meth:`from_fields`."""

    year: int
    month: int | None = None
    day: int | None = None
    hour: int | None = None
    minute: int | None = None

    @property
    def granularity(self) -> str:
        if self.minute is not None:
            return "minute"
        if self.day is not None:
            return "day"
        if self.month is not None:
            return "month"
        return "year"

    def iso(self) -> str:
        """The partial-ISO string used inside a token: ``2025`` / ``2025-07`` / ``2025-07-07`` /
        ``2025-07-07T22:00``."""
        g = self.granularity
        if g == "year":
            return f"{self.year:04d}"
        if g == "month":
            return f"{self.year:04d}-{self.month:02d}"
        if g == "day":
            return f"{self.year:04d}-{self.month:02d}-{self.day:02d}"
        return f"{self.year:04d}-{self.month:02d}-{self.day:02d}T{self.hour:02d}:{self.minute:02d}"

    def floor_date(self) -> date:
        """The first calendar day of this partial's span (month/day default to 1)."""
        return date(self.year, self.month or 1, self.day or 1)

    def ceil_date(self) -> date:
        """The last calendar day of this partial's span: a bare year → Dec 31, a bare month → its
        last day, a day/minute → that day."""
        if self.day is not None:
            return date(self.year, self.month, self.day)
        if self.month is not None:
            return date(self.year, self.month, calendar.monthrange(self.year, self.month)[1])
        return date(self.year, 12, 31)

    @classmethod
    def from_fields(
        cls,
        year: int,
        month: int | None = None,
        day: int | None = None,
        hour: int | None = None,
        minute: int | None = None,
    ) -> PartialDate | None:
        """Build a :class:`PartialDate`, returning ``None`` if the fields don't form a real date
        (e.g. 30 February) or skip a granularity level (a day without a month). Fail-closed so
        callers never store an impossible date."""
        if day is not None and month is None:
            return None
        if month is not None and not (1 <= month <= 12):
            return None
        if (hour is not None or minute is not None) and day is None:
            return None
        if day is not None:
            try:
                date(year, month, day)
            except ValueError:
                return None
        if hour is not None and not (0 <= hour <= 23):
            return None
        if minute is not None and not (0 <= minute <= 59):
            return None
        # A time-of-day needs both components to serialize; treat a half-specified time as absent.
        if (hour is None) != (minute is None):
            hour = minute = None
        return cls(year=year, month=month, day=day, hour=hour, minute=minute)

    @classmethod
    def parse(cls, s: str) -> PartialDate | None:
        """Parse a partial-ISO string. ``None`` on any malformed or impossible value."""
        m = _PARTIAL_RE.match(s.strip())
        if not m:
            return None
        y, mo, d, h, mi = m.groups()
        return cls.from_fields(
            int(y),
            int(mo) if mo is not None else None,
            int(d) if d is not None else None,
            int(h) if h is not None else None,
            int(mi) if mi is not None else None,
        )


@dataclass(frozen=True)
class ResolvedTime:
    """A resolved temporal reference: a start partial, an optional end partial (making it a range),
    and an optional absolute display label. This is the output of the resolver and the parsed form
    of a token — everything the renderers and the DB-facing ``occurred`` mapping need."""

    start: PartialDate
    end: PartialDate | None = None
    label: str | None = None

    def token(self) -> str:
        """Serialize to ``[[t:START[/END][|label]]]``."""
        inner = self.start.iso()
        if self.end is not None:
            inner += "/" + self.end.iso()
        if self.label:
            inner += "|" + self.label
        return f"[[t:{inner}]]"

    @property
    def is_range(self) -> bool:
        return self.end is not None

    def start_date_iso(self) -> str:
        """The start partial as a **date-granular** partial-ISO string for the frontmatter
        ``occurred`` field (``2025`` / ``2025-07`` / ``2025-07-07``) — any time-of-day dropped
        (``occurred_*`` are day-granular; tokens own sub-day, ADR-056 §6). Granularity is preserved
        (a bare year stays ``2025``), so the indexer re-expands it to the same day range."""
        return self.start.iso().split("T", 1)[0]

    def end_date_iso(self) -> str | None:
        """The end partial as a date-granular partial-ISO for the frontmatter ``occurred_end``
        field, or ``None`` when this is not a range (a coarse single partial's span is implicit in
        its own granularity, so it needs no explicit end — mirrors the organizer's emission)."""
        return self.end.iso().split("T", 1)[0] if self.end is not None else None

    def occurred_start(self) -> date:
        """The day-granular ``occurred_start`` for the DB (floor of the start partial)."""
        return self.start.floor_date()

    def occurred_end(self) -> date | None:
        """The day-granular ``occurred_end`` for the DB, or ``None`` for a precise single point.

        A range → the ceil of its end. A coarse single partial (bare year/month) → the ceil of its
        own span, so "in July 2026" honestly spans Jul 1–31 (this is what the ADR-049 dedup
        occurred-overlap gate reads). A day/minute-precise single partial is a point → ``None``."""
        if self.end is not None:
            return self.end.ceil_date()
        if self.start.granularity in ("year", "month"):
            return self.start.ceil_date()
        return None


@dataclass(frozen=True)
class TokenMatch:
    """One ``[[t:…]]`` occurrence located in a body: its character ``span``, the raw matched text,
    and the parsed :class:`ResolvedTime` (``None`` if the token was malformed)."""

    span: tuple[int, int]
    raw: str
    resolved: ResolvedTime | None


def parse_inner(inner: str) -> ResolvedTime | None:
    """Parse a token's inner text (the part between ``[[t:`` and ``]]``) into a
    :class:`ResolvedTime`. ``None`` if the start (or a present end) is not valid partial-ISO."""
    body, _, label = inner.partition("|")
    label = label.strip() or None
    start_s, sep, end_s = body.partition("/")
    start = PartialDate.parse(start_s)
    if start is None:
        return None
    end: PartialDate | None = None
    if sep:  # a "/" was present → a range; the end must parse
        end = PartialDate.parse(end_s)
        if end is None:
            return None
    return ResolvedTime(start=start, end=end, label=label)


def find_tokens(body: str) -> list[TokenMatch]:
    """Locate every ``[[t:…]]`` token in ``body`` (in order), parsing each. Malformed tokens are
    returned with ``resolved=None`` so callers can decide how to degrade them."""
    return [
        TokenMatch(span=m.span(), raw=m.group(0), resolved=parse_inner(m.group(1)))
        for m in TOKEN_RE.finditer(body)
    ]


def make_token(start: PartialDate, end: PartialDate | None = None, label: str | None = None) -> str:
    """Convenience serializer mirroring :meth:`ResolvedTime.token`."""
    return ResolvedTime(start=start, end=end, label=label).token()
