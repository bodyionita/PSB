"""Admin router test: POST /admin/backup delegates to the vault backup and returns its result."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.dependencies import get_vault_backup, require_session
from app.routers import admin
from app.services.vault_backup import BackupResult

PREFIX = "/api/v1"


class FakeBackup:
    def __init__(self, result: BackupResult) -> None:
        self._result = result
        self.calls = 0

    async def backup_now(self, reason: str = "manual backup") -> BackupResult:
        self.calls += 1
        return self._result


@pytest.fixture
def client_and_backup():
    app = FastAPI()
    app.include_router(admin.router, prefix=PREFIX)
    fake = FakeBackup(BackupResult(committed=True, pushed=True))
    app.dependency_overrides[get_vault_backup] = lambda: fake
    app.dependency_overrides[require_session] = lambda: None
    return TestClient(app), fake


def test_backup_returns_result(client_and_backup):
    client, fake = client_and_backup
    resp = client.post(f"{PREFIX}/admin/backup")
    assert resp.status_code == 200
    assert resp.json() == {"committed": True, "pushed": True}
    assert fake.calls == 1


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
