"""PipelineRunner tests (ADR-047) — sequential-on-completion, per-step ``on_fail``, parent/child
run linkage — all with **fake steps** (no live DB/LLM, 08 testing policy).

A fake step mirrors a real nightly job's ``run_scheduled``: it records its call order, opens its own
``agent_runs`` row via the store (picking up the ambient pipeline parent), finishes it
``succeeded``/``failed``, and never raises. The runner is then asserted on ordering, the
``continue`` vs ``halt`` branch, and that every child row links back to the one parent run.
"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.services.agent_runs import FAILED, SUCCEEDED
from app.services.pipeline import (
    CONTINUE,
    HALT,
    PipelineDef,
    PipelineRunner,
    PipelineStepDef,
)

from .fakes import FakeAgentRunStore


def _step(order: list[str], store: FakeAgentRunStore, agent: str, *, fail: bool = False):
    """A fake nightly job: records its order, opens+closes its own child run, never raises."""

    async def run() -> None:
        order.append(agent)
        run_id = await store.start(agent)
        await store.finish(
            run_id,
            status=FAILED if fail else SUCCEEDED,
            summary=f"{agent} {'failed' if fail else 'ok'}",
        )

    return run


def _runner(defn: PipelineDef, funcs, store: FakeAgentRunStore) -> PipelineRunner:
    return PipelineRunner(definition=defn, step_funcs=funcs, run_store=store)


# --- ordering ----------------------------------------------------------------------------------


async def test_steps_run_sequentially_in_definition_order():
    store = FakeAgentRunStore()
    order: list[str] = []
    defn = PipelineDef(
        name="nightly",
        cron="0 3 * * *",
        steps=(PipelineStepDef("a"), PipelineStepDef("b"), PipelineStepDef("c")),
    )
    funcs = {name: _step(order, store, name) for name in ("a", "b", "c")}

    outcome = await _runner(defn, funcs, store).run()

    assert order == ["a", "b", "c"]  # strict order, each after the previous completes
    assert outcome is not None
    assert [s.name for s in outcome.steps] == ["a", "b", "c"]
    assert all(s.status == SUCCEEDED for s in outcome.steps)
    assert outcome.halted_at is None


# --- parent / child run linkage (ADR-047 §5) ---------------------------------------------------


async def test_parent_run_opened_and_children_link_back():
    store = FakeAgentRunStore()
    order: list[str] = []
    defn = PipelineDef(
        name="nightly",
        cron="0 3 * * *",
        steps=(PipelineStepDef("a"), PipelineStepDef("b")),
    )
    funcs = {name: _step(order, store, name) for name in ("a", "b")}

    await _runner(defn, funcs, store).run()

    # exactly one parent (the pipeline) + one child per step.
    parents = [r for r in store.runs.values() if r.parent_run_id is None]
    children = [r for r in store.runs.values() if r.parent_run_id is not None]
    assert len(parents) == 1
    parent = parents[0]
    assert parent.agent == "nightly"
    assert {c.agent for c in children} == {"a", "b"}
    # every child links to the one parent; the parent itself is parentless.
    assert all(c.parent_run_id == parent.id for c in children)


async def test_parent_run_records_the_step_sequence_and_succeeds():
    store = FakeAgentRunStore()
    order: list[str] = []
    defn = PipelineDef(name="nightly", cron="0 3 * * *", steps=(PipelineStepDef("a"),))
    funcs = {"a": _step(order, store, "a")}

    await _runner(defn, funcs, store).run()

    parent = next(r for r in store.runs.values() if r.parent_run_id is None)
    assert parent.status == SUCCEEDED
    assert parent.details["pipeline"] == "nightly"
    steps = parent.details["steps"]
    assert [s["name"] for s in steps] == ["a"]
    assert steps[0]["status"] == SUCCEEDED
    assert steps[0]["child_run_id"] is not None


# --- on_fail: continue vs halt (ADR-047 §4) ----------------------------------------------------


async def test_continue_step_failure_does_not_stop_the_pipeline():
    store = FakeAgentRunStore()
    order: list[str] = []
    defn = PipelineDef(
        name="nightly",
        cron="0 3 * * *",
        steps=(
            PipelineStepDef("a"),
            PipelineStepDef("b", on_fail=CONTINUE),
            PipelineStepDef("c"),
        ),
    )
    funcs = {
        "a": _step(order, store, "a"),
        "b": _step(order, store, "b", fail=True),  # fails, but continue
        "c": _step(order, store, "c"),
    }

    outcome = await _runner(defn, funcs, store).run()

    assert order == ["a", "b", "c"]  # c still ran after b failed
    assert outcome.halted_at is None
    assert outcome.failed == 1 and outcome.succeeded == 2
    # a continue-only failure completes the pipeline: parent succeeds (per-step failure recorded).
    parent = next(r for r in store.runs.values() if r.parent_run_id is None)
    assert parent.status == SUCCEEDED


async def test_halt_step_failure_aborts_the_remaining_steps():
    store = FakeAgentRunStore()
    order: list[str] = []
    defn = PipelineDef(
        name="nightly",
        cron="0 3 * * *",
        steps=(
            PipelineStepDef("a"),
            PipelineStepDef("b", on_fail=HALT),
            PipelineStepDef("c"),
        ),
    )
    funcs = {
        "a": _step(order, store, "a"),
        "b": _step(order, store, "b", fail=True),  # fails with halt → abort
        "c": _step(order, store, "c"),
    }

    outcome = await _runner(defn, funcs, store).run()

    assert order == ["a", "b"]  # c never ran
    assert outcome.halted_at == "b"
    parent = next(r for r in store.runs.values() if r.parent_run_id is None)
    assert parent.status == FAILED
    # c has no child run because it never ran.
    assert {r.agent for r in store.runs.values() if r.parent_run_id is not None} == {"a", "b"}


async def test_halt_step_that_succeeds_does_not_abort():
    store = FakeAgentRunStore()
    order: list[str] = []
    defn = PipelineDef(
        name="nightly",
        cron="0 3 * * *",
        steps=(PipelineStepDef("a", on_fail=HALT), PipelineStepDef("b")),
    )
    funcs = {"a": _step(order, store, "a"), "b": _step(order, store, "b")}

    outcome = await _runner(defn, funcs, store).run()

    assert order == ["a", "b"]
    assert outcome.halted_at is None


# --- defensive: raising step + missing runnable ------------------------------------------------


async def test_a_raising_step_is_treated_as_failed_and_honours_halt():
    store = FakeAgentRunStore()
    ran_after = []

    async def boom() -> None:
        raise RuntimeError("job blew up despite rule 7")

    async def after() -> None:
        ran_after.append("after")

    defn = PipelineDef(
        name="nightly",
        cron="0 3 * * *",
        steps=(PipelineStepDef("boom", on_fail=HALT), PipelineStepDef("after")),
    )
    outcome = await _runner(defn, {"boom": boom, "after": after}, store).run()

    assert ran_after == []  # halted before `after`
    assert outcome.halted_at == "boom"
    assert outcome.steps[0].status == FAILED
    # the runner itself never raised; the parent run is closed failed.
    parent = next(r for r in store.runs.values() if r.parent_run_id is None)
    assert parent.status == FAILED


async def test_a_child_left_running_is_treated_as_failed_and_honours_halt():
    # A job that opened its row but never closed it (its own finish() blipped) must not read as
    # success — a halt step has to abort on it (ADR-047 §4), not proceed on half-built state.
    store = FakeAgentRunStore()
    order: list[str] = []

    async def leaves_running() -> None:
        order.append("stuck")
        await store.start("stuck")  # opened, never finished → status stays `running`

    defn = PipelineDef(
        name="nightly",
        cron="0 3 * * *",
        steps=(PipelineStepDef("stuck", on_fail=HALT), PipelineStepDef("b")),
    )
    outcome = await _runner(
        defn, {"stuck": leaves_running, "b": _step(order, store, "b")}, store
    ).run()

    assert order == ["stuck"]  # b never ran — the running child aborted a halt step
    assert outcome.halted_at == "stuck"
    assert outcome.steps[0].status == FAILED


async def test_a_raising_step_closes_its_orphaned_child_run():
    # rule 7: a job that raised after opening its run must not leave a `running` row orphaned.
    store = FakeAgentRunStore()

    async def opens_then_raises() -> None:
        await store.start("boom")  # row opened...
        raise RuntimeError("kaboom")  # ...then the job raises before finishing it

    defn = PipelineDef(name="nightly", cron="0 3 * * *", steps=(PipelineStepDef("boom"),))
    await _runner(defn, {"boom": opens_then_raises}, store).run()

    child = next(r for r in store.runs.values() if r.parent_run_id is not None)
    assert child.status == FAILED  # closed failed, not left running
    assert child.error is not None


async def test_missing_runnable_is_a_failed_step_and_honours_on_fail():
    store = FakeAgentRunStore()
    order: list[str] = []
    defn = PipelineDef(
        name="nightly",
        cron="0 3 * * *",
        steps=(PipelineStepDef("ghost", on_fail=HALT), PipelineStepDef("b")),
    )
    outcome = await _runner(defn, {"b": _step(order, store, "b")}, store).run()

    assert order == []  # halted at the missing step
    assert outcome.steps[0].status == "missing"
    assert outcome.halted_at == "ghost"


async def test_a_step_that_opens_no_run_is_reported_skipped_not_failed():
    store = FakeAgentRunStore()

    async def noop() -> None:  # a job that couldn't open its row (e.g. DB down) — returns quietly
        return None

    defn = PipelineDef(name="nightly", cron="0 3 * * *", steps=(PipelineStepDef("noop"),))
    outcome = await _runner(defn, {"noop": noop}, store).run()

    assert outcome.steps[0].status == "skipped"
    assert outcome.halted_at is None
    parent = next(r for r in store.runs.values() if r.parent_run_id is None)
    assert parent.status == SUCCEEDED


# --- resilience: parent row can't be opened ----------------------------------------------------


async def test_returns_none_when_the_parent_run_cannot_be_opened():
    class BrokenStore(FakeAgentRunStore):
        async def start(self, agent: str) -> str:
            raise RuntimeError("db down")

    store = BrokenStore()
    defn = PipelineDef(name="nightly", cron="0 3 * * *", steps=(PipelineStepDef("a"),))

    async def a() -> None:  # never reached
        raise AssertionError("step should not run when the parent can't open")

    outcome = await _runner(defn, {"a": a}, store).run()
    assert outcome is None  # never crashes the scheduler (rule 7)


# --- definition validation ---------------------------------------------------------------------


def test_bad_on_fail_is_rejected_at_definition_time():
    with pytest.raises(ValueError, match="on_fail"):
        PipelineStepDef("a", on_fail="retry")


def test_empty_pipeline_is_rejected():
    with pytest.raises(ValueError, match="at least one step"):
        PipelineDef(name="nightly", cron="0 3 * * *", steps=())


# --- config model: the migrated roster (ADR-047 §3) --------------------------------------------


def test_config_defines_nightly_and_weekly_pipelines():
    defs = Settings(scheduler_tz="UTC").pipeline_defs()
    by_name = {d.name: d for d in defs}

    assert set(by_name) == {"nightly", "weekly"}
    nightly = by_name["nightly"]
    # dependency order preserved from the retired ADR-010 stagger + the M6 sleep-cycle jobs woven
    # into their slots (ADR-048): chat-distiller first, inbox-drain before reindex, then dedup-sweep
    # after the entity jobs (it needs post-reindex embeddings).
    assert [s.name for s in nightly.steps] == [
        "chat-distiller",
        "data-sync",
        "db-backup",
        "inbox-drain",
        "reindex",
        "profile-refresh",
        "entity-backfill",
        "identity-capsule-refresh",
        "dedup-sweep",
        "store-sweep",
        "store-backup",
    ]
    assert [s.name for s in by_name["weekly"].steps] == ["integrity-drill", "maybe-digest"]
    # continue-dominant roster (ADR-047 §4): no migrated durability step aborts the rest.
    assert all(s.on_fail == CONTINUE for d in defs for s in d.steps)


def test_config_pipeline_crons_parse_as_crontabs():
    from apscheduler.triggers.cron import CronTrigger

    for d in Settings(scheduler_tz="UTC").pipeline_defs():
        assert CronTrigger.from_crontab(d.cron)
