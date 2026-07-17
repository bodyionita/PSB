"""Unit tests for the recursive run-tree builder (M8.1, ADR-054 §2).

``build_run_tree`` folds a flat descendant list into the ``children[]`` forest a run's detail
renders; pure logic, so it's tested directly (no DB). The recursive CTE that produces the flat rows
is covered by the real-PG smoke (``check_run_children_tree``)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.services.agent_runs import RunTreeRow, build_run_tree

BASE = datetime(2026, 7, 17, 3, 0, 0, tzinfo=UTC)


def _row(rid: str, parent: str | None, *, offset: int, agent: str = "step") -> RunTreeRow:
    return RunTreeRow(
        id=rid,
        agent=agent,
        status="succeeded",
        ts=BASE + timedelta(seconds=offset),
        summary=f"summary-{rid}",
        parent_run_id=parent,
    )


def test_empty_rows_give_empty_forest():
    assert build_run_tree([], "root") == []


def test_flat_children_attach_to_root_in_order():
    # The common shape (ADR-050): steps + spawned runs all parent directly to the pipeline root.
    rows = [
        _row("a", "root", offset=1),
        _row("b", "root", offset=2),
        _row("c", "root", offset=3),
    ]
    forest = build_run_tree(rows, "root")
    assert [n.id for n in forest] == ["a", "b", "c"]  # early→late (input order preserved)
    assert all(n.children == [] for n in forest)
    assert forest[0].name == "step" and forest[0].summary == "summary-a"


def test_nested_grandchild_reflects_true_depth():
    rows = [
        _row("stepA", "root", offset=1),
        _row("grand", "stepA", offset=2, agent="capture"),
        _row("stepB", "root", offset=3),
    ]
    forest = build_run_tree(rows, "root")
    assert [n.id for n in forest] == ["stepA", "stepB"]
    step_a = forest[0]
    assert [c.id for c in step_a.children] == ["grand"]
    assert step_a.children[0].name == "capture"


def test_pre_creation_tolerates_child_before_parent_in_input():
    # Nodes are all pre-created, so a child appearing before its parent in the list still attaches.
    rows = [
        _row("grand", "stepA", offset=5),
        _row("stepA", "root", offset=1),
    ]
    forest = build_run_tree(rows, "root")
    assert [n.id for n in forest] == ["stepA"]
    assert [c.id for c in forest[0].children] == ["grand"]


def test_orphan_parent_not_in_set_kept_at_top_level():
    # A row whose parent is neither the root nor in the descendant set is never dropped (rule 7):
    # it surfaces at the top level rather than vanishing.
    rows = [_row("x", "some-missing-parent", offset=1)]
    forest = build_run_tree(rows, "root")
    assert [n.id for n in forest] == ["x"]
