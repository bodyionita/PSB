"""Unit tests for the temporal engine (M8.2 T1 · ADR-056). Pure logic, zero mocks — the whole
point of "LLMs classify, code computes" is that this layer is exhaustively checkable in isolation.

Covers: symbolic-schema validation (fail-closed), deterministic resolution against a fixed anchor
(offsets, weekday walks, month/year snapping, season windows), token parse/serialize round-trips
+ the day-granular ``occurred`` mapping, and rendering (absolute / live-relative / index / LLM),
including the two ADR-056 Accept scenarios.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from app.temporal import (
    ExplicitRef,
    MonthRef,
    PartialDate,
    RelativeRef,
    ResolvedTime,
    SeasonRef,
    WeekdayRef,
    expand_body_for_index,
    expand_body_for_llm,
    expand_for_index,
    expand_for_llm,
    find_tokens,
    format_occurred,
    make_token,
    parse_inner,
    parse_symbolic,
    render_absolute,
    render_body,
    render_relative,
    resolve,
    resolve_reference,
    temporal_header,
)

# A fixed anchor for every anchored test: Friday, 17 July 2026 (its Mon–Sun week is 13–19 July).
ANCHOR = datetime(2026, 7, 17, 8, 40)


# --------------------------------------------------------------------------- symbolic schema


def test_parse_symbolic_dispatches_each_kind():
    assert isinstance(
        parse_symbolic({"kind": "relative", "unit": "day", "offset": -10, "phrase": "10 days ago"}),
        RelativeRef,
    )
    assert isinstance(
        parse_symbolic({"kind": "explicit", "year": 2025, "phrase": "2025"}), ExplicitRef
    )
    assert isinstance(
        parse_symbolic(
            {"kind": "weekday", "weekday": "tue", "direction": "last", "phrase": "last Tuesday"}
        ),
        WeekdayRef,
    )
    assert isinstance(
        parse_symbolic({"kind": "month", "month": 3, "phrase": "last March"}), MonthRef
    )
    assert isinstance(
        parse_symbolic(
            {"kind": "season", "season": "summer", "year_offset": -1, "phrase": "last summer"}
        ),
        SeasonRef,
    )


def test_parse_symbolic_fails_closed():
    assert parse_symbolic(None) is None
    assert parse_symbolic("10 days ago") is None
    assert parse_symbolic({}) is None
    assert parse_symbolic({"kind": "nope", "phrase": "x"}) is None
    # out-of-range / missing fields
    assert parse_symbolic({"kind": "month", "month": 13, "phrase": "x"}) is None
    assert (
        parse_symbolic({"kind": "explicit", "month": 2, "phrase": "x", "year": 2020, "hour": 24})
        is None
    )
    # phrase is required and non-empty
    assert parse_symbolic({"kind": "relative", "unit": "day", "offset": 0}) is None
    assert parse_symbolic({"kind": "relative", "unit": "day", "offset": 0, "phrase": ""}) is None
    # unknown extra field is rejected (extra=forbid guards against silent drift)
    assert (
        parse_symbolic(
            {"kind": "relative", "unit": "day", "offset": 0, "phrase": "today", "bogus": 1}
        )
        is None
    )


def test_parse_symbolic_coerces_variants():
    wd = parse_symbolic(
        {"kind": "weekday", "weekday": "Tuesday", "direction": "last", "phrase": "last Tuesday"}
    )
    assert isinstance(wd, WeekdayRef) and wd.weekday == "tue"
    se = parse_symbolic({"kind": "season", "season": "Fall", "phrase": "last fall"})
    assert isinstance(se, SeasonRef) and se.season == "autumn"


# --------------------------------------------------------------------------- resolver: relative


@pytest.mark.parametrize(
    "offset,expected",
    [
        (-10, date(2026, 7, 7)),
        (-1, date(2026, 7, 16)),
        (0, date(2026, 7, 17)),
        (1, date(2026, 7, 18)),
    ],
)
def test_relative_days(offset, expected):
    rt = resolve(RelativeRef(unit="day", offset=offset, phrase="x"), ANCHOR)
    assert rt is not None and rt.start == PartialDate(expected.year, expected.month, expected.day)
    assert rt.occurred_start() == expected
    assert rt.occurred_end() is None  # a precise day is a point, not a span


def test_relative_weeks_is_day_granular():
    rt = resolve(RelativeRef(unit="week", offset=-2, phrase="2 weeks ago"), ANCHOR)
    assert rt is not None and rt.start == PartialDate(2026, 7, 3)


def test_relative_month_and_year_granularity():
    rt = resolve(RelativeRef(unit="month", offset=-1, phrase="last month"), ANCHOR)
    assert rt is not None and rt.start == PartialDate(2026, 6) and rt.start.granularity == "month"
    assert rt.occurred_start() == date(2026, 6, 1) and rt.occurred_end() == date(2026, 6, 30)

    ry = resolve(RelativeRef(unit="year", offset=-2, phrase="2 years ago"), ANCHOR)
    assert ry is not None and ry.start == PartialDate(2024) and ry.start.granularity == "year"
    assert ry.occurred_start() == date(2024, 1, 1) and ry.occurred_end() == date(2024, 12, 31)


def test_relative_month_wraps_year():
    rt = resolve(RelativeRef(unit="month", offset=-9, phrase="9 months ago"), ANCHOR)
    assert rt is not None and rt.start == PartialDate(2025, 10)


def test_relative_year_out_of_range_is_none():
    assert resolve(RelativeRef(unit="year", offset=-3000, phrase="x"), ANCHOR) is None


# --------------------------------------------------------------------------- resolver: weekday


def test_weekday_last_strictly_before():
    # Anchor is Friday 2026-07-17. "last Tuesday" → 2026-07-14.
    rt = resolve(WeekdayRef(weekday="tue", direction="last", phrase="last Tuesday"), ANCHOR)
    assert rt is not None and rt.start.floor_date() == date(2026, 7, 14)


def test_weekday_last_same_weekday_goes_back_a_full_week():
    # "last Friday" on a Friday anchor is the previous Friday (strictly before), not the anchor.
    rt = resolve(WeekdayRef(weekday="fri", direction="last", phrase="last Friday"), ANCHOR)
    assert rt is not None and rt.start.floor_date() == date(2026, 7, 10)


def test_weekday_next_strictly_after():
    # "next Thursday" on a Friday anchor → the Thursday of next week, 2026-07-23.
    rt = resolve(WeekdayRef(weekday="thu", direction="next", phrase="next Thursday"), ANCHOR)
    assert rt is not None and rt.start.floor_date() == date(2026, 7, 23)


def test_weekday_this_within_anchor_week():
    # Anchor week is Mon 2026-07-13 .. Sun 2026-07-19.
    rt = resolve(WeekdayRef(weekday="mon", direction="this", phrase="this Monday"), ANCHOR)
    assert rt is not None and rt.start.floor_date() == date(2026, 7, 13)


# --- resolver: month/explicit


def test_month_snapping_last_this_next():
    # last March: 3 <= 7 → this year
    assert resolve(
        MonthRef(month=3, direction="last", phrase="last March"), ANCHOR
    ).start == PartialDate(2026, 3)
    # last December: 12 > 7 → previous year
    assert resolve(
        MonthRef(month=12, direction="last", phrase="last December"), ANCHOR
    ).start == PartialDate(2025, 12)
    # next January: 1 <= 7 → next year
    assert resolve(
        MonthRef(month=1, direction="next", phrase="next January"), ANCHOR
    ).start == PartialDate(2027, 1)
    # explicit year overrides direction
    assert resolve(MonthRef(month=3, year=2024, phrase="March 2024"), ANCHOR).start == PartialDate(
        2024, 3
    )


def test_explicit_full_and_partial():
    assert resolve(ExplicitRef(year=2025, phrase="2025"), ANCHOR).start == PartialDate(2025)
    assert resolve(
        ExplicitRef(year=2026, month=7, day=7, phrase="7 Jul"), ANCHOR
    ).start == PartialDate(2026, 7, 7)
    # with time-of-day
    rt = resolve(ExplicitRef(year=2026, month=7, day=7, hour=22, minute=0, phrase="10pm"), ANCHOR)
    assert rt.start == PartialDate(2026, 7, 7, 22, 0) and rt.start.granularity == "minute"


def test_explicit_yearless_snaps_to_most_recent_past():
    # "December 25th" with a July anchor → the previous Christmas (2025), not this year's future.
    rt = resolve(ExplicitRef(month=12, day=25, phrase="Dec 25"), ANCHOR)
    assert rt is not None and rt.start == PartialDate(2025, 12, 25)
    # "March 3rd" (already passed this year) → this year.
    rt2 = resolve(ExplicitRef(month=3, day=3, phrase="Mar 3"), ANCHOR)
    assert rt2 is not None and rt2.start == PartialDate(2026, 3, 3)


def test_explicit_yearless_without_month_is_none():
    assert resolve(ExplicitRef(day=5, phrase="the 5th"), ANCHOR) is None


def test_explicit_impossible_date_is_none():
    assert resolve(ExplicitRef(year=2021, month=2, day=30, phrase="30 Feb"), ANCHOR) is None


def test_explicit_yearless_feb29_snaps_to_previous_leap_year_not_future():
    # Anchor 2024-01-15 is in a leap year, but 29 Feb 2024 is *after* the anchor — snapping must
    # walk back to the previous real leap-year occurrence (2020), never keep the future date.
    rt = resolve(ExplicitRef(month=2, day=29, phrase="Feb 29"), datetime(2024, 1, 15))
    assert rt is not None and rt.start == PartialDate(2020, 2, 29)
    assert rt.occurred_start() <= date(2024, 1, 15)  # never in the future


def test_explicit_yearless_impossible_day_finds_no_past_and_is_none():
    # 31 April never exists → the back-walk exhausts → None (fail-closed, never guessed).
    assert resolve(ExplicitRef(month=4, day=31, phrase="April 31"), ANCHOR) is None


# --------------------------------------------------------------------------- resolver: season


def test_season_last_summer_is_a_labelled_range():
    rt = resolve(SeasonRef(season="summer", year_offset=-1, phrase="last summer"), ANCHOR)
    assert rt is not None
    assert rt.token() == "[[t:2025-06/2025-08|summer 2025]]"
    assert rt.occurred_start() == date(2025, 6, 1) and rt.occurred_end() == date(2025, 8, 31)


def test_season_winter_spans_the_year_boundary():
    rt = resolve(SeasonRef(season="winter", year=2025, phrase="winter 2025"), ANCHOR)
    assert rt is not None
    assert rt.token() == "[[t:2025-12/2026-02|winter 2025]]"
    assert rt.occurred_start() == date(2025, 12, 1) and rt.occurred_end() == date(2026, 2, 28)


def test_season_this_uses_anchor_year():
    rt = resolve(SeasonRef(season="summer", phrase="this summer"), ANCHOR)
    assert rt is not None and rt.label == "summer 2026"


# --- resolve_reference (parse+resolve)


def test_resolve_reference_end_to_end_and_failclosed():
    rt = resolve_reference(
        {"kind": "relative", "unit": "day", "offset": -10, "phrase": "10 days ago"}, ANCHOR
    )
    assert rt is not None and rt.occurred_start() == date(2026, 7, 7)
    assert resolve_reference({"kind": "bogus"}, ANCHOR) is None
    assert (
        resolve_reference(
            {"kind": "explicit", "month": 2, "day": 30, "year": 2021, "phrase": "x"}, ANCHOR
        )
        is None
    )


def test_resolve_accepts_a_plain_date_anchor():
    rt = resolve(RelativeRef(unit="day", offset=-1, phrase="yesterday"), date(2026, 7, 17))
    assert rt is not None and rt.start == PartialDate(2026, 7, 16)


# --------------------------------------------------------------------------- tokens: partial ISO


@pytest.mark.parametrize(
    "iso,gran",
    [("2025", "year"), ("2025-07", "month"), ("2025-07-07", "day"), ("2025-07-07T22:00", "minute")],
)
def test_partial_date_iso_round_trip(iso, gran):
    pd = PartialDate.parse(iso)
    assert pd is not None and pd.granularity == gran and pd.iso() == iso


def test_partial_date_floor_ceil():
    assert PartialDate.parse("2025").floor_date() == date(2025, 1, 1)
    assert PartialDate.parse("2025").ceil_date() == date(2025, 12, 31)
    assert PartialDate.parse("2024-02").ceil_date() == date(2024, 2, 29)  # leap
    assert PartialDate.parse("2025-02").ceil_date() == date(2025, 2, 28)
    assert PartialDate.parse("2025-07-07").ceil_date() == date(2025, 7, 7)


def test_partial_date_parse_rejects_garbage():
    for bad in [
        "",
        "not-a-date",
        "2025-13",
        "2025-02-30",
        "20250707",
        "2025-7-7",
        "2025-07-07T99:00",
    ]:
        assert PartialDate.parse(bad) is None, bad


def test_partial_date_from_fields_guards():
    assert PartialDate.from_fields(2025, day=5) is None  # day without month
    assert PartialDate.from_fields(2025, 2, 30) is None  # impossible
    # a half-specified time is dropped rather than stored malformed
    pd = PartialDate.from_fields(2025, 7, 7, hour=10)
    assert pd is not None and pd.hour is None and pd.granularity == "day"


# --- tokens: serialize/parse/locate


def test_token_serialize_variants():
    assert make_token(PartialDate(2025)) == "[[t:2025]]"
    assert make_token(PartialDate(2026, 7)) == "[[t:2026-07]]"
    assert make_token(PartialDate(2026, 7, 7, 22, 0)) == "[[t:2026-07-07T22:00]]"
    assert (
        make_token(PartialDate(2025, 6), PartialDate(2025, 8), "summer 2025")
        == "[[t:2025-06/2025-08|summer 2025]]"
    )


def test_parse_inner_round_trips_and_fails_closed():
    for tok in ["2025", "2026-07", "2025-06/2025-08|summer 2025", "2026-07-07T22:00"]:
        rt = parse_inner(tok)
        assert rt is not None and rt.token() == f"[[t:{tok}]]"
    assert parse_inner("garbage") is None
    assert parse_inner("2025/garbage") is None  # bad range end
    assert parse_inner("2025-13") is None


def test_parse_inner_mixed_granularity_range_round_trips():
    # A range whose ends differ in granularity is valid and preserved verbatim.
    rt = parse_inner("2025/2025-08")
    assert rt is not None and rt.is_range
    assert rt.start == PartialDate(2025) and rt.end == PartialDate(2025, 8)
    assert rt.token() == "[[t:2025/2025-08]]"
    assert rt.occurred_start() == date(2025, 1, 1) and rt.occurred_end() == date(2025, 8, 31)


def test_find_tokens_locates_all_in_order():
    body = "Met on [[t:2026-07-07]] and again [[t:2025-06/2025-08|summer 2025]], plus [[t:bad]]."
    matches = find_tokens(body)
    assert len(matches) == 3
    assert matches[0].resolved.start == PartialDate(2026, 7, 7)
    assert matches[1].resolved.label == "summer 2025"
    assert matches[2].resolved is None  # malformed, surfaced not crashed
    # spans point at the exact token text
    s, e = matches[0].span
    assert body[s:e] == "[[t:2026-07-07]]"


# --------------------------------------------------------------------------- render: absolute


def test_render_absolute_by_granularity():
    assert render_absolute(ResolvedTime(PartialDate(2025))) == "2025"
    assert render_absolute(ResolvedTime(PartialDate(2025, 7))) == "July 2025"
    assert render_absolute(ResolvedTime(PartialDate(2026, 7, 7))) == "7 July 2026"
    assert render_absolute(ResolvedTime(PartialDate(2026, 7, 7, 22, 0))) == "7 July 2026, 22:00"
    # label wins; range without label shows both ends
    assert render_absolute(parse_inner("2025-06/2025-08|summer 2025")) == "summer 2025"
    assert render_absolute(parse_inner("2025-06/2025-08")) == "June 2025 – August 2025"


# --- render: relative (the live phrase)


def test_render_relative_accept_10_days_ago_then_a_year_ago():
    # ADR-056 Accept: a token for "10 days ago" renders "10 days ago" today, "a year ago" next year.
    rt = resolve(RelativeRef(unit="day", offset=-10, phrase="10 days ago"), ANCHOR)
    assert render_relative(rt, now=date(2026, 7, 17)) == "10 days ago"
    assert render_relative(rt, now=date(2027, 7, 7)) == "a year ago"


@pytest.mark.parametrize(
    "target,now,expected",
    [
        (date(2026, 7, 17), date(2026, 7, 17), "today"),
        (date(2026, 7, 16), date(2026, 7, 17), "yesterday"),
        (date(2026, 7, 18), date(2026, 7, 17), "tomorrow"),
        (date(2026, 7, 7), date(2026, 7, 17), "10 days ago"),
        (date(2026, 7, 27), date(2026, 7, 17), "in 10 days"),
        (date(2026, 5, 17), date(2026, 7, 17), "2 months ago"),
        (date(2025, 7, 20), date(2026, 7, 17), "a year ago"),
        (date(2024, 7, 17), date(2026, 7, 17), "2 years ago"),
    ],
)
def test_humanize_day_buckets(target, now, expected):
    rt = ResolvedTime(PartialDate(target.year, target.month, target.day))
    assert render_relative(rt, now) == expected


def test_humanize_day_round_half_up_ties():
    # 75 days → round(2.5); the spec pins round-half-UP so the web (Math.round) mirror matches
    # exactly. Python's default round() would give banker's "2 months ago" here.
    now = date(2026, 7, 17)
    assert render_relative(ResolvedTime(PartialDate(2026, 5, 3)), now) == "3 months ago"  # 75d→2.5
    assert render_relative(ResolvedTime(PartialDate(2026, 6, 2)), now) == "2 months ago"  # 45d→1.5


def test_render_relative_month_and_year_points():
    assert render_relative(ResolvedTime(PartialDate(2026, 6)), date(2026, 7, 17)) == "last month"
    assert render_relative(ResolvedTime(PartialDate(2026, 7)), date(2026, 7, 17)) == "this month"
    assert render_relative(ResolvedTime(PartialDate(2025, 7)), date(2026, 7, 17)) == "a year ago"
    assert render_relative(ResolvedTime(PartialDate(2025)), date(2026, 7, 17)) == "last year"
    assert render_relative(ResolvedTime(PartialDate(2024)), date(2026, 7, 17)) == "2 years ago"


def test_render_relative_future_coarse_points():
    now = date(2026, 7, 17)
    assert render_relative(ResolvedTime(PartialDate(2026, 8)), now) == "next month"
    assert render_relative(ResolvedTime(PartialDate(2026, 11)), now) == "in 4 months"
    assert render_relative(ResolvedTime(PartialDate(2027)), now) == "next year"
    assert render_relative(ResolvedTime(PartialDate(2029)), now) == "in 3 years"


def test_render_relative_range_is_absolute_label():
    rt = resolve(SeasonRef(season="summer", year=2025, phrase="summer 2025"), ANCHOR)
    assert render_relative(rt, date(2030, 1, 1)) == "summer 2025"  # absolute regardless of now


# --- render: index / LLM expansion


def test_expand_for_index_is_absolute_only():
    rt = resolve(RelativeRef(unit="day", offset=-10, phrase="10 days ago"), ANCHOR)
    assert expand_for_index(rt) == "7 July 2026"  # no relative phrase for stable embeddings


def test_expand_for_llm_adds_relative_hint():
    rt = resolve(RelativeRef(unit="day", offset=-10, phrase="10 days ago"), ANCHOR)
    assert expand_for_llm(rt, now=date(2026, 7, 17)) == "7 July 2026 (10 days ago)"
    # ranges get absolute only
    season = resolve(SeasonRef(season="summer", year=2025, phrase="summer 2025"), ANCHOR)
    assert expand_for_llm(season, now=date(2026, 7, 17)) == "summer 2025"


# --- render: whole-body helpers


def test_render_body_replaces_every_token_and_degrades_malformed():
    body = "Trip [[t:2025-06/2025-08|summer 2025]]; call [[t:2026-07-07]]; ref [[t:bad|fallback]]."
    out = render_body(body, now=date(2026, 7, 17))
    assert out == "Trip summer 2025; call 10 days ago; ref fallback."


def test_expand_body_for_index_and_llm():
    body = "on [[t:2026-07-07]]."
    assert expand_body_for_index(body) == "on 7 July 2026."
    assert expand_body_for_llm(body, now=date(2026, 7, 17)) == "on 7 July 2026 (10 days ago)."


# --- LLM-bound rendering contract: temporal metadata header (ADR-056 §4, M8.2 T3)


def test_format_occurred_point_range_and_unknown():
    assert format_occurred(date(2026, 7, 7), None) == "7 July 2026"
    assert format_occurred(date(2025, 6, 1), date(2025, 8, 31)) == "1 June 2025 – 31 August 2025"
    # A degenerate range (end == start) reads as a single point, not "X – X".
    assert format_occurred(date(2026, 7, 7), date(2026, 7, 7)) == "7 July 2026"
    assert format_occurred(None, None) == "unknown"


def test_temporal_header_recorded_and_occurred():
    header = temporal_header(
        recorded_at=datetime(2026, 7, 7, 22, 0),
        occurred_start=date(2025, 6, 1),
        occurred_end=date(2025, 8, 31),
        now=date(2026, 7, 17),
    )
    assert header == "recorded 7 July 2026 (10 days ago) · occurred 1 June 2025 – 31 August 2025"


def test_temporal_header_omits_missing_recorded_and_unknown_occurred():
    header = temporal_header(
        recorded_at=None, occurred_start=None, occurred_end=None, now=date(2026, 7, 17)
    )
    assert header == "occurred unknown"


def test_body_without_tokens_is_untouched():
    body = "no tokens here at all"
    assert render_body(body, date(2026, 7, 17)) == body
    assert expand_body_for_index(body) == body
