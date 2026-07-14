"""TagConsolidationService tests: fake store + fake chat + tmp vault (no DB, no LLM)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import Settings
from app.providers.base import ProviderUnavailable
from app.providers.registry import ProviderRegistry
from app.tags.consolidation import TagMerge
from app.tags.service import TagConsolidationService

from .fakes import (
    FakeAgentRunStore,
    FakeChatProvider,
    FakeCommitBackup,
    FakeIndexer,
    FakeTagStore,
    fake_routing,
)


def _registry(chat: FakeChatProvider) -> ProviderRegistry:
    return ProviderRegistry(
        {"fake-chat": chat},
        chat_chain=["fake-chat"],
        distill_chain=["fake-chat"],
        embedding_provider_id="none",
        stt_chain=[],
    )


def _service(
    tmp_path: Path,
    *,
    store: FakeTagStore,
    chat: FakeChatProvider | None = None,
    indexer: FakeIndexer | None = None,
    backup: FakeCommitBackup | None = None,
    runs: FakeAgentRunStore | None = None,
) -> TagConsolidationService:
    settings = Settings(graph_store_path=str(tmp_path / "vault"))
    return TagConsolidationService(
        settings=settings,
        store=store,
        routing=fake_routing(_registry(chat or FakeChatProvider("fake-chat", reply="{}"))),
        indexer=indexer or FakeIndexer(),
        store_backup=backup or FakeCommitBackup(),
        run_store=runs or FakeAgentRunStore(),
    )


# --- propose ------------------------------------------------------------------------------------


async def test_propose_returns_sanitised_merges(tmp_path: Path):
    store = FakeTagStore(counts=[("second-brain", 5), ("secondbrain", 2), ("calm", 4)])
    plan = {"merges": [{"canonical": "second-brain", "variants": ["secondbrain"]}]}
    chat = FakeChatProvider("fake-chat", reply=json.dumps(plan))
    service = _service(tmp_path, store=store, chat=chat)

    proposal = await service.propose()

    assert proposal.plan_id  # opaque correlation id present
    assert proposal.merges == [TagMerge(canonical="second-brain", variants=("secondbrain",))]
    assert store.vocab_calls == [Settings().tags_consolidate_max_vocabulary]


async def test_propose_skips_model_when_fewer_than_two_tags(tmp_path: Path):
    store = FakeTagStore(counts=[("solo", 1)])
    chat = FakeChatProvider("fake-chat", reply="{}")
    service = _service(tmp_path, store=store, chat=chat)

    proposal = await service.propose()

    assert proposal.merges == []
    assert chat.calls == 0  # no model call spent on a 0/1-tag vault


async def test_propose_drops_hallucinated_tags(tmp_path: Path):
    store = FakeTagStore(counts=[("second-brain", 5), ("secondbrain", 2)])
    plan = {"merges": [{"canonical": "second-brain", "variants": ["secondbrain", "invented"]}]}
    chat = FakeChatProvider("fake-chat", reply=json.dumps(plan))
    service = _service(tmp_path, store=store, chat=chat)

    proposal = await service.propose()

    assert proposal.merges == [TagMerge(canonical="second-brain", variants=("secondbrain",))]


async def test_propose_propagates_provider_unavailable(tmp_path: Path):
    store = FakeTagStore(counts=[("a", 2), ("b", 1)])
    chat = FakeChatProvider("fake-chat", available=False)
    service = _service(tmp_path, store=store, chat=chat)

    with pytest.raises(ProviderUnavailable):
        await service.propose()


# --- apply --------------------------------------------------------------------------------------


def _write_note(vault: Path, rel: str, tags: list[str]) -> None:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    tag_list = ", ".join(tags)
    frontmatter = f"---\nid: x\nplane: Ideas\nplanes: [Ideas]\ntags: [{tag_list}]\nrelated: []\n---"
    path.write_text(f"{frontmatter}\n\n# T\n\nbody\n", encoding="utf-8")


async def test_apply_rewrites_affected_notes_and_reindexes(tmp_path: Path):
    vault = tmp_path / "vault"
    _write_note(vault, "Ideas/a.md", ["secondbrain", "calm"])
    _write_note(vault, "Ideas/b.md", ["second-brain-app"])
    _write_note(vault, "Ideas/c.md", ["unrelated"])
    store = FakeTagStore(
        nodes_by_tag={"secondbrain": ["Ideas/a.md"], "second-brain-app": ["Ideas/b.md"]}
    )
    indexer = FakeIndexer()
    backup = FakeCommitBackup()
    runs = FakeAgentRunStore()
    service = _service(tmp_path, store=store, indexer=indexer, backup=backup, runs=runs)

    plan = [TagMerge(canonical="second-brain", variants=("secondbrain", "second-brain-app"))]
    run_id = await service.apply(plan)
    await service.drain()

    assert "tags: [second-brain, calm]" in (vault / "Ideas/a.md").read_text(encoding="utf-8")
    assert "tags: [second-brain]" in (vault / "Ideas/b.md").read_text(encoding="utf-8")
    assert (vault / "Ideas/c.md").read_text(encoding="utf-8").count("unrelated") == 1  # untouched
    # only the two rewritten notes are reindexed + one commit+push.
    assert sorted(indexer.calls[0]) == ["Ideas/a.md", "Ideas/b.md"]
    assert backup.reasons == ["tags consolidate"]
    run = runs.runs[run_id]
    assert run.status == "succeeded"
    assert run.details["nodes_rewritten"] == 2


async def test_apply_with_no_affected_notes_makes_no_commit(tmp_path: Path):
    store = FakeTagStore(nodes_by_tag={})  # nothing carries the variant
    indexer = FakeIndexer()
    backup = FakeCommitBackup()
    runs = FakeAgentRunStore()
    service = _service(tmp_path, store=store, indexer=indexer, backup=backup, runs=runs)

    run_id = await service.apply([TagMerge(canonical="x", variants=("y",))])
    await service.drain()

    assert indexer.calls == []
    assert backup.reasons == []  # no writes → no commit
    assert runs.runs[run_id].status == "succeeded"


async def test_apply_skips_a_missing_note_and_still_succeeds(tmp_path: Path):
    vault = tmp_path / "vault"
    _write_note(vault, "Ideas/a.md", ["secondbrain"])
    # b.md is in the DB index but its file is gone on disk (stale index / external delete).
    store = FakeTagStore(nodes_by_tag={"secondbrain": ["Ideas/a.md", "Ideas/gone.md"]})
    runs = FakeAgentRunStore()
    backup = FakeCommitBackup()
    service = _service(tmp_path, store=store, backup=backup, runs=runs)

    run_id = await service.apply([TagMerge(canonical="second-brain", variants=("secondbrain",))])
    await service.drain()

    assert "tags: [second-brain]" in (vault / "Ideas/a.md").read_text(encoding="utf-8")
    assert runs.runs[run_id].status == "succeeded"  # missing note skipped, run still ok
    assert runs.runs[run_id].details["nodes_rewritten"] == 1
