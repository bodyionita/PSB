"""Admin router test: POST /admin/backup delegates to the store backup and returns its result."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.dependencies import (
    get_edge_consolidation_service,
    get_identity_capsule_service,
    get_merge_service,
    get_node_delete_service,
    get_orphan_keep_service,
    get_registry,
    get_reindex_service,
    get_reprocess_service,
    get_store_backup,
    get_tag_consolidation_service,
    require_session,
)
from app.entities.keep_store import KeepDecision
from app.entities.merge import BadMerge, MergeNodeNotFound, MergeProposal, MergeSide
from app.providers.base import ProviderUnavailable
from app.providers.registry import ProviderReport
from app.providers.status import ProviderError
from app.routers import admin
from app.services.node_delete import (
    NodeDeleteIsContent,
    NodeDeleteNotFound,
    NodeDeleteNotOrphan,
)
from app.services.orphan_keep import (
    OrphanKeepIsContent,
    OrphanKeepKeyNotFound,
    OrphanKeepNotFound,
)
from app.services.store_backup import BackupResult
from app.tags.consolidation import TagMerge
from app.tags.service import ConsolidationProposal
from app.vocab.edge_consolidation import (
    BadConsolidation,
    EdgeConsolidationProposal,
    EdgeRetype,
)

PREFIX = "/api/v1"


class FakeBackup:
    def __init__(self, result: BackupResult) -> None:
        self._result = result
        self.calls = 0

    async def backup_now(self, reason: str = "manual backup") -> BackupResult:
        self.calls += 1
        return self._result


class FakeReindex:
    """POST /admin/reindex fake: start_manual returns a run_id, or None to force the 409 path."""

    def __init__(self, run_id: str | None) -> None:
        self._run_id = run_id
        self.calls = 0

    async def start_manual(self) -> str | None:
        self.calls += 1
        return self._run_id


@pytest.fixture
def client_and_backup():
    app = FastAPI()
    app.include_router(admin.router, prefix=PREFIX)
    fake = FakeBackup(BackupResult(committed=True, pushed=True))
    app.dependency_overrides[get_store_backup] = lambda: fake
    app.dependency_overrides[require_session] = lambda: None
    return TestClient(app), fake


def _reindex_client(run_id: str | None) -> tuple[TestClient, FakeReindex]:
    app = FastAPI()
    app.include_router(admin.router, prefix=PREFIX)
    fake = FakeReindex(run_id)
    app.dependency_overrides[get_reindex_service] = lambda: fake
    app.dependency_overrides[require_session] = lambda: None
    return TestClient(app), fake


def test_backup_returns_result(client_and_backup):
    client, fake = client_and_backup
    resp = client.post(f"{PREFIX}/admin/backup")
    assert resp.status_code == 200
    assert resp.json() == {"committed": True, "pushed": True}
    assert fake.calls == 1


def test_reindex_returns_202_with_run_id():
    client, fake = _reindex_client("run-42")
    resp = client.post(f"{PREFIX}/admin/reindex")
    assert resp.status_code == 202
    assert resp.json() == {"run_id": "run-42"}
    assert fake.calls == 1


def test_reindex_returns_409_when_already_running():
    client, fake = _reindex_client(None)  # start_manual → None ⇒ single-flight conflict
    resp = client.post(f"{PREFIX}/admin/reindex")
    assert resp.status_code == 409
    assert fake.calls == 1


# --- POST /admin/identity-capsule/refresh (M5 task 2, ADR-046 §5) ---
class FakeCapsuleTrigger:
    """trigger() returns a run_id, or None to force the 409 single-flight path."""

    def __init__(self, run_id: str | None) -> None:
        self._run_id = run_id
        self.calls = 0

    async def trigger(self) -> str | None:
        self.calls += 1
        return self._run_id


def _capsule_client(run_id: str | None) -> tuple[TestClient, FakeCapsuleTrigger]:
    app = FastAPI()
    app.include_router(admin.router, prefix=PREFIX)
    fake = FakeCapsuleTrigger(run_id)
    app.dependency_overrides[get_identity_capsule_service] = lambda: fake
    app.dependency_overrides[require_session] = lambda: None
    return TestClient(app), fake


def test_identity_capsule_refresh_returns_202_with_run_id():
    client, fake = _capsule_client("run-7")
    resp = client.post(f"{PREFIX}/admin/identity-capsule/refresh")
    assert resp.status_code == 202
    assert resp.json() == {"run_id": "run-7"}
    assert fake.calls == 1


def test_identity_capsule_refresh_returns_409_when_already_running():
    client, fake = _capsule_client(None)
    resp = client.post(f"{PREFIX}/admin/identity-capsule/refresh")
    assert resp.status_code == 409
    assert fake.calls == 1


# --- POST /admin/reprocess (ADR-042, M3 task 11) ---
class FakeReprocess:
    """confirm=false → preview; confirm=true → apply (run_id, or None for the 409 path)."""

    def __init__(self, *, run_id: str | None = "run-9") -> None:
        from app.services.reprocess import ReprocessPreview

        self._run_id = run_id
        self._preview = ReprocessPreview(captures=4, nodes=12, merges=0)
        self.applied = 0
        self.previewed = 0

    async def preview(self):
        self.previewed += 1
        return self._preview

    async def apply(self):
        self.applied += 1
        return self._run_id


def _reprocess_client(fake: FakeReprocess) -> TestClient:
    app = FastAPI()
    app.include_router(admin.router, prefix=PREFIX)
    app.dependency_overrides[get_reprocess_service] = lambda: fake
    app.dependency_overrides[require_session] = lambda: None
    return TestClient(app)


def test_reprocess_preview_returns_counts():
    fake = FakeReprocess()
    resp = _reprocess_client(fake).post(f"{PREFIX}/admin/reprocess", json={"confirm": False})
    assert resp.status_code == 200
    assert resp.json() == {"captures": 4, "nodes": 12, "merges": 0}
    assert fake.previewed == 1 and fake.applied == 0


def test_reprocess_confirm_returns_202_run_id():
    fake = FakeReprocess(run_id="run-9")
    resp = _reprocess_client(fake).post(f"{PREFIX}/admin/reprocess", json={"confirm": True})
    assert resp.status_code == 202
    assert resp.json() == {"run_id": "run-9"}
    assert fake.applied == 1


def test_reprocess_confirm_409_when_already_running():
    fake = FakeReprocess(run_id=None)
    resp = _reprocess_client(fake).post(f"{PREFIX}/admin/reprocess", json={"confirm": True})
    assert resp.status_code == 409


class FakeTagConsolidation:
    """propose returns a preset proposal (or raises); apply returns a run_id, recording the plan."""

    def __init__(
        self, *, proposal: ConsolidationProposal | None = None, propose_raises: bool = False
    ) -> None:
        self._proposal = proposal
        self._propose_raises = propose_raises
        self.applied: list[TagMerge] | None = None

    async def propose(self) -> ConsolidationProposal:
        if self._propose_raises:
            raise ProviderUnavailable("distill chain down")
        return self._proposal

    async def apply(self, plan: list[TagMerge]) -> str:
        self.applied = plan
        return "run-tags-1"


def _tags_client(fake: FakeTagConsolidation) -> TestClient:
    app = FastAPI()
    app.include_router(admin.router, prefix=PREFIX)
    app.dependency_overrides[get_tag_consolidation_service] = lambda: fake
    app.dependency_overrides[require_session] = lambda: None
    return TestClient(app)


def test_tags_consolidate_propose_returns_plan():
    proposal = ConsolidationProposal(
        plan_id="plan-9",
        merges=[TagMerge(canonical="second-brain", variants=("secondbrain",))],
    )
    client = _tags_client(FakeTagConsolidation(proposal=proposal))
    resp = client.post(f"{PREFIX}/admin/tags/consolidate", json={"apply": False})
    assert resp.status_code == 200
    assert resp.json() == {
        "plan_id": "plan-9",
        "merges": [{"canonical": "second-brain", "variants": ["secondbrain"]}],
    }


def test_tags_consolidate_propose_503_when_chain_down():
    client = _tags_client(FakeTagConsolidation(propose_raises=True))
    resp = client.post(f"{PREFIX}/admin/tags/consolidate", json={})
    assert resp.status_code == 503


def test_tags_consolidate_apply_returns_202_run_id():
    fake = FakeTagConsolidation()
    client = _tags_client(fake)
    resp = client.post(
        f"{PREFIX}/admin/tags/consolidate",
        json={
            "apply": True,
            "plan": [{"canonical": "second-brain", "variants": ["secondbrain"]}],
        },
    )
    assert resp.status_code == 202
    assert resp.json() == {"run_id": "run-tags-1"}
    assert fake.applied == [TagMerge(canonical="second-brain", variants=("secondbrain",))]


def test_tags_consolidate_apply_without_plan_is_400():
    fake = FakeTagConsolidation()
    client = _tags_client(fake)
    resp = client.post(f"{PREFIX}/admin/tags/consolidate", json={"apply": True})
    assert resp.status_code == 400
    assert fake.applied is None


class FakeEdgeConsolidation:
    """propose returns a preset proposal (or raises); apply returns a run_id, recording the plan."""

    def __init__(
        self,
        *,
        proposal: EdgeConsolidationProposal | None = None,
        propose_error: Exception | None = None,
        apply_error: Exception | None = None,
    ) -> None:
        self._proposal = proposal
        self._propose_error = propose_error
        self._apply_error = apply_error
        self.applied: tuple[str, list[EdgeRetype]] | None = None

    async def propose(self, rel: str) -> EdgeConsolidationProposal:
        if self._propose_error is not None:
            raise self._propose_error
        return self._proposal

    async def apply(self, rel: str, plan: list[EdgeRetype]) -> str:
        if self._apply_error is not None:
            raise self._apply_error
        self.applied = (rel, plan)
        return "run-vocab-1"


def _vocab_client(fake: FakeEdgeConsolidation) -> TestClient:
    app = FastAPI()
    app.include_router(admin.router, prefix=PREFIX)
    app.dependency_overrides[get_edge_consolidation_service] = lambda: fake
    app.dependency_overrides[require_session] = lambda: None
    return TestClient(app)


def test_vocab_consolidate_propose_returns_plan():
    proposal = EdgeConsolidationProposal(
        plan_id="plan-e1",
        rel="mentors",
        retypings=[EdgeRetype(src_id="s1", to="d1", from_rel="involves", to_rel="mentors")],
    )
    client = _vocab_client(FakeEdgeConsolidation(proposal=proposal))
    resp = client.post(f"{PREFIX}/admin/vocab/consolidate", json={"rel": "mentors"})
    assert resp.status_code == 200
    assert resp.json() == {
        "plan_id": "plan-e1",
        "rel": "mentors",
        "retypings": [{"src_id": "s1", "to": "d1", "from_rel": "involves", "to_rel": "mentors"}],
    }


def test_vocab_consolidate_propose_400_unknown_rel():
    fake = FakeEdgeConsolidation(propose_error=BadConsolidation("unknown edge rel 'x'"))
    client = _vocab_client(fake)
    resp = client.post(f"{PREFIX}/admin/vocab/consolidate", json={"rel": "x"})
    assert resp.status_code == 400


def test_vocab_consolidate_propose_503_when_chain_down():
    client = _vocab_client(FakeEdgeConsolidation(propose_error=ProviderUnavailable("distill down")))
    resp = client.post(f"{PREFIX}/admin/vocab/consolidate", json={"rel": "mentors"})
    assert resp.status_code == 503


def test_vocab_consolidate_apply_returns_202_run_id():
    fake = FakeEdgeConsolidation()
    client = _vocab_client(fake)
    resp = client.post(
        f"{PREFIX}/admin/vocab/consolidate",
        json={
            "rel": "mentors",
            "apply": True,
            "plan": [{"src_id": "s1", "to": "d1", "from_rel": "involves", "to_rel": "mentors"}],
        },
    )
    assert resp.status_code == 202
    assert resp.json() == {"run_id": "run-vocab-1"}
    assert fake.applied == (
        "mentors",
        [EdgeRetype(src_id="s1", to="d1", from_rel="involves", to_rel="mentors")],
    )


def test_vocab_consolidate_empty_rel_is_400_not_422():
    # No schema min_length: an empty rel reaches the service, which 400s (unknown rel) per 03-api.
    fake = FakeEdgeConsolidation(propose_error=BadConsolidation("unknown edge rel ''"))
    client = _vocab_client(fake)
    resp = client.post(f"{PREFIX}/admin/vocab/consolidate", json={"rel": ""})
    assert resp.status_code == 400


def test_vocab_consolidate_apply_without_plan_is_400():
    fake = FakeEdgeConsolidation()
    client = _vocab_client(fake)
    resp = client.post(f"{PREFIX}/admin/vocab/consolidate", json={"rel": "mentors", "apply": True})
    assert resp.status_code == 400
    assert fake.applied is None


class FakeMerge:
    """propose returns a preset proposal (or raises); apply returns a run_id."""

    def __init__(self, *, proposal: MergeProposal | None = None, error: Exception | None = None):
        self._proposal = proposal
        self._error = error
        self.applied: tuple[str, str] | None = None

    async def propose(self, loser: str, survivor: str) -> MergeProposal:
        if self._error is not None:
            raise self._error
        return self._proposal

    async def apply(self, loser: str, survivor: str) -> str:
        if self._error is not None:
            raise self._error
        self.applied = (loser, survivor)
        return "run-merge-1"


def _merge_client(fake: FakeMerge) -> TestClient:
    app = FastAPI()
    app.include_router(admin.router, prefix=PREFIX)
    app.dependency_overrides[get_merge_service] = lambda: fake
    app.dependency_overrides[require_session] = lambda: None
    return TestClient(app)


def test_entities_merge_propose_returns_inventory():
    proposal = MergeProposal(
        plan_id="plan-1",
        loser=MergeSide(id="l", type="person", title="Alex", aliases=["alex"]),
        survivor=MergeSide(id="s", type="person", title="Alexandru", aliases=["alexandru"]),
    )
    client = _merge_client(FakeMerge(proposal=proposal))
    resp = client.post(f"{PREFIX}/admin/entities/merge", json={"loser": "l", "survivor": "s"})
    assert resp.status_code == 200
    assert resp.json()["plan_id"] == "plan-1"
    assert resp.json()["inbound_count"] == 0


def test_entities_merge_apply_returns_202_run_id():
    fake = FakeMerge()
    client = _merge_client(fake)
    resp = client.post(
        f"{PREFIX}/admin/entities/merge",
        json={"loser": "l", "survivor": "s", "apply": True},
    )
    assert resp.status_code == 202
    assert resp.json() == {"run_id": "run-merge-1"}
    assert fake.applied == ("l", "s")


def test_entities_merge_404_unknown_node():
    client = _merge_client(FakeMerge(error=MergeNodeNotFound("l")))
    resp = client.post(f"{PREFIX}/admin/entities/merge", json={"loser": "l", "survivor": "s"})
    assert resp.status_code == 404


def test_entities_merge_400_bad_merge():
    client = _merge_client(FakeMerge(error=BadMerge("cannot merge a node into itself")))
    resp = client.post(f"{PREFIX}/admin/entities/merge", json={"loser": "x", "survivor": "x"})
    assert resp.status_code == 400


# --- POST /admin/nodes/{id}/delete (ADR-064 §5, M9.8 T5) ---
class FakeNodeDelete:
    """delete returns a run_id, or raises the routing/validation error the router maps."""

    def __init__(self, *, error: Exception | None = None) -> None:
        self._error = error
        self.deleted: str | None = None

    async def delete(self, node_id: str) -> str:
        if self._error is not None:
            raise self._error
        self.deleted = node_id
        return "run-del-1"


def _node_delete_client(fake: FakeNodeDelete) -> TestClient:
    app = FastAPI()
    app.include_router(admin.router, prefix=PREFIX)
    app.dependency_overrides[get_node_delete_service] = lambda: fake
    app.dependency_overrides[require_session] = lambda: None
    return TestClient(app)


def test_node_delete_returns_202_run_id():
    fake = FakeNodeDelete()
    resp = _node_delete_client(fake).post(f"{PREFIX}/admin/nodes/hub-1/delete")
    assert resp.status_code == 202
    assert resp.json() == {"run_id": "run-del-1"}
    assert fake.deleted == "hub-1"


def test_node_delete_404_unknown():
    resp = _node_delete_client(FakeNodeDelete(error=NodeDeleteNotFound("x"))).post(
        f"{PREFIX}/admin/nodes/x/delete"
    )
    assert resp.status_code == 404


def test_node_delete_400_content_node():
    resp = _node_delete_client(FakeNodeDelete(error=NodeDeleteIsContent("m"))).post(
        f"{PREFIX}/admin/nodes/m/delete"
    )
    assert resp.status_code == 400


def test_node_delete_409_still_referenced():
    resp = _node_delete_client(FakeNodeDelete(error=NodeDeleteNotOrphan(3))).post(
        f"{PREFIX}/admin/nodes/hub-1/delete"
    )
    assert resp.status_code == 409
    assert "3 canonical edge" in resp.json()["detail"]


# --- orphan keep-list endpoints (ADR-064 §5, M9.8 T5.5) ---
class FakeOrphanKeep:
    """keep returns a KeepDecision (or raises); list returns keeps; unkeep raises on unknown."""

    def __init__(self, *, keep_error=None, unkeep_error=None, keeps=None) -> None:
        self._keep_error = keep_error
        self._unkeep_error = unkeep_error
        self._keeps = list(keeps or [])
        self.kept: str | None = None
        self.unkept: str | None = None

    async def keep(self, node_id: str) -> KeepDecision:
        if self._keep_error is not None:
            raise self._keep_error
        self.kept = node_id
        return KeepDecision(node_type="person", forms=["father", "dad"], node_id=node_id)

    async def list_keeps(self):
        return self._keeps

    async def unkeep(self, key: str) -> None:
        if self._unkeep_error is not None:
            raise self._unkeep_error
        self.unkept = key


def _orphan_keep_client(fake: FakeOrphanKeep) -> TestClient:
    app = FastAPI()
    app.include_router(admin.router, prefix=PREFIX)
    app.dependency_overrides[get_orphan_keep_service] = lambda: fake
    app.dependency_overrides[require_session] = lambda: None
    return TestClient(app)


def test_keep_node_returns_200_with_item():
    fake = FakeOrphanKeep()
    resp = _orphan_keep_client(fake).post(f"{PREFIX}/admin/nodes/hub-1/keep")
    assert resp.status_code == 200
    body = resp.json()
    assert body["type"] == "person"
    assert body["label"] == "father"
    assert body["key"]  # the stable keep_key
    assert fake.kept == "hub-1"


def test_keep_node_404_unknown():
    resp = _orphan_keep_client(FakeOrphanKeep(keep_error=OrphanKeepNotFound("x"))).post(
        f"{PREFIX}/admin/nodes/x/keep"
    )
    assert resp.status_code == 404


def test_keep_node_400_content_node():
    resp = _orphan_keep_client(FakeOrphanKeep(keep_error=OrphanKeepIsContent("m"))).post(
        f"{PREFIX}/admin/nodes/m/keep"
    )
    assert resp.status_code == 400


def test_list_orphan_keeps_maps_to_wire_shape():
    at = datetime(2026, 7, 19, 9, 0, 0, tzinfo=UTC)
    keeps = [KeepDecision(node_type="person", forms=["mother"], node_id="h2", created_at=at)]
    resp = _orphan_keep_client(FakeOrphanKeep(keeps=keeps)).get(f"{PREFIX}/admin/orphan-keeps")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["type"] == "person"
    assert body[0]["label"] == "mother"
    assert body[0]["kept_at"] == "2026-07-19T09:00:00Z"


def test_delete_orphan_keep_returns_204():
    fake = FakeOrphanKeep()
    resp = _orphan_keep_client(fake).delete(f"{PREFIX}/admin/orphan-keeps/some-key")
    assert resp.status_code == 204
    assert fake.unkept == "some-key"


def test_delete_orphan_keep_404_unknown_key():
    resp = _orphan_keep_client(FakeOrphanKeep(unkeep_error=OrphanKeepKeyNotFound("k"))).delete(
        f"{PREFIX}/admin/orphan-keeps/k"
    )
    assert resp.status_code == 404


# --- GET /admin/providers (ADR-044, M4 follow-up) ---
class FakeRegistry:
    """provider_report returns a preset report; the router maps it to the wire shape."""

    def __init__(self, report: list[ProviderReport]) -> None:
        self._report = report
        self.calls = 0

    async def provider_report(self) -> list[ProviderReport]:
        self.calls += 1
        return self._report


def _providers_client(report: list[ProviderReport]) -> tuple[TestClient, FakeRegistry]:
    app = FastAPI()
    app.include_router(admin.router, prefix=PREFIX)
    fake = FakeRegistry(report)
    app.dependency_overrides[get_registry] = lambda: fake
    app.dependency_overrides[require_session] = lambda: None
    return TestClient(app), fake


def test_providers_maps_report_to_wire_shape():
    at = datetime(2026, 7, 15, 12, 0, 0, tzinfo=UTC)
    report = [
        # A provider with a sticky error → last_error present and serialized as {message, at}.
        ProviderReport(
            id="nebius",
            label="Llama 3.3 70B",
            capabilities=["chat"],
            reachable=False,
            last_error=ProviderError(message="nebius chat failed: 404", at=at),
            last_success_at=None,
            consecutive_failures=2,
        ),
        # A healthy provider with no error → last_error is null (the other mapping branch).
        ProviderReport(
            id="ollama",
            label="ollama",
            capabilities=["embedding"],
            reachable=True,
            last_error=None,
            last_success_at=at,
            consecutive_failures=0,
        ),
    ]
    client, fake = _providers_client(report)
    resp = client.get(f"{PREFIX}/admin/providers")
    assert resp.status_code == 200
    assert fake.calls == 1
    body = resp.json()
    assert body == {
        "providers": [
            {
                "id": "nebius",
                "label": "Llama 3.3 70B",
                "capabilities": ["chat"],
                "reachable": False,
                "last_error": {"message": "nebius chat failed: 404", "at": "2026-07-15T12:00:00Z"},
                "last_success_at": None,
                "consecutive_failures": 2,
            },
            {
                "id": "ollama",
                "label": "ollama",
                "capabilities": ["embedding"],
                "reachable": True,
                "last_error": None,
                "last_success_at": "2026-07-15T12:00:00Z",
                "consecutive_failures": 0,
            },
        ]
    }


def test_providers_requires_session():
    from app.config import Settings

    app = FastAPI()
    app.state.settings = Settings(session_cookie_name="braindan_session")

    class _DenyAuth:
        async def validate(self, token):
            return None

    app.state.auth_service = _DenyAuth()
    app.include_router(admin.router, prefix=PREFIX)
    client = TestClient(app)
    assert client.get(f"{PREFIX}/admin/providers").status_code == 401


def test_backup_requires_session():
    from app.config import Settings

    app = FastAPI()
    app.state.settings = Settings(session_cookie_name="braindan_session")

    class _DenyAuth:
        async def validate(self, token):
            return None

    app.state.auth_service = _DenyAuth()
    app.include_router(admin.router, prefix=PREFIX)
    client = TestClient(app)
    assert client.post(f"{PREFIX}/admin/backup").status_code == 401
