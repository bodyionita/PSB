"""In-memory per-provider status tracking (ADR-044).

A small, unit-testable collaborator held by the :class:`ProviderRegistry`. It records — per
provider id, captured at the chat/STT/embedding call sites — the last runtime error, the last
success time, and a consecutive-failure counter. This closes the vision-P8 / rule-7 gap the M4
Accept exposed: a provider that silently falls back with no visible reason.

No persistence, no DB write, no migration (ADR-044 decision 2): a chat/STT fallback is a
*degradation signal*, not a durable job failure (rule 7's durability mandate is about jobs
ending in ``agent_runs``, which this is not). The failure mode that motivated this is
*persistent* — every call failed — so even after a redeploy wipes memory, the next call
repopulates the last-error immediately. Durable reliability *history* is a clean follow-up
(persist this same record shape) if it ever becomes a product need.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

# The last-error message is the ``ProviderUnavailable`` text as-is (already ``{id}``-prefixed;
# carries a URL + status but no headers/keys — verified safe, ADR-044 decision 4). Truncated so a
# pathological upstream error body can't bloat the in-memory snapshot or the JSON response.
MAX_ERROR_MESSAGE_LEN = 500


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class ProviderError:
    """The last runtime failure for a provider — sticky (see :class:`ProviderStatusTracker`)."""

    message: str
    at: datetime


@dataclass(frozen=True)
class ProviderStatus:
    """An immutable snapshot of one provider's runtime status."""

    last_error: ProviderError | None = None
    last_success_at: datetime | None = None
    consecutive_failures: int = 0


@dataclass
class _MutableStatus:
    last_error: ProviderError | None = None
    last_success_at: datetime | None = None
    consecutive_failures: int = 0

    def frozen(self) -> ProviderStatus:
        return ProviderStatus(
            last_error=self.last_error,
            last_success_at=self.last_success_at,
            consecutive_failures=self.consecutive_failures,
        )


class ProviderStatusTracker:
    """Mutable in-memory status per provider id.

    ``now`` is injectable so unit tests can pin timestamps; production uses UTC wall-clock.
    """

    def __init__(self, *, now: Callable[[], datetime] = _utc_now) -> None:
        self._now = now
        self._status: dict[str, _MutableStatus] = {}

    def record_success(self, provider_id: str) -> None:
        """Stamp ``last_success_at`` and reset the consecutive-failure counter.

        ``last_error`` is **sticky** — a later success does *not* clear it (ADR-044 decision 4):
        the post-hoc forensic trail ("broke at 2pm, recovered at 2:05") is the point, and
        ``consecutive_failures == 0`` is the clean "is it broken *right now*" signal instead.
        """
        st = self._status.setdefault(provider_id, _MutableStatus())
        st.last_success_at = self._now()
        st.consecutive_failures = 0

    def record_failure(self, provider_id: str, message: str) -> None:
        """Overwrite ``last_error`` (truncated) and bump the consecutive-failure counter."""
        st = self._status.setdefault(provider_id, _MutableStatus())
        st.last_error = ProviderError(message=message[:MAX_ERROR_MESSAGE_LEN], at=self._now())
        st.consecutive_failures += 1

    def status_for(self, provider_id: str) -> ProviderStatus:
        """The current snapshot for ``provider_id`` (a clean zero-state if never seen)."""
        st = self._status.get(provider_id)
        return st.frozen() if st is not None else ProviderStatus()
