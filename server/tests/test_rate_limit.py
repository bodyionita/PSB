"""Fixed-window login rate limiter (ADR-007: login is rate-limited)."""

from __future__ import annotations

from app.services.rate_limit import RateLimiter


def test_allows_up_to_limit_then_blocks():
    rl = RateLimiter(max_events=5, window_seconds=60)
    assert [rl.allow("ip", now=0) for _ in range(5)] == [True] * 5
    assert rl.allow("ip", now=0) is False


def test_window_slides_and_frees_capacity():
    rl = RateLimiter(max_events=2, window_seconds=60)
    assert rl.allow("ip", now=0) is True
    assert rl.allow("ip", now=1) is True
    assert rl.allow("ip", now=2) is False
    # After the window passes, capacity returns.
    assert rl.allow("ip", now=61) is True


def test_keys_are_isolated():
    rl = RateLimiter(max_events=1, window_seconds=60)
    assert rl.allow("a", now=0) is True
    assert rl.allow("a", now=0) is False
    assert rl.allow("b", now=0) is True  # different IP unaffected
