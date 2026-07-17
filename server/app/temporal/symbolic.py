"""Symbolic time-reference schema — what the organizer LLM is allowed to emit (ADR-056 §2,
CLAUDE.md hard rule 12: *"LLMs classify, code computes"*).

The model NEVER emits a computed date. For every temporal phrase it recognizes it emits a
**symbolic classification** — the *kind* of reference plus its linguistic parameters — and the
deterministic :mod:`~app.temporal.resolver` turns that into an absolute date/range against the
capture's stored anchor. This module is the validated contract of those classifications.

Fail-closed (rule 12): :func:`parse_symbolic` returns ``None`` for anything that doesn't validate,
so an ill-formed emission produces no token (the phrase stays prose) rather than a guessed date.

Pure, no I/O — unit-tested with no mocks. Kept deliberately arithmetic-free so the web can mirror
the same schema (ADR-056 §Consequences: heavily unit-testable, zero mocks; web-mirrorable).
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field, TypeAdapter, ValidationError, field_validator

# Canonical vocab the resolver understands. Validators below coerce common LLM variants
# (full names, "fall") to these so the resolver only ever sees canonical forms.
Weekday = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
Season = Literal["winter", "spring", "summer", "autumn"]
RelativeUnit = Literal["day", "week", "month", "year"]
Direction = Literal["last", "this", "next"]

_WEEKDAY_ALIASES = {
    "monday": "mon",
    "tuesday": "tue",
    "wednesday": "wed",
    "thursday": "thu",
    "friday": "fri",
    "saturday": "sat",
    "sunday": "sun",
    "tues": "tue",
    "thur": "thu",
    "thurs": "thu",
}
_SEASON_ALIASES = {"fall": "autumn", "autumn": "autumn"}

# Generous bounds — a real memory never references tens of thousands of units away, but the bound
# keeps a pathological emission from overflowing date arithmetic (it degrades to None instead).
_OFFSET_LIMIT = 100_000
_YEAR_MIN, _YEAR_MAX = 1, 9999


class _RefBase(BaseModel):
    """Common to every reference: the exact source phrase (kept for review payloads / debugging;
    it is never itself resolved)."""

    model_config = {"extra": "forbid"}
    phrase: str = Field(min_length=1)


class ExplicitRef(_RefBase):
    """An explicitly-stated calendar date, whole or partial: "7 July 2026", "March 2024", "the
    22nd" (year/month snapped by code), "2025". ``year`` may be omitted — the resolver snaps it to
    the most recent occurrence at or before the anchor (a memory refers to the past by default).
    Granularity follows which fields are present; ``day``/``month`` may be omitted for coarser
    dates. Time-of-day (``hour``/``minute``) is optional and only meaningful with a ``day``."""

    kind: Literal["explicit"] = "explicit"
    year: int | None = Field(default=None, ge=_YEAR_MIN, le=_YEAR_MAX)
    month: int | None = Field(default=None, ge=1, le=12)
    day: int | None = Field(default=None, ge=1, le=31)
    hour: int | None = Field(default=None, ge=0, le=23)
    minute: int | None = Field(default=None, ge=0, le=59)


class RelativeRef(_RefBase):
    """A count of whole units from the anchor: "10 days ago" (day, -10), "yesterday" (day, -1),
    "today" (day, 0), "in 3 weeks" (week, +3), "last month" (month, -1), "2 years ago"
    (year, -2). Granularity follows the unit (a week resolves to a day-granular point; a month to
    a month; a year to a year). The sign carries direction — negative is past."""

    kind: Literal["relative"] = "relative"
    unit: RelativeUnit
    offset: int = Field(ge=-_OFFSET_LIMIT, le=_OFFSET_LIMIT)


class WeekdayRef(_RefBase):
    """A named weekday relative to the anchor: "last Tuesday" (tue, last), "next Friday"
    (fri, next), "this Wednesday" (wed, this). Resolves day-granular by a weekday walk from the
    anchor; ``last``/``next`` are strictly before/after, ``this`` is within the anchor's week."""

    kind: Literal["weekday"] = "weekday"
    weekday: Weekday
    direction: Direction = "last"

    @field_validator("weekday", mode="before")
    @classmethod
    def _canon_weekday(cls, v: object) -> object:
        if isinstance(v, str):
            low = v.strip().lower()
            return _WEEKDAY_ALIASES.get(low, low)
        return v


class MonthRef(_RefBase):
    """A named month with the year left to code: "last March" (month 3, last), "this December"
    (month 12, this), "next January" (month 1, next). If ``year`` is given it is absolute (prefer
    :class:`ExplicitRef` for that); otherwise the resolver snaps by ``direction`` relative to the
    anchor. Month-granular."""

    kind: Literal["month"] = "month"
    month: int = Field(ge=1, le=12)
    year: int | None = Field(default=None, ge=_YEAR_MIN, le=_YEAR_MAX)
    direction: Direction = "last"


class SeasonRef(_RefBase):
    """A season: "last summer" (summer, year_offset -1), "this winter" (winter, 0), "summer 2025"
    (summer, year=2025). Resolves to a **range** (northern-hemisphere months) with an absolute
    display label ("summer 2025"). ``year`` is absolute; otherwise ``year_offset`` shifts from the
    anchor's calendar year (base = anchor year, so it is trivial for the model to reason about and
    for the web to mirror)."""

    kind: Literal["season"] = "season"
    season: Season
    year: int | None = Field(default=None, ge=_YEAR_MIN, le=_YEAR_MAX)
    year_offset: int = Field(default=0, ge=-_OFFSET_LIMIT, le=_OFFSET_LIMIT)

    @field_validator("season", mode="before")
    @classmethod
    def _canon_season(cls, v: object) -> object:
        if isinstance(v, str):
            return _SEASON_ALIASES.get(v.strip().lower(), v.strip().lower())
        return v


TimeReference = Annotated[
    ExplicitRef | RelativeRef | WeekdayRef | MonthRef | SeasonRef,
    Field(discriminator="kind"),
]

_ADAPTER: TypeAdapter[TimeReference] = TypeAdapter(TimeReference)


def parse_symbolic(data: object) -> TimeReference | None:
    """Validate one raw LLM-emitted symbolic reference into a typed :data:`TimeReference`.

    Fail-closed (rule 12 / ADR-056 §2): returns ``None`` for a missing/unknown ``kind``, an
    out-of-range field, an unknown weekday/season, or any other schema violation — the caller then
    emits no token and the phrase stays prose. Never raises for bad model output."""
    if not isinstance(data, dict):
        return None
    try:
        return _ADAPTER.validate_python(data)
    except ValidationError:
        return None
