"""Settings model-routing router tests (M4 task 5, 03-api §Settings, ADR-025/043): GET /settings +
PUT /settings/models over a real ModelRoutingService + registry of fakes — no DB, no LLM, auth off.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.dependencies import get_model_routing, require_session
from app.providers.openai_compatible import OpenAICompatibleProvider
from app.providers.registry import ProviderRegistry
from app.routers import settings as settings_router
from app.services.model_routing import ModelRoutingService

from .fakes import FakeChatProvider, FakeModelRoutingStore

PREFIX = "/api/v1"


def _routing() -> ModelRoutingService:
    providers = {
        "claude-max": FakeChatProvider("claude-max", supports_effort=True, label="Claude Opus"),
        "nebius": FakeChatProvider("nebius", label="Llama 70B"),
        # STT/embedding-only: a ChatProvider by class but can_chat False → excluded from the picker.
        "stt-only": OpenAICompatibleProvider(id="stt-only", base_url="x", api_key="k"),
    }
    registry = ProviderRegistry(
        providers,
        chat_chain=["claude-max", "nebius"],
        distill_chain=["claude-max", "nebius"],
        embedding_provider_id="none",
        stt_chain=[],
    )
    settings = Settings(
        chat_chain=["claude-max", "nebius"],
        distill_chain=["claude-max", "nebius"],
        quick_chain=["claude-max", "nebius"],
    )
    return ModelRoutingService(
        settings=settings, store=FakeModelRoutingStore(), registry=registry
    )


def _client(routing: ModelRoutingService) -> TestClient:
    app = FastAPI()
    app.include_router(settings_router.router, prefix=PREFIX)
    app.dependency_overrides[get_model_routing] = lambda: routing
    app.dependency_overrides[require_session] = lambda: None  # bypass auth
    return TestClient(app)


# --- GET /settings --------------------------------------------------------------------------------


def test_get_settings_returns_all_three_groups_from_seed():
    resp = _client(_routing()).get(f"{PREFIX}/settings")
    assert resp.status_code == 200
    groups = {g["group"]: g for g in resp.json()["groups"]}
    assert set(groups) == {"chat", "conspect", "quick"}
    chat = groups["chat"]
    assert chat["active"] == "claude-max"
    assert chat["fallback"] == "nebius"
    # Effort seed lands only on the effort-capable provider (claude_max_effort default = medium).
    assert chat["effort_by_provider"] == {"claude-max": "medium"}
    # quick seeds its own cheaper effort (ADR-043).
    assert groups["quick"]["effort_by_provider"] == {"claude-max": "low"}


def test_get_settings_models_carry_effort_capability_and_exclude_non_chat():
    resp = _client(_routing()).get(f"{PREFIX}/settings")
    models = {m["id"]: m for m in resp.json()["groups"][0]["models"]}
    assert set(models) == {"claude-max", "nebius"}  # stt-only excluded
    assert models["claude-max"]["supports_effort"] is True
    assert models["claude-max"]["effort_levels"] == ["low", "medium", "high", "xhigh", "max"]
    assert models["claude-max"]["label"] == "Claude Opus"
    assert models["nebius"]["supports_effort"] is False
    assert models["nebius"]["effort_levels"] == []


# --- PUT /settings/models -------------------------------------------------------------------------


def test_put_models_saves_and_is_forward_live():
    routing = _routing()
    client = _client(routing)
    resp = client.put(
        f"{PREFIX}/settings/models",
        json={
            "group": "chat",
            "active": "nebius",
            "fallback": "claude-max",
            "effort_by_provider": {"claude-max": "high"},
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["group"] == "chat"
    assert (body["active"], body["fallback"]) == ("nebius", "claude-max")
    assert body["effort_by_provider"] == {"claude-max": "high"}
    # Cache busted → a fresh GET reflects the saved routing (forward-live, no restart).
    chat = {g["group"]: g for g in client.get(f"{PREFIX}/settings").json()["groups"]}["chat"]
    assert chat["active"] == "nebius"
    assert chat["effort_by_provider"] == {"claude-max": "high"}
    # The PUT echoes the group's full editable state, incl. the pickable models list.
    assert {m["id"] for m in body["models"]} == {"claude-max", "nebius"}


def test_put_models_saves_a_non_chat_group():
    # Any of the 3 groups is savable, not just `chat` (conspect/quick share the machinery).
    routing = _routing()
    client = _client(routing)
    resp = client.put(
        f"{PREFIX}/settings/models", json={"group": "quick", "active": "nebius"}
    )
    assert resp.status_code == 200
    assert resp.json()["group"] == "quick"
    quick = {g["group"]: g for g in client.get(f"{PREFIX}/settings").json()["groups"]}["quick"]
    assert quick["active"] == "nebius"


def test_put_models_unknown_active_is_422():
    resp = _client(_routing()).put(
        f"{PREFIX}/settings/models", json={"group": "chat", "active": "ghost"}
    )
    assert resp.status_code == 422


def test_put_models_unknown_fallback_is_422():
    resp = _client(_routing()).put(
        f"{PREFIX}/settings/models",
        json={"group": "chat", "active": "claude-max", "fallback": "ghost"},
    )
    assert resp.status_code == 422


def test_put_models_unknown_group_is_422():
    resp = _client(_routing()).put(
        f"{PREFIX}/settings/models", json={"group": "bogus", "active": "claude-max"}
    )
    assert resp.status_code == 422  # Literal-constrained group


def test_put_models_effort_on_non_effort_model_is_422():
    resp = _client(_routing()).put(
        f"{PREFIX}/settings/models",
        json={"group": "chat", "active": "claude-max", "effort_by_provider": {"nebius": "high"}},
    )
    assert resp.status_code == 422


def test_put_models_invalid_effort_level_is_422():
    resp = _client(_routing()).put(
        f"{PREFIX}/settings/models",
        json={
            "group": "chat",
            "active": "claude-max",
            "effort_by_provider": {"claude-max": "ultra"},
        },
    )
    assert resp.status_code == 422


def test_put_models_missing_active_is_422():
    resp = _client(_routing()).put(f"{PREFIX}/settings/models", json={"group": "chat"})
    assert resp.status_code == 422
