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


def _runner(defn: PipelineDef, funcs, store: FakeAgentRunStore, *, job_runner=None):
    return PipelineRunner(definition=defn, step_funcs=funcs, run_store=store, job_runner=job_runner)


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


# --- ADR-050: a step's status is its OWN job run, not the transitive scope ---------------------


async def test_a_nested_spawned_capture_failure_does_not_fail_the_step():
    # ADR-050: a data-safe ``inbox/`` fallback closes its nested ``agent="capture"`` organize run
    # ``failed`` (rule 7). That nested run is flattened into the step's scope via child_run_scope,
    # but it must NOT fail the enclosing step (chat-distiller/inbox-drain) whose OWN run succeeded.
    store = FakeAgentRunStore()
    order: list[str] = []

    async def distiller() -> (
        None
    ):  # own run ok + a spawned capture that inbox-falls (chat-distiller)
        own = await store.start("chat-distiller")
        nested = await store.start("capture")  # a spawned organize under a different agent
        await store.finish(nested, status=FAILED, summary="organize -> inbox fallback")
        await store.finish(own, status=SUCCEEDED, summary="4 endorsed")

    defn = PipelineDef(
        name="nightly",
        cron="0 3 * * *",
        # on_fail=HALT so we also prove a benign nested fallback never trips a halt gate.
        steps=(PipelineStepDef("chat-distiller", on_fail=HALT), PipelineStepDef("b")),
    )
    outcome = await _runner(
        defn, {"chat-distiller": distiller, "b": _step(order, store, "b")}, store
    ).run()

    step = outcome.steps[0]
    assert step.status == SUCCEEDED  # the nested capture failure did not fail the step
    assert outcome.halted_at is None  # nor abort the halt step
    assert order == ["b"]  # the pipeline proceeded
    # the step reports its OWN run, not the nested capture one
    assert step.child_run_id is not None
    assert store.runs[step.child_run_id].agent == "chat-distiller"
    # the nested capture run stays visible + parented to the pipeline parent (rule 7)
    nested = next(r for r in store.runs.values() if r.agent == "capture")
    assert nested.status == FAILED
    assert nested.parent_run_id is not None


async def test_the_steps_own_failure_still_fails_even_with_a_clean_nested_run():
    # The other side of ADR-050: the own-run gate must not HIDE a genuine step failure. If the job's
    # own run fails, the step fails (and a halt step aborts) even when a spawned run succeeded.
    store = FakeAgentRunStore()
    order: list[str] = []

    async def drainer() -> None:
        own = await store.start("inbox-drain")
        nested = await store.start("capture")
        await store.finish(nested, status=SUCCEEDED, summary="reorganized ok")
        await store.finish(own, status=FAILED, summary="drain itself errored")

    defn = PipelineDef(
        name="nightly",
        cron="0 3 * * *",
        steps=(PipelineStepDef("inbox-drain", on_fail=HALT), PipelineStepDef("b")),
    )
    outcome = await _runner(
        defn, {"inbox-drain": drainer, "b": _step(order, store, "b")}, store
    ).run()

    assert outcome.steps[0].status == FAILED  # own-run failure not masked by the clean nested run
    assert outcome.halted_at == "inbox-drain"
    assert order == []  # halted before b


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


# --- single-flight guard integration (M8, ADR-053 §7) -----------------------------------------


async def test_step_marks_itself_running_in_the_job_runner():
    from app.services.job_runner import JobRunner

    store = FakeAgentRunStore()
    order: list[str] = []
    runner_guard = JobRunner()
    seen_running: list[bool] = []

    async def watched() -> None:
        order.append("w")
        seen_running.append(runner_guard.is_running("w"))  # marked running during execution
        run_id = await store.start("w")
        await store.finish(run_id, status=SUCCEEDED)

    defn = PipelineDef(name="nightly", cron="0 3 * * *", steps=(PipelineStepDef("w"),))
    outcome = await _runner(defn, {"w": watched}, store, job_runner=runner_guard).run()

    assert seen_running == [True]
    assert outcome.steps[0].status == SUCCEEDED
    assert not runner_guard.is_running("w")  # released after the step


async def test_step_skipped_when_a_manual_run_holds_the_slot():
    import asyncio

    from app.services.job_runner import JobRunner

    store = FakeAgentRunStore()
    guard = JobRunner()
    ran: list[str] = []

    async def job() -> None:
        ran.append("reindex")
        run_id = await store.start("reindex")
        await store.finish(run_id, status=SUCCEEDED)

    # A manual run holds "reindex" for the duration of the pipeline step.
    started = asyncio.Event()
    release = asyncio.Event()

    async def manual() -> None:
        started.set()
        await release.wait()

    manual_task = asyncio.create_task(guard.run_manual("reindex", manual))
    await started.wait()

    defn = PipelineDef(name="nightly", cron="0 3 * * *", steps=(PipelineStepDef("reindex"),))
    outcome = await _runner(defn, {"reindex": job}, store, job_runner=guard).run()

    assert ran == []  # the scheduled step was skipped, not run concurrently
    assert outcome.steps[0].status == "skipped"
    assert outcome.halted_at is None  # a skip never halts

    release.set()
    await manual_task


# --- config model: the migrated roster (ADR-047 §3) --------------------------------------------


def test_config_defines_nightly_and_weekly_pipelines():
    defs = Settings(scheduler_tz="UTC").pipeline_defs()
    by_name = {d.name: d for d in defs}

    assert set(by_name) == {"nightly", "weekly"}
    nightly = by_name["nightly"]
    # dependency order preserved from the retired ADR-010 stagger + the M6 sleep-cycle jobs woven
    # into their slots (ADR-048): chat-distiller first, inbox-drain before reindex, then dedup-sweep
    # after the entity jobs (it needs post-reindex embeddings); the M8 read-only graph-health
    # reporter is the nightly TAIL (ADR-053 §9 — reports on the settled state).
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
        "graph-health",
    ]
    assert [s.name for s in by_name["weekly"].steps] == ["integrity-drill", "maybe-digest"]
    # continue-dominant roster (ADR-047 §4): no migrated durability step aborts the rest.
    assert all(s.on_fail == CONTINUE for d in defs for s in d.steps)


def test_config_pipeline_crons_parse_as_crontabs():
    from apscheduler.triggers.cron import CronTrigger

    for d in Settings(scheduler_tz="UTC").pipeline_defs():
        assert CronTrigger.from_crontab(d.cron)
