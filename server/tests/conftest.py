"""Shared pytest fixtures / test-environment isolation."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_ambient_claude_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate ``Settings()`` from the developer's shell.

    The Claude Code CLI dev shell exports its own ``CLAUDE_EFFORT`` (the session's reasoning-effort
    setting), which collides with the server's ``CLAUDE_EFFORT`` config key (ADR-045 §5) and would
    otherwise leak into any test that constructs :class:`~app.config.Settings` and asserts a config
    default. Production sets ``CLAUDE_EFFORT`` deliberately via the deploy env, so this only affects
    local dev runs; clearing it here keeps the suite deterministic regardless of the runner's shell.
    """
    monkeypatch.delenv("CLAUDE_EFFORT", raising=False)
