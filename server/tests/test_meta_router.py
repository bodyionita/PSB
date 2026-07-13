"""Meta router tests: GET /planes returns the configured plane vocabulary (no DB, auth bypassed)."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.dependencies import get_settings, require_session
from app.routers import meta

PREFIX = "/api/v1"


def _client(settings: Settings) -> TestClient:
    app = FastAPI()
    app.include_router(meta.router, prefix=PREFIX)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[require_session] = lambda: None  # bypass auth
    return TestClient(app)


def test_planes_returns_configured_planes_and_inbox():
    settings = Settings(planes=["Work", "Home"], inbox_folder="inbox")
    resp = _client(settings).get(f"{PREFIX}/planes")
    assert resp.status_code == 200
    body = resp.json()
    assert body["planes"] == ["Work", "Home"]
    assert body["inbox"] == "inbox"


def test_planes_empty_config_still_returns_inbox():
    settings = Settings(planes=[], inbox_folder="inbox")
    body = _client(settings).get(f"{PREFIX}/planes").json()
    assert body["planes"] == []
    assert body["inbox"] == "inbox"
