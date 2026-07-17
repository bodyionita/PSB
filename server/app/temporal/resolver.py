"""The deterministic resolver (ADR-056 §2, CLAUDE.md rule 12).

Given a symbolic :data:`~app.temporal.symbolic.TimeReference` (what the LLM classified) and the
capture's **stored anchor** (never wall-clock — reprocess-determinism, ADR-056 §1), this computes
the absolute :class:`~app.temporal.tokens.ResolvedTime`. All date math lives here: offset
arithmetic, weekday walks, month/year snapping, and northern-hemisphere season windows.

Fail-closed (rule 12): any reference that cannot be resolved to a real date returns ``None`` — the
caller emits no token and the phrase stays prose. Nothing is ever guessed.

Pure stdlib arithmetic (``datetime``/``calendar`` + integer month math), no ``dateutil`` — so the
web mirror computes byte-identical results (ADR-056 §Consequences: web-mirrorable spec).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from .symbolic import (
    ExplicitRef,
    MonthRef,
    RelativeRef,
    SeasonRef,
    TimeReference,
    WeekdayRef,
    parse_symbolic,
)
from .tokens import PartialDate, ResolvedTime

_WEEKDAY_INDEX = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}

# Northern-hemisphere season windows as (start_month, end_month, spans_year_boundary). A season's
# "year" labels its start (ADR-056 §2 default). Winter runs Dec (year Y) → Feb (year Y+1).
_SEASONS = {
    "spring": (3, 5, False),
    "summer": (6, 8, False),
    "autumn": (9, 11, False),
    "winter": (12, 2, True),
}


def _add_months(year: int, month: int, delta: int) -> tuple[int, int]:
    """Shift a (year, 1-based month) by ``delta`` months. Integer arithmetic (web-mirrorable)."""
    total = year * 12 + (month - 1) + delta
    return total // 12, total % 12 + 1


def _anchor_date(anchor: datetime | date) -> date:
    return anchor.date() if isinstance(anchor, datetime) else anchor


def resolve(ref: TimeReference, anchor: datetime | date) -> ResolvedTime | None:
    """Resolve one typed symbolic reference against the anchor. ``None`` if it can't be computed to
    a real date (fail-closed). Dispatches by concrete type."""
    if isinstance(ref, ExplicitRef):
        return _resolve_explicit(ref, anchor)
    if isinstance(ref, RelativeRef):
        return _resolve_relative(ref, anchor)
    if isinstance(ref, WeekdayRef):
        return _resolve_weekday(ref, anchor)
    if isinstance(ref, MonthRef):
        return _resolve_month(ref, anchor)
    if isinstance(ref, SeasonRef):
        return _resolve_season(ref, anchor)
    return None


def resolve_reference(data: object, anchor: datetime | date) -> ResolvedTime | None:
    """Parse a raw LLM emission and resolve it in one step. ``None`` if it fails to validate
    (:func:`~app.temporal.symbolic.parse_symbolic`) or to resolve. The convenience entry point the
    organizer path calls per emitted reference."""
    ref = parse_symbolic(data)
    if ref is None:
        return None
    return resolve(ref, anchor)


def _resolve_explicit(ref: ExplicitRef, anchor: datetime | date) -> ResolvedTime | None:
    year = ref.year
    if year is None:
        # No year stated → snap to the most recent occurrence at or before the anchor (a memory
        # refers to the past by default). Snapping needs at least a month.
        if ref.month is None:
            return None
        y = _anchor_date(anchor).year
        candidate = PartialDate.from_fields(y, ref.month, ref.day, ref.hour, ref.minute)
        if candidate is None:
            # e.g. 29 Feb of a non-leap anchor year — step back a year and retry once.
            candidate = PartialDate.from_fields(y - 1, ref.month, ref.day, ref.hour, ref.minute)
            if candidate is None:
                return None
            return ResolvedTime(start=candidate)
        if candidate.floor_date() > _anchor_date(anchor):
            stepped = PartialDate.from_fields(y - 1, ref.month, ref.day, ref.hour, ref.minute)
            candidate = stepped or candidate
        return ResolvedTime(start=candidate)
    pd = PartialDate.from_fields(year, ref.month, ref.day, ref.hour, ref.minute)
    return ResolvedTime(start=pd) if pd is not None else None


def _resolve_relative(ref: RelativeRef, anchor: datetime | date) -> ResolvedTime | None:
    a = _anchor_date(anchor)
    if ref.unit == "day":
        target = a + timedelta(days=ref.offset)
        return ResolvedTime(start=PartialDate(target.year, target.month, target.day))
    if ref.unit == "week":
        target = a + timedelta(weeks=ref.offset)
        return ResolvedTime(start=PartialDate(target.year, target.month, target.day))
    if ref.unit == "month":
        y, m = _add_months(a.year, a.month, ref.offset)
        return ResolvedTime(start=PartialDate(y, m))
    if ref.unit == "year":
        y = a.year + ref.offset
        if not (1 <= y <= 9999):
            return None
        return ResolvedTime(start=PartialDate(y))
    return None


def _resolve_weekday(ref: WeekdayRef, anchor: datetime | date) -> ResolvedTime | None:
    a = _anchor_date(anchor)
    target_wd = _WEEKDAY_INDEX[ref.weekday]
    anchor_wd = a.weekday()
    if ref.direction == "last":
        delta = (anchor_wd - target_wd) % 7 or 7  # strictly before
        target = a - timedelta(days=delta)
    elif ref.direction == "next":
        delta = (target_wd - anchor_wd) % 7 or 7  # strictly after
        target = a + timedelta(days=delta)
    else:  # this — the weekday within the anchor's Mon–Sun week
        week_start = a - timedelta(days=anchor_wd)
        target = week_start + timedelta(days=target_wd)
    return ResolvedTime(start=PartialDate(target.year, target.month, target.day))


def _resolve_month(ref: MonthRef, anchor: datetime | date) -> ResolvedTime | None:
    a = _anchor_date(anchor)
    if ref.year is not None:
        y = ref.year
    elif ref.direction == "this":
        y = a.year
    elif ref.direction == "last":
        # Most recent occurrence at or before the anchor month.
        y = a.year if ref.month <= a.month else a.year - 1
    else:  # next — the soonest occurrence strictly after the anchor month
        y = a.year if ref.month > a.month else a.year + 1
    if not (1 <= y <= 9999):
        return None
    return ResolvedTime(start=PartialDate(y, ref.month))


def _resolve_season(ref: SeasonRef, anchor: datetime | date) -> ResolvedTime | None:
    a = _anchor_date(anchor)
    year = ref.year if ref.year is not None else a.year + ref.year_offset
    if not (1 <= year <= 9999):
        return None
    start_month, end_month, spans = _SEASONS[ref.season]
    end_year = year + 1 if spans else year
    if not (1 <= end_year <= 9999):
        return None
    return ResolvedTime(
        start=PartialDate(year, start_month),
        end=PartialDate(end_year, end_month),
        label=f"{ref.season} {year}",
    )
