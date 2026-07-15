"""Settings model-routing router tests (M4 task 5, 03-api §Settings, ADR-025/043/045): GET
/settings + PUT /settings/models over a real ModelRoutingService + registry of fakes — no DB, no
LLM, auth off. Routing keys on MODEL ids (ADR-045): a group's active/fallback + effort_by_model.
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

OPUS = "claude-opus-4-8"
SONNET = "claude-sonnet-4-6"
LLAMA = "meta-llama/Llama-3.3-70B-Instruct"


def _routing() -> ModelRoutingService:
    providers = {
        # One `claude` provider serving BOTH Claude models (ADR-045); effort-capable.
        "claude": FakeChatProvider(
            "claude", supports_effort=True, provider_label="Claude", models=[OPUS, SONNET]
        ),
        # Nebius serves one chat model, no reasoning effort.
        "nebius": FakeChatProvider("nebius", provider_label="Nebius", models=[LLAMA]),
        # STT/embedding-only: a ChatProvider by class but can_chat False → excluded from the picker.
        "stt-only": OpenAICompatibleProvider(id="stt-only", base_url="x", api_key="k"),
    }
    registry = ProviderRegistry(
        providers,
        chat_chain=[OPUS, LLAMA],
        distill_chain=[OPUS, LLAMA],
        embedding_provider_id="none",
        stt_chain=[],
    )
    settings = Settings(
        chat_chain=[OPUS, LLAMA], distill_chain=[OPUS, LLAMA], quick_chain=[SONNET, LLAMA]
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
    assert chat["active"] == OPUS
    assert chat["fallback"] == LLAMA
    # Effort seed lands only on the effort-capable model (claude_effort default = medium).
    assert chat["effort_by_model"] == {OPUS: "medium"}
    # quick seeds the cheaper Sonnet MODEL at claude_effort (ADR-045 §5 — no per-tier effort).
    assert groups["quick"]["effort_by_model"] == {SONNET: "medium"}


def test_get_settings_models_carry_effort_capability_and_exclude_non_chat():
    resp = _client(_routing()).get(f"{PREFIX}/settings")
    models = {m["id"]: m for m in resp.json()["groups"][0]["models"]}
    assert set(models) == {OPUS, SONNET, LLAMA}  # stt-only excluded
    assert models[OPUS]["supports_effort"] is True
    assert models[OPUS]["effort_levels"] == ["low", "medium", "high", "xhigh", "max"]
    assert models[OPUS]["label"] == "Claude Opus 4.8"  # model-derived friendly label (labels.py)
    assert models[LLAMA]["supports_effort"] is False
    assert models[LLAMA]["effort_levels"] == []
    assert models[LLAMA]["label"] == "Llama 3.3 70B"
    # Each model option carries its serving PROVIDER (derived — ADR-045 §1); both Claude models
    # resolve to the one `claude` provider, Llama to `nebius`.
    assert models[OPUS]["provider"] == "claude"
    assert models[SONNET]["provider"] == "claude"
    assert models[LLAMA]["provider"] == "nebius"


# --- PUT /settings/models -------------------------------------------------------------------------


def test_put_models_saves_and_is_forward_live():
    routing = _routing()
    client = _client(routing)
    resp = client.put(
        f"{PREFIX}/settings/models",
        json={
            "group": "chat",
            "active": LLAMA,
            "fallback": OPUS,
            "effort_by_model": {OPUS: "high"},
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["group"] == "chat"
    assert (body["active"], body["fallback"]) == (LLAMA, OPUS)
    assert body["effort_by_model"] == {OPUS: "high"}
    # Cache busted → a fresh GET reflects the saved routing (forward-live, no restart).
    chat = {g["group"]: g for g in client.get(f"{PREFIX}/settings").json()["groups"]}["chat"]
    assert chat["active"] == LLAMA
    assert chat["effort_by_model"] == {OPUS: "high"}
    # The PUT echoes the group's full editable state, incl. the pickable models list.
    assert {m["id"] for m in body["models"]} == {OPUS, SONNET, LLAMA}


def test_put_models_saves_a_non_chat_group():
    # Any of the 3 groups is savable, not just `chat` (conspect/quick share the machinery).
    routing = _routing()
    client = _client(routing)
    resp = client.put(f"{PREFIX}/settings/models", json={"group": "quick", "active": LLAMA})
    assert resp.status_code == 200
    assert resp.json()["group"] == "quick"
    quick = {g["group"]: g for g in client.get(f"{PREFIX}/settings").json()["groups"]}["quick"]
    assert quick["active"] == LLAMA


def test_put_models_unknown_active_is_422():
    resp = _client(_routing()).put(
        f"{PREFIX}/settings/models", json={"group": "chat", "active": "ghost"}
    )
    assert resp.status_code == 422


def test_put_models_unknown_fallback_is_422():
    resp = _client(_routing()).put(
        f"{PREFIX}/settings/models",
        json={"group": "chat", "active": OPUS, "fallback": "ghost"},
    )
    assert resp.status_code == 422


def test_put_models_unknown_group_is_422():
    resp = _client(_routing()).put(
        f"{PREFIX}/settings/models", json={"group": "bogus", "active": OPUS}
    )
    assert resp.status_code == 422  # Literal-constrained group


def test_put_models_effort_on_non_effort_model_is_422():
    resp = _client(_routing()).put(
        f"{PREFIX}/settings/models",
        json={"group": "chat", "active": OPUS, "effort_by_model": {LLAMA: "high"}},
    )
    assert resp.status_code == 422


def test_put_models_invalid_effort_level_is_422():
    resp = _client(_routing()).put(
        f"{PREFIX}/settings/models",
        json={"group": "chat", "active": OPUS, "effort_by_model": {OPUS: "ultra"}},
    )
    assert resp.status_code == 422


def test_put_models_missing_active_is_422():
    resp = _client(_routing()).put(f"{PREFIX}/settings/models", json={"group": "chat"})
    assert resp.status_code == 422
