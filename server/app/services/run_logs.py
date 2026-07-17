"""Live per-run log capture — the one genuinely new M8 subsystem (ADR-053 §1/§2).

An ``app.*``/``INFO``+ ``logging.Handler`` tags each record with the **currently-executing run**
(the ``current_run_id`` contextvar the ``agent_runs`` store maintains, ADR-053 §1) and appends it to
a **bounded per-run in-memory buffer** — synchronously, never awaiting a DB write (stdlib logging is
sync; rule 8). A small async :class:`RunLogFlusher` persists new lines to the durable
``agent_run_logs`` table on a ~1s cadence + immediately on run finish, and the poll endpoint
(``GET /activity/runs/{id}/logs``) reads the **table**, so the flush lag is the liveness bound and
the buffer never crosses a worker boundary.

Design guards:
- **Namespace + level filter** (``app.*`` at ``INFO``+): keeps clean progress lines and structurally
  excludes library ``DEBUG`` chatter — exactly where connection strings / bearer tokens leak — off a
  UI-rendered store (rule 11).
- **Bounded buffer, drop-oldest + elision marker**: a runaway logger can't grow memory unbounded,
  and the drop is surfaced as a ``… N lines elided`` line rather than a silent cap (rule 7).
- **Rebuildable op-state, not graph truth** (rule 1): a flush failure logs and drops the batch; the
  store remains the source of record.

The handler + buffers are pure in-memory (unit-testable with a fake store); only the
:class:`PgRunLogStore` + the flusher's persistence touch the DB.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from ..db import Database
from .agent_runs import current_run_id

logger = logging.getLogger(__name__)

# Only our own namespace is captured (ADR-053 §1) — see `_in_namespace`.
APP_NAMESPACE = "app"


@dataclass(frozen=True)
class RunLogLine:
    """One captured log line — the wire + storage shape (``agent_run_logs`` row minus ``id``)."""

    seq: int  # per-run monotonic ordinal (gaps allowed on overflow-elision); the poll cursor key
    ts: datetime
    level: str
    message: str


def _in_namespace(logger_name: str, namespace: str) -> bool:
    """True for the namespace logger itself and its descendants (``app`` / ``app.x`` — not
    ``apple``)."""
    return logger_name == namespace or logger_name.startswith(namespace + ".")


class _RunBuffer:
    """A single run's un-flushed lines + its monotonic seq counter + a drop tally.

    Bounded: when full, the oldest un-flushed line is dropped and counted; the next drain emits one
    ``… N lines elided`` marker in its place so the loss is visible (rule 7). Not thread-safe on its
    own — :class:`RunLogBuffers` serialises all access under one lock (the handler can ``emit`` from
    an ``asyncio.to_thread`` worker while the flusher drains on the loop)."""

    def __init__(self, max_lines: int) -> None:
        self._max = max(1, max_lines)
        self._pending: deque[RunLogLine] = deque()
        # 1-based ordinals, so the poll's default `after_seq=0` cursor includes the first line
        # (a "give me everything from the start" read) — a 0-based first line would be skipped.
        self._seq = 1
        self._elided = 0

    def append(self, *, level: str, message: str, ts: datetime) -> None:
        seq = self._seq
        self._seq += 1
        if len(self._pending) >= self._max:
            self._pending.popleft()  # drop oldest un-flushed line; surfaced as an elision marker
            self._elided += 1
        self._pending.append(RunLogLine(seq=seq, ts=ts, level=level, message=message))

    def drain(self) -> list[RunLogLine]:
        lines = list(self._pending)
        self._pending.clear()
        if self._elided:
            marker = RunLogLine(
                seq=self._seq,
                ts=datetime.now(UTC),
                level="WARNING",
                message=f"… {self._elided} line(s) elided (log buffer overflow)",
            )
            self._seq += 1
            self._elided = 0
            lines.append(marker)
        return lines


class RunLogBuffers:
    """The per-run buffer registry the handler writes and the flusher drains. Thread-safe."""

    def __init__(self, *, max_lines: int) -> None:
        self._max_lines = max_lines
        self._buffers: dict[str, _RunBuffer] = {}
        self._lock = threading.Lock()

    def capture(self, run_id: str, *, level: str, message: str, ts: datetime) -> None:
        with self._lock:
            buf = self._buffers.get(run_id)
            if buf is None:
                buf = _RunBuffer(self._max_lines)
                self._buffers[run_id] = buf
            buf.append(level=level, message=message, ts=ts)

    def drain(self, run_id: str) -> list[RunLogLine]:
        with self._lock:
            buf = self._buffers.get(run_id)
            return buf.drain() if buf is not None else []

    def active_run_ids(self) -> list[str]:
        with self._lock:
            return list(self._buffers)

    def reap(self, run_id: str) -> None:
        with self._lock:
            self._buffers.pop(run_id, None)


class RunLogHandler(logging.Handler):
    """Process-wide ``app.*``/``INFO``+ handler → the active run's buffer (ADR-053 §1). Always
    attached; a no-op when the emitting task isn't inside a run scope."""

    def __init__(self, buffers: RunLogBuffers, *, namespace: str = APP_NAMESPACE) -> None:
        super().__init__(level=logging.INFO)
        self._buffers = buffers
        self._namespace = namespace

    def emit(self, record: logging.LogRecord) -> None:
        try:
            # Namespace + level filter (ADR-053 §1). The level check is explicit — `Handler.handle`
            # doesn't apply it (the logger's `callHandlers` normally does), so checking here keeps
            # DEBUG chatter out however the handler is reached, not only via the root logger.
            if record.levelno < self.level or not _in_namespace(record.name, self._namespace):
                return
            run_id = current_run_id()
            if run_id is None:
                return
            self._buffers.capture(
                run_id,
                level=record.levelname,
                message=record.getMessage(),
                ts=datetime.fromtimestamp(record.created, tz=UTC),
            )
        except Exception:  # noqa: BLE001 — logging must never raise into the caller
            self.handleError(record)


class RunLogStore(Protocol):
    async def insert_lines(self, run_id: str, lines: list[RunLogLine]) -> None: ...

    async def read_after(self, run_id: str, *, after_seq: int, limit: int) -> list[RunLogLine]: ...


class PgRunLogStore:
    """asyncpg-backed ``agent_run_logs`` store — plain SQL, no ORM (ADR-011)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def insert_lines(self, run_id: str, lines: list[RunLogLine]) -> None:
        if not lines:
            return
        rows = [(run_id, line.seq, line.ts, line.level, line.message) for line in lines]
        async with self._db.acquire() as conn:
            # ON CONFLICT DO NOTHING is defensive against the UNIQUE (run_id, seq) index: `drain`
            # clears the buffer so the live flusher never re-flushes a batch, but this keeps a
            # would-be duplicate (run, seq) a harmless no-op instead of failing the whole batch
            # (idempotent, rule 6) if the write path is ever driven differently.
            await conn.executemany(
                """
                INSERT INTO agent_run_logs (run_id, seq, ts, level, message)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (run_id, seq) DO NOTHING
                """,
                rows,
            )

    async def read_after(self, run_id: str, *, after_seq: int, limit: int) -> list[RunLogLine]:
        async with self._db.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT seq, ts, level, message
                  FROM agent_run_logs
                 WHERE run_id = $1 AND seq > $2
                 ORDER BY seq
                 LIMIT $3
                """,
                run_id,
                after_seq,
                limit,
            )
        return [
            RunLogLine(seq=r["seq"], ts=r["ts"], level=r["level"], message=r["message"])
            for r in rows
        ]


class RunLogFlusher:
    """Persists buffered run logs to ``agent_run_logs`` on a ~1s cadence + right away on run finish.

    One long-lived task on the app's event loop. :meth:`request_flush` (wired as the ``agent_runs``
    run-finish hook) flushes + reaps a finished run's buffer promptly so a completed run's logs are
    fully durable (ADR-053 §2) and memory doesn't accumulate one buffer per historical run."""

    def __init__(
        self, *, buffers: RunLogBuffers, store: RunLogStore, interval_seconds: float
    ) -> None:
        self._buffers = buffers
        self._store = store
        self._interval = max(0.05, interval_seconds)
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._reap_now: set[str] = set()
        self._stopped = False

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Stop the loop after a final flush of everything still buffered. Idempotent."""
        self._stopped = True
        self._wake.set()
        if self._task is not None:
            await self._task
            self._task = None

    def request_flush(self, run_id: str) -> None:
        """The run-finish hook (sync, on the loop): flush + reap this run on the next tick."""
        self._reap_now.add(run_id)
        self._wake.set()

    async def _run(self) -> None:
        while not self._stopped:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._interval)
            except TimeoutError:
                pass
            self._wake.clear()
            await self._tick()
        await self._tick()  # final drain on shutdown

    async def _tick(self) -> None:
        reap = self._reap_now
        self._reap_now = set()
        for run_id in self._buffers.active_run_ids():
            await self._flush_run(run_id, reap=run_id in reap)
            reap.discard(run_id)
        # Finished runs with no buffered lines left (already drained) — just reap them.
        for run_id in reap:
            await self._flush_run(run_id, reap=True)

    async def _flush_run(self, run_id: str, *, reap: bool) -> None:
        lines = self._buffers.drain(run_id)
        try:
            await self._store.insert_lines(run_id, lines)
        except Exception:  # noqa: BLE001 — op-state (rule 1): log + drop, never crash the flusher
            logger.exception("flushing %d log line(s) for run %s failed", len(lines), run_id)
        if reap:
            self._buffers.reap(run_id)
