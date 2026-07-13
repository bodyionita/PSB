"""Claude Max provider — primary mind, driven through the headless Claude CLI (ADR-004).

A Max subscription is not an API key: programmatic use goes through the Claude Agent SDK /
CLI (OAuth), whose credentials live on a Docker volume and are established once with
``claude login`` (07-infrastructure.md).

**Health-guarded (ADR-012):** if the CLI is absent or not logged in, ``health()`` reports
False and ``complete()`` raises :class:`ProviderUnavailable`, so the chain falls back to
Nebius and records ``fallback_used``. On the VPS, ``claude login`` lights up the real path
with zero code change — nothing here special-cases dev vs prod.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess

from .base import ChatMessage, ChatProvider, ProviderUnavailable


def _render_prompt(messages: list[ChatMessage]) -> str:
    """Flatten a message list into a single prompt for the CLI's print mode."""
    parts: list[str] = []
    for m in messages:
        if m.role == "system":
            parts.append(f"[system]\n{m.content}")
        elif m.role == "assistant":
            parts.append(f"[assistant]\n{m.content}")
        else:
            parts.append(m.content)
    return "\n\n".join(parts)


class ClaudeMaxProvider(ChatProvider):
    def __init__(
        self,
        *,
        id: str = "claude-max",
        model: str,
        effort: str = "medium",
        cli_path: str = "claude",
    ) -> None:
        self.id = id
        self._model = model
        self._effort = effort
        self._cli_name = cli_path

    def _resolve_cli(self) -> str | None:
        return shutil.which(self._cli_name)

    async def health(self) -> bool:
        """True only if the CLI exists and responds — cheap, no LLM call, never raises."""
        cli = self._resolve_cli()
        if cli is None:
            return False
        try:
            return await asyncio.to_thread(self._probe_version, cli)
        except Exception:
            return False

    @staticmethod
    def _probe_version(cli: str) -> bool:
        try:
            result = subprocess.run([cli, "--version"], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return False
        return result.returncode == 0

    async def complete(self, messages: list[ChatMessage], *, model: str | None = None) -> str:
        cli = self._resolve_cli()
        if cli is None:
            raise ProviderUnavailable("claude-max: CLI not found on PATH")
        prompt = _render_prompt(messages)
        try:
            result = await asyncio.to_thread(
                self._run_cli, cli, prompt, model or self._model, self._effort
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ProviderUnavailable(f"claude-max invocation failed: {exc}") from exc
        if result.returncode != 0:
            # Most commonly: not logged in, or usage window exhausted. Fall back.
            raise ProviderUnavailable(
                f"claude-max returned {result.returncode}: {result.stderr.strip()[:200]}"
            )
        text = result.stdout.strip()
        if not text:
            raise ProviderUnavailable("claude-max returned empty output")
        return text

    @staticmethod
    def _run_cli(
        cli: str, prompt: str, model: str, effort: str
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [cli, "--print", "--model", model, "--effort", effort, prompt],
            capture_output=True,
            text=True,
            timeout=300,
        )
