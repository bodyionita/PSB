"""SystemHealth `backups` leg (ADR-014 §6) — no DB, no git; the drill state comes from a fake.

Covers the four ways the integrity-drill state maps to the leg, that a currently-running drill
doesn't flip health while the last good one is fresh, that the leg is prod-gated like git_remote,
and that a DB blip on the agent_runs read degrades rather than errors /health.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.config import Settings
from app.services import system_health
from app.services.agent_runs import AgentRun
from app.services.system_health import SystemHealth

from .fakes import FakeAgentRunStore


class FakeDB:
    async def healthcheck(self) -> bool:
        return True


def _drill(status: str, *, age_days: float = 0.0) -> AgentRun:
    started = datetime.now(UTC) - timedelta(days=age_days)
    return AgentRun(id="d", agent="integrity-drill", status=status, started_at=started)


def _health(tmp_path: Path, store, *, environment: str = "production") -> SystemHealth:
    # session_secret/mcp_token_hmac_secret must be real in production (config boot guard); this
    # test is about the backups health leg, so any non-default value suffices.
    settings = Settings(
        graph_store_path=str(tmp_path),
        environment=environment,
        session_secret="test-secret",
        mcp_token_hmac_secret="test-secret",
    )
    return SystemHealth(FakeDB(), settings, agent_runs=store)


async def test_backups_false_when_no_drill_ever_ran(tmp_path: Path):
    sh = _health(tmp_path, FakeAgentRunStore())
    assert await sh._backups_ok() is False


async def test_backups_false_when_latest_drill_failed(tmp_path: Path):
    store = FakeAgentRunStore()
    store.preloaded["integrity-drill"] = _drill("failed")
    sh = _health(tmp_path, store)
    assert await sh._backups_ok() is False


async def test_backups_true_when_recent_drill_succeeded(tmp_path: Path):
    store = FakeAgentRunStore()
    store.preloaded["integrity-drill"] = _drill("succeeded", age_days=1)
    sh = _health(tmp_path, store)
    assert await sh._backups_ok() is True


async def test_backups_false_when_last_good_drill_is_overdue(tmp_path: Path):
    store = FakeAgentRunStore()
    store.preloaded["integrity-drill"] = _drill("succeeded", age_days=9)  # > 8-day max age
    sh = _health(tmp_path, store)
    assert await sh._backups_ok() is False


async def test_running_drill_does_not_flip_health_when_last_good_is_fresh(tmp_path: Path):
    # latest() → running (newest), latest(status=succeeded) → an older-but-fresh success.
    store = FakeAgentRunStore()
    store.runs["1"] = _drill("succeeded", age_days=2)
    store.runs["2"] = _drill("running")
    sh = _health(tmp_path, store)
    assert await sh._backups_ok() is True


async def test_backups_false_when_agent_runs_read_errors(tmp_path: Path):
    class _Boom(FakeAgentRunStore):
        async def latest(self, agent, *, status=None):
            raise RuntimeError("db down")

    sh = _health(tmp_path, _Boom())
    assert await sh._backups_ok() is False


async def test_prod_check_degrades_on_backups_leg(tmp_path: Path, monkeypatch):
    # Isolate the backups leg: force the store + git_remote legs green so `ok` turns on backups.
    monkeypatch.setattr(system_health, "_store_ok", lambda p: True)
    monkeypatch.setattr(system_health, "_git_remote_ok", lambda p: True)
    report = await _health(tmp_path, FakeAgentRunStore(), environment="production").check()
    assert report.backups is False and report.ok is False


async def test_dev_check_reports_backups_but_does_not_gate_on_it(tmp_path: Path):
    # No drill in dev ⇒ backups False, but dev /health ignores it (deferred to provisioning).
    report = await _health(tmp_path, FakeAgentRunStore(), environment="development").check()
    assert report.backups is False and report.ok is True
