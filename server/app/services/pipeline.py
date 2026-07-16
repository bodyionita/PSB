"""Pipeline runner — the scheduling primitive (ADR-047).

A **pipeline** is the only schedulable unit: a name + one cron + an ordered list of **steps**, run
**sequentially, each starting only when the previous completes**, from a single scheduled start. No
minute-tuning, one step's RAM at a time (ADR-047 §1/§3), and the 03:00–05:00 window (ADR-010) is
enforced by *sequencing from a 03:00 start*, not by stagger.

Each step is one existing nightly job — an idempotent, never-raising ``run_scheduled`` coroutine
that already opens its own ``agent_runs`` row (rule 7). The runner opens a **parent** run for the
pipeline and executes every step inside :func:`~app.services.agent_runs.child_run_scope`, so each
step's own row links back to the parent via ``parent_run_id`` (ADR-047 §5) with **no change to what
any job does** — the job never knows it is running under a pipeline. The runner reads each step's
child run back to decide its ``on_fail`` policy:

- ``continue`` (the rule-7 default): a failed step is recorded and the pipeline proceeds — one flaky
  LLM call never costs the night its backups.
- ``halt``: a failed step aborts the remaining steps — reserved for a foundational precondition.

The parent run records the per-step sequence + status in its ``details``; richer visualization (a
pipeline timeline) is deferred to the M8 ops console. Single-flight is unchanged: the runner adds no
new locking — a manual run firing mid-pipeline is serialised by the steps' own guards (the store git
lock + per-service single-flight), exactly as before (ADR-047 §6).

The runner depends only on the :class:`~app.services.agent_runs.AgentRunStore` protocol and a
mapping of step name → coroutine, so it unit-tests with fake steps (no live DB/LLM — 08 policy).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field

from .agent_runs import (
    FAILED,
    RUNNING,
    SKIPPED,
    SUCCEEDED,
    AgentRunStore,
    child_run_scope,
)

logger = logging.getLogger(__name__)

# Per-step failure policy (ADR-047 §4).
CONTINUE = "continue"
HALT = "halt"
_ON_FAIL_VALUES = (CONTINUE, HALT)

# A step whose runnable wasn't found in the map — a definition/wiring mismatch, never a job outcome.
MISSING = "missing"
# Step statuses that count as a failure for `on_fail` + the parent's failed tally. ``skipped`` (the
# job opened no run but didn't hard-fail) is *not* a failure — it never halts a halt step.
_FAILURE_STATUSES = frozenset({FAILED, MISSING})
# A step's child run counts as *cleanly done* only in these terminal states; anything else — a
# child left ``running`` (its own ``finish`` blipped) or an unknown status — is read as a failure so
# a ``halt`` step reliably aborts on it (ADR-047 §4), rather than proceeding on half-built state.
_STEP_OK_STATUSES = frozenset({SUCCEEDED, SKIPPED})

# A step is one job's scheduler/CLI entry point: no args, returns an outcome-or-``None`` the runner
# ignores, never raises (rule 7). Matches every existing ``*.run_scheduled``.
StepFunc = Callable[[], Awaitable[object | None]]


@dataclass(frozen=True)
class PipelineStepDef:
    """One ordered step: the job/agent name (also the child ``agent_runs.agent`` and the key into
    the runner's step-func map) + its failure policy."""

    name: str
    on_fail: str = CONTINUE

    def __post_init__(self) -> None:
        if self.on_fail not in _ON_FAIL_VALUES:
            raise ValueError(
                f"pipeline step {self.name!r}: on_fail must be one of {_ON_FAIL_VALUES},"
                f" got {self.on_fail!r}"
            )


@dataclass(frozen=True)
class PipelineDef:
    """A named, ordered pipeline + its single cron (ADR-047 §1). The name doubles as the parent
    ``agent_runs.agent``. Cadence maps to a pipeline (``nightly``/``weekly``), not to per-step
    day-of-week conditionals (ADR-047 §3)."""

    name: str
    cron: str
    steps: tuple[PipelineStepDef, ...]

    def __post_init__(self) -> None:
        if not self.steps:
            raise ValueError(f"pipeline {self.name!r}: needs at least one step")


@dataclass
class _StepResult:
    """One step's outcome, folded into the parent run's ``details`` (ADR-047 §5)."""

    name: str
    on_fail: str
    status: str  # succeeded | failed | skipped | missing
    child_run_id: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "on_fail": self.on_fail,
            "status": self.status,
            "child_run_id": self.child_run_id,
        }


@dataclass
class PipelineOutcome:
    """Result of one pipeline run — feeds the parent ``agent_runs`` row + tests."""

    pipeline: str
    # the parent agent_runs row the steps link back to (ADR-047 §5).
    parent_run_id: str | None = None
    steps: list[_StepResult] = field(default_factory=list)
    halted_at: str | None = None

    @property
    def failed(self) -> int:
        return sum(1 for s in self.steps if s.status in _FAILURE_STATUSES)

    @property
    def succeeded(self) -> int:
        return sum(1 for s in self.steps if s.status == SUCCEEDED)

    @property
    def skipped(self) -> int:
        return sum(1 for s in self.steps if s.status == SKIPPED)

    def summary(self) -> str:
        base = (
            f"{self.pipeline} pipeline: {len(self.steps)} step(s),"
            f" {self.succeeded} succeeded, {self.failed} failed, {self.skipped} skipped"
        )
        if self.halted_at is not None:
            base += f" — HALTED at {self.halted_at}"
        return base

    def as_dict(self) -> dict[str, object]:
        return {
            "pipeline": self.pipeline,
            "parent_run_id": self.parent_run_id,
            "steps": [s.as_dict() for s in self.steps],
            "halted_at": self.halted_at,
        }


class PipelineRunner:
    """Runs one :class:`PipelineDef` sequentially, linking each step's run to a parent run."""

    def __init__(
        self,
        *,
        definition: PipelineDef,
        step_funcs: Mapping[str, StepFunc],
        run_store: AgentRunStore,
    ) -> None:
        self._def = definition
        self._funcs = step_funcs
        self._runs = run_store

    async def run(self) -> PipelineOutcome | None:
        """The scheduler/CLI entry point. Opens the parent run, executes the steps, closes it; never
        raises (rule 7). Returns the outcome, or ``None`` when the parent row couldn't be opened."""
        try:
            parent_id = await self._runs.start(self._def.name)
        except Exception:  # noqa: BLE001 — DB down at row-open: log, never crash the scheduler
            logger.exception("could not open parent agent_runs row for pipeline %s", self._def.name)
            return None
        try:
            outcome = await self._run_steps(parent_id)
            logger.info("%s", outcome.summary())
            # The pipeline "failed" only when a halt step aborted it; a continue-step failure is
            # recorded per-step but the pipeline still completed its roster (ADR-047 §4).
            status = FAILED if outcome.halted_at is not None else SUCCEEDED
            await self._runs.finish(
                parent_id, status=status, summary=outcome.summary(), details=outcome.as_dict()
            )
            return outcome
        except Exception as exc:  # noqa: BLE001 — end the parent failed with context, never crash
            logger.exception("pipeline %s crashed", self._def.name)
            await self._safe_finish(parent_id, exc)
            return None

    async def _run_steps(self, parent_id: str) -> PipelineOutcome:
        outcome = PipelineOutcome(pipeline=self._def.name, parent_run_id=parent_id)
        for step in self._def.steps:
            result = await self._run_step(parent_id, step)
            outcome.steps.append(result)
            if result.status in _FAILURE_STATUSES and step.on_fail == HALT:
                outcome.halted_at = step.name
                logger.warning(
                    "pipeline %s halted at step %s (on_fail=halt)", self._def.name, step.name
                )
                break
        return outcome

    async def _run_step(self, parent_id: str, step: PipelineStepDef) -> _StepResult:
        func = self._funcs.get(step.name)
        if func is None:
            # A definition/wiring mismatch — never silently skip (rule 7). Treated as a failed step
            # so ``on_fail`` still governs whether the pipeline proceeds.
            logger.error("pipeline %s: no runnable for step %s", self._def.name, step.name)
            return _StepResult(name=step.name, on_fail=step.on_fail, status=MISSING)
        # Every row the step opens links to the parent and is captured here (ADR-047 §5).
        with child_run_scope(parent_id) as child_ids:
            try:
                await func()
                raised = False
            except Exception:  # noqa: BLE001 — a job shouldn't raise (rule 7); if it does, the step
                logger.exception(  # is failed and on_fail decides — the pipeline never crashes.
                    "pipeline %s: step %s raised", self._def.name, step.name
                )
                raised = True
        if raised:
            # A job that raised mid-flight left its own run row open — close it failed so no
            # ``running`` row is orphaned (rule 7: failures end runs as failed, always visible).
            await self._fail_orphaned_children(child_ids)
        own_run_id, status = await self._step_status(child_ids, step.name, raised=raised)
        return _StepResult(
            name=step.name, on_fail=step.on_fail, status=status, child_run_id=own_run_id
        )

    async def _step_status(
        self, child_ids: list[str], step_name: str, *, raised: bool
    ) -> tuple[str | None, str]:
        """A step's status is its **own** job run — the child whose ``agent == step_name``
        (ADR-050). Runs the job *spawns* under a different agent (``"capture"`` organize/reorganize)
        stay parented to the pipeline for visibility (rule 7) but do **not** gate the step — so a
        data-safe ``inbox/`` fallback (its ``capture`` run closed ``failed``) never fails the
        enclosing step. FAILED if it raised or the own run is not *cleanly done*
        (``failed``/orphaned ``running``/unknown — a ``halt`` step must abort on it); otherwise the
        own run's terminal status (``succeeded``/``skipped``). A step that opened no own run (e.g.
        ``store-sweep``) but didn't raise is ``skipped``. Returns ``(own_run_id, status)`` so the
        step reports its **own** run, not a nested spawned one."""
        own_run_id: str | None = None
        own_status: str | None = None
        failed = raised
        for cid in child_ids:
            run = await self._runs.get(cid)
            if run is None or run.agent != step_name:
                continue  # a nested spawned run (organize/capture) — visible, not step-gating
            own_run_id = cid
            own_status = run.status
            if run.status not in _STEP_OK_STATUSES:
                failed = True  # own job not cleanly done → a halt step must abort on it
        if failed:
            return own_run_id, FAILED
        if own_status is None:
            return own_run_id, SKIPPED  # the step opened no own run — nothing it itself ran
        return own_run_id, own_status

    async def _fail_orphaned_children(self, child_ids: list[str]) -> None:
        """Close any still-``running`` child run a raising step left behind (rule 7). Best-effort:
        the DB may be down — log and move on, never re-raise into the pipeline."""
        for cid in child_ids:
            try:
                run = await self._runs.get(cid)
                if run is not None and run.status == RUNNING:
                    await self._runs.finish(
                        cid,
                        status=FAILED,
                        summary="step raised before closing its run",
                        error="pipeline step raised",
                    )
            except Exception:  # noqa: BLE001 — last-ditch cleanup; never crash the pipeline
                logger.exception("could not close orphaned child run %s", cid)

    async def _safe_finish(self, parent_id: str, exc: Exception) -> None:
        try:
            await self._runs.finish(
                parent_id,
                status=FAILED,
                summary=f"{self._def.name} pipeline crashed",
                error=f"{type(exc).__name__}: {exc}",
            )
        except Exception:  # noqa: BLE001 — last-ditch; the DB may be down
            logger.exception("could not close pipeline %s parent run %s", self._def.name, parent_id)
