"""Admin router test: POST /admin/backup delegates to the vault backup and returns its result."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.dependencies import (
    get_reindex_service,
    get_tag_consolidation_service,
    get_vault_backup,
    require_session,
)
from app.providers.base import ProviderUnavailable
from app.routers import admin
from app.services.vault_backup import BackupResult
from app.tags.consolidation import TagMerge
from app.tags.service import ConsolidationProposal

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
    app.dependency_overrides[get_vault_backup] = lambda: fake
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
